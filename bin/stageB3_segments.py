#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Tuple

def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def atomic_write_json(path: str, obj: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass

def ffprobe_duration_seconds(path: str) -> float:
    """
    Return container duration in seconds using ffprobe.
    Returns 0.0 if unavailable.
    """
    cmd = [
        "ffprobe", "-hide_banner", "-loglevel", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        path
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", errors="replace").strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0

def run_ffmpeg_extract_wav(src_mp4: str, out_wav: str) -> Tuple[bool, str]:
    """
    Deterministic WAV extraction (mono, 16kHz, s16le).
    """
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src_mp4,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        out_wav
    ]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, (e.output.decode("utf-8", errors="replace") if e.output else str(e))

def read_wav_rms_envelope(wav_path: str, win_s: float, step_s: float) -> Tuple[List[float], float, bool]:
    """
    Compute RMS envelope using stdlib only (wave + struct).

    Returns:
      (rms_list, duration_s, degraded)

    degraded=True if wav is not 16-bit PCM and we had to approximate.
    """
    import wave
    import struct

    degraded = False

    with wave.open(wav_path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        nframes = w.getnframes()
        dur = nframes / float(fr) if fr else 0.0

        win_n = int(fr * win_s)
        step_n = max(1, int(fr * step_s))
        if win_n <= 0 or fr <= 0:
            return [], dur, degraded

        rms = []
        idx = 0
        # read first channel only if stereo
        stride_bytes = sw * nch

        while idx + win_n <= nframes:
            w.setpos(idx)
            raw = w.readframes(win_n)
            if not raw:
                break

            if sw == 2:
                samples = struct.iter_unpack("<h", raw[0:len(raw):stride_bytes])
                s2 = 0
                count = 0
                for (s,) in samples:
                    s2 += s * s
                    count += 1
                val = math.sqrt(s2 / max(1, count))
                rms.append(val)
            else:
                # Non-16-bit: approximate by byte magnitude (degraded but deterministic)
                degraded = True
                b = raw[0:len(raw):stride_bytes]
                s2 = 0
                count = 0
                for bb in b:
                    s2 += int(bb) * int(bb)
                    count += 1
                val = math.sqrt(s2 / max(1, count))
                rms.append(val)

            idx += step_n

    return rms, dur, degraded

def load_asr_segments(asr_json_path: str) -> List[Tuple[float, float]]:
    """
    Returns list of (t0, t1) for dialogue segments. Empty list if none.
    """
    try:
        d = json.load(open(asr_json_path, "r", encoding="utf-8"))
    except Exception:
        return []
    segs = d.get("segments", [])
    out = []
    if isinstance(segs, list):
        for s in segs:
            if not isinstance(s, dict):
                continue
            t0 = s.get("t0", s.get("start", None))
            t1 = s.get("t1", s.get("end", None))
            if t0 is None or t1 is None:
                continue
            try:
                out.append((float(t0), float(t1)))
            except Exception:
                continue
    return out

def dialogue_coverage_per_window(dsegs: List[Tuple[float, float]], t0: float, t1: float) -> float:
    """
    Fraction of [t0,t1] covered by any dialogue segment.
    """
    if t1 <= t0:
        return 0.0
    covered = 0.0
    for a, b in dsegs:
        lo = max(t0, a)
        hi = min(t1, b)
        if hi > lo:
            covered += (hi - lo)
    return min(1.0, covered / (t1 - t0))

def load_motion_per_pair(motion_json_path: str) -> List[float]:
    """
    Returns list of mean_mag per pair. Empty list if none.
    """
    d = json.load(open(motion_json_path, "r", encoding="utf-8"))
    pp = d.get("per_pair", [])
    mags = []
    if isinstance(pp, list):
        for r in pp:
            if isinstance(r, dict) and "mean_mag" in r:
                try:
                    mags.append(float(r["mean_mag"]))
                except Exception:
                    mags.append(0.0)
    return mags

def get_video_fps(src_path: str) -> float:
    try:
        import cv2
        cap = cv2.VideoCapture(src_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        return float(fps) if fps and fps > 0 else 0.0
    except Exception:
        return 0.0

def bin_motion_to_windows(mags: List[float], fps: float, stride: int, win_s: float, step_s: float, nwin: int) -> List[float]:
    """
    Convert per-pair magnitudes to windowed averages aligned to audio windows.
    Each pair i corresponds roughly to time (i*stride)/fps.
    """
    if fps <= 0 or stride <= 0 or not mags or nwin <= 0:
        return [0.0] * max(0, nwin)

    pair_times = [(i * stride) / fps for i in range(len(mags))]

    out = []
    for w in range(nwin):
        t0 = w * step_s
        t1 = t0 + win_s
        vals = []
        for tm, mv in zip(pair_times, mags):
            if tm < t0:
                continue
            if tm >= t1:
                break
            vals.append(mv)
        out.append(sum(vals) / len(vals) if vals else 0.0)
    return out

def zscore(xs: List[float]) -> List[float]:
    if not xs:
        return []
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / max(1, len(xs))
    s = math.sqrt(v)
    if s <= 1e-9:
        return [0.0 for _ in xs]
    return [(x - m) / s for x in xs]

def detect_changepoints(X: List[List[float]], pen: float, min_size: int, use_ruptures: bool) -> List[int]:
    """
    Returns list of changepoint indices in window units, including final endpoint.
    If ruptures available and use_ruptures=True, uses PELT(rbf).
    Otherwise uses deterministic delta-threshold fallback.

    Returns a list that always starts with 0 and ends with n.
    """
    n = len(X)
    if n == 0:
        return [0]

    if use_ruptures:
        try:
            import ruptures as rpt
            algo = rpt.Pelt(model="rbf", min_size=min_size).fit(X)
            bkps = algo.predict(pen=pen)
            return [0] + bkps
        except Exception:
            pass

    deltas = [0.0]
    for i in range(1, n):
        a = X[i - 1]
        b = X[i]
        d2 = 0.0
        for aa, bb in zip(a, b):
            d2 += (bb - aa) * (bb - aa)
        deltas.append(math.sqrt(d2))

    ds = sorted(deltas[1:])
    if not ds:
        return [0, n]
    k = max(0, int(0.9 * (len(ds) - 1)))
    thr = ds[k]

    bkps = [0]
    last = 0
    for i in range(1, n):
        if deltas[i] >= thr and (i - last) >= min_size:
            bkps.append(i)
            last = i
    bkps.append(n)
    return bkps

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Absolute input media path")
    ap.add_argument("--mid", dest="mid", default="", help="Optional explicit media_id (64-hex). If empty, compute from bytes.")
    ap.add_argument("--out_dir", default="/workspace/catalog/segments", help="Output directory for segment artifacts")
    ap.add_argument("--win_s", type=float, default=0.5, help="Window size in seconds for envelopes")
    ap.add_argument("--step_s", type=float, default=0.5, help="Step size in seconds for envelopes")
    ap.add_argument("--motion_stride", type=int, default=2, help="Stride used in motion per_pair (must match B2)")
    ap.add_argument("--pen", type=float, default=6.0, help="Penalty for ruptures PELT (if available)")
    ap.add_argument("--min_seg_s", type=float, default=1.0, help="Minimum segment length in seconds")
    ap.add_argument("--no_ruptures", action="store_true", help="Disable ruptures even if installed")
    args = ap.parse_args()

    inp = os.path.abspath(args.inp)
    if not os.path.exists(inp):
        print(f"ERROR: input not found: {inp}", file=sys.stderr)
        sys.exit(2)

    mid = args.mid.strip()
    if not mid:
        mid = sha256_file(inp)

    out_path = os.path.join(args.out_dir, f"{mid}.json")

    created = utc_now_iso()
    inp_sha = sha256_file(inp)

    # Reference durations
    video_dur = ffprobe_duration_seconds(inp)

    # Locate Stage A artifacts if present
    asr_json = f"/workspace/catalog/asr/{mid}.json"
    wav_path = f"/workspace/catalog/asr/{mid}.wav"

    wav_used = wav_path if os.path.exists(wav_path) else ""
    extracted_wav_tmp = ""
    wav_extract_ok = False
    wav_extract_err = ""

    if not wav_used:
        tmpdir = tempfile.mkdtemp(prefix=f"segwav_{mid[:8]}_")
        extracted_wav_tmp = os.path.join(tmpdir, f"{mid}.wav")
        wav_extract_ok, wav_extract_err = run_ffmpeg_extract_wav(inp, extracted_wav_tmp)
        if wav_extract_ok and os.path.exists(extracted_wav_tmp):
            wav_used = extracted_wav_tmp

    # Audio RMS envelope
    rms = []
    audio_dur = 0.0
    audio_degraded = False
    wav_non_pcm_degraded = False

    if wav_used:
        rms, audio_dur, wav_non_pcm_degraded = read_wav_rms_envelope(wav_used, args.win_s, args.step_s)
        if rms and not os.path.exists(wav_path):
            # extracted temporary wav, not canonical
            audio_degraded = True
        if wav_non_pcm_degraded:
            audio_degraded = True

    # Choose reference duration
    duration_ref = audio_dur if audio_dur > 0 else (video_dur if video_dur > 0 else 0.0)

    # Dialogue coverage envelope
    dsegs = load_asr_segments(asr_json) if os.path.exists(asr_json) else []

    # Choose number of windows:
    # Prefer RMS windows if available, but ensure coverage up to duration_ref by allowing a final partial tail.
    if duration_ref <= 0:
        nwin = 0
    else:
        nwin = int(math.ceil(duration_ref / max(1e-9, args.step_s)))

    dialogue_cov = []
    for i in range(nwin):
        t0 = i * args.step_s
        t1 = min(duration_ref, t0 + args.win_s)
        dialogue_cov.append(dialogue_coverage_per_window(dsegs, t0, t1))

    # Motion envelope
    motion_path = f"/workspace/catalog/motion/{mid}.json"
    motion_mags = []
    motion_env = []
    fps = get_video_fps(inp)
    motion_degraded = False

    if os.path.exists(motion_path):
        try:
            motion_mags = load_motion_per_pair(motion_path)
        except Exception:
            motion_mags = []

    if motion_mags and nwin > 0 and fps > 0:
        motion_env = bin_motion_to_windows(motion_mags, fps, args.motion_stride, args.win_s, args.step_s, nwin)
    else:
        motion_env = [0.0] * max(0, nwin)
        if not os.path.exists(motion_path):
            motion_degraded = True

    # RMS series length may be shorter than nwin due to whole-window reads; pad deterministically
    if len(rms) < nwin:
        rms = rms + [rms[-1] if rms else 0.0] * (nwin - len(rms))
    else:
        rms = rms[:nwin]

    # Build multivariate feature series X
    rms_z = zscore(rms) if rms else [0.0] * max(0, nwin)
    mot_z = zscore(motion_env) if motion_env else [0.0] * max(0, nwin)
    dlg = dialogue_cov if dialogue_cov else [0.0] * max(0, nwin)

    X = [[r, m, d] for r, m, d in zip(rms_z, mot_z, dlg)]

    min_size = max(1, int(math.ceil(args.min_seg_s / max(1e-9, args.step_s))))
    bkps = detect_changepoints(X, pen=args.pen, min_size=min_size, use_ruptures=(not args.no_ruptures))

    # Convert bkps (window indices) to segments in seconds.
    # Ensure last endpoint reaches duration_ref exactly.
    segments = []
    for a, b in zip(bkps[:-1], bkps[1:]):
        t0 = a * args.step_s
        if b >= nwin:
            t1 = duration_ref
        else:
            t1 = b * args.step_s

        if t1 <= t0:
            continue
        if (t1 - t0) + 1e-9 < args.min_seg_s:
            continue

        segments.append({
            "t0_s": round(t0, 6),
            "t1_s": round(t1, 6),
            "t0_ms": int(round(t0 * 1000.0)),
            "t1_ms": int(round(t1 * 1000.0)),
            "kind": "changepoint",
        })

    # Outcome
    outcome = {
        "ok": True,
        "skipped": False,
        "blocked": False,
        "reason": "OK",
        "detail": ""
    }

    if nwin == 0:
        outcome["ok"] = True
        outcome["reason"] = "SIGNAL_TOO_SHORT"
        outcome["detail"] = "No windows available for segmentation."
    elif len(segments) == 0:
        outcome["ok"] = True
        outcome["reason"] = "NO_CHANGEPOINTS_DETECTED"
        outcome["detail"] = "No changepoints detected under current parameters."

    # Runtime versions
    runtime = {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "device": "cpu",
    }
    try:
        import cv2
        runtime["opencv_version"] = cv2.__version__
    except Exception:
        pass
    try:
        import numpy as np
        runtime["numpy_version"] = np.__version__
    except Exception:
        pass
    try:
        import ruptures as rpt
        runtime["ruptures_version"] = getattr(rpt, "__version__", "unknown")
    except Exception:
        runtime["ruptures_version"] = "(not installed)"

    config = {
        "win_s": args.win_s,
        "step_s": args.step_s,
        "motion_stride": args.motion_stride,
        "pen": args.pen,
        "min_seg_s": args.min_seg_s,
        "algo": "ruptures_pelt_rbf" if (runtime.get("ruptures_version") not in [None, "(not installed)"] and not args.no_ruptures) else "delta_threshold_fallback",
        "duration_ref": "audio" if audio_dur > 0 else ("video" if video_dur > 0 else "unknown"),
    }
    config_sha = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()

    tail_gap = 0.0
    if duration_ref > 0:
        approx_end = nwin * args.step_s
        tail_gap = max(0.0, duration_ref - approx_end)

    rec = {
        "schema_version": 1,
        "record_type": "segments",
        "media_id": mid,
        "created_utc": created,
        "input": {
            "path": inp,
            "sha256": inp_sha,
        },
        "inputs": {
            "asr_json": asr_json if os.path.exists(asr_json) else "",
            "wav": wav_path if os.path.exists(wav_path) else (wav_used or ""),
            "motion_json": motion_path if os.path.exists(motion_path) else "",
        },
        "config": config,
        "config_sha256": config_sha,
        "runtime": runtime,
        "outcome": outcome,
        "segments": segments,
        "stats": {
            "duration_audio_s": round(audio_dur, 6),
            "duration_video_s": round(video_dur, 6),
            "duration_ref_s": round(duration_ref, 6),
            "duration_ref_kind": config["duration_ref"],
            "windows": nwin,
            "window_win_s": args.win_s,
            "window_step_s": args.step_s,
            "tail_gap_s": round(tail_gap, 6),
            "dialogue_segments": len(dsegs),
            "motion_pairs": len(motion_mags),
            "fps": fps,
            "audio_degraded": audio_degraded,
            "motion_degraded": motion_degraded,
            "wav_extracted_tmp": bool(extracted_wav_tmp),
        }
    }

    if extracted_wav_tmp and not wav_extract_ok:
        rec["stats"]["wav_extract_error"] = wav_extract_err[:5000]

    atomic_write_json(out_path, rec)

    if extracted_wav_tmp:
        try:
            shutil.rmtree(os.path.dirname(extracted_wav_tmp), ignore_errors=True)
        except Exception:
            pass

    print(f"WROTE: {out_path}")
    print(f"media_id: {mid} segments: {len(segments)} windows: {nwin} reason: {outcome['reason']} duration_ref_s: {duration_ref:.6f}")

if __name__ == "__main__":
    main()
    