#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path


def die(msg: str, code: int = 2) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_json(path: Path, ctx: str):
    if not path.is_file():
        die(f"missing {ctx}: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"failed to parse {ctx} json {path}: {e}")


def must_exist(path: Path, ctx: str):
    if not path.is_file():
        die(f"missing {ctx}: {path}")


def must_nonempty(path: Path, ctx: str):
    must_exist(path, ctx)
    try:
        if path.stat().st_size <= 0:
            die(f"empty {ctx}: {path}")
    except Exception as e:
        die(f"cannot stat {ctx}: {path}: {e}")


def is_no_audio_track_asr(a: dict) -> bool:
    """
    True iff ASR explicitly indicates there is no audio track / no audio stream.
    This is a valid success case where wav/srt/vtt cannot exist and must not be required.
    """
    if not isinstance(a, dict):
        return False

    if a.get("empty_transcript") is not True:
        return False

    reason = a.get("reason")
    reason_raw = a.get("reason_raw")

    if reason == "NO_AUDIO_TRACK":
        return True
    if reason_raw == "no_audio_stream":
        return True

    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Absolute input media path")
    ap.add_argument("--mid", required=True, help="64-hex sha256 media_id")
    ap.add_argument("--catalog-root", default="/workspace/catalog")
    args = ap.parse_args()

    IN = Path(args.inp)
    if not IN.is_absolute():
        die("--in must be absolute")
    if not IN.is_file():
        die(f"input missing: {IN}")

    mid = args.mid.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", mid):
        die(f"--mid must be 64 lowercase hex sha256, got: {mid!r}")

    root = Path(args.catalog_root)
    asr_dir = root / "asr"
    manifest_dir = root / "media_manifest"
    status_dir = root / "status"

    wav = asr_dir / f"{mid}.wav"
    srt = asr_dir / f"{mid}.srt"
    vtt = asr_dir / f"{mid}.vtt"
    asr_json = asr_dir / f"{mid}.json"
    qc_json = asr_dir / f"{mid}.qc.json"
    manifest_json = manifest_dir / f"{mid}.json"
    status_json = status_dir / f"{mid}.json"

    # Always-required artifacts
    must_nonempty(manifest_json, "manifest_json")
    must_nonempty(asr_json, "asr_json")
    must_nonempty(qc_json, "asr_qc_json")
    must_nonempty(status_json, "status_json")

    m = load_json(manifest_json, "manifest")
    if m.get("media_id") != mid:
        die("manifest media_id mismatch")
    if m.get("source_path") != str(IN):
        die(f"manifest source_path mismatch: expected {IN}, got {m.get('source_path')}")

    a = load_json(asr_json, "asr")

    # Canonical ASR JSON schema lock
    sv = a.get("schema_version", None)
    if sv != 2:
        die(f"asr schema_version must be 2, got: {sv!r}")

    if a.get("media_id") != mid:
        die("asr media_id mismatch")

    segs = a.get("segments") or []
    if not isinstance(segs, list):
        die("asr.segments must be a list")

    empty_flag = a.get("empty_transcript")
    if empty_flag not in (True, False):
        die("asr.empty_transcript must be boolean")

    if (len(segs) == 0) != bool(empty_flag):
        die(f"asr empty_transcript inconsistency: segments={len(segs)} empty_transcript={empty_flag}")

    # Sidecar requirements:
    # - Normally, WAV/SRT/VTT must exist and be non-empty.
    # - If ASR explicitly indicates NO_AUDIO_TRACK/no_audio_stream, these are not required.
    no_audio_track = is_no_audio_track_asr(a)
    if not no_audio_track:
        must_nonempty(wav, "wav")
        must_nonempty(srt, "srt")
        must_nonempty(vtt, "vtt")

    # No BLANK_AUDIO inside machine segments
    for i, seg in enumerate(segs):
        t = (seg.get("text") or "")
        if "[BLANK_AUDIO]" in t:
            die(f"asr segment contains [BLANK_AUDIO] at index {i}")

        # Basic timestamp sanity
        st = seg.get("start")
        en = seg.get("end")
        if st is None or en is None:
            die(f"asr segment missing start/end at index {i}")
        try:
            stf = float(st)
            enf = float(en)
        except Exception:
            die(f"asr segment start/end not numeric at index {i}: start={st} end={en}")
        if stf < 0 or enf < 0 or enf < stf:
            die(f"asr segment timestamps invalid at index {i}: start={stf} end={enf}")

    q = load_json(qc_json, "qc")
    if q.get("media_id") != mid:
        die("qc media_id mismatch")
    for k in ["vad_used", "vad_fallback", "asr_pass1_rc", "asr_pass2_rc"]:
        if k not in q:
            die(f"qc missing key: {k}")

    s = load_json(status_json, "status")
    if s.get("media_id") != mid:
        die("status media_id mismatch")
    stages = s.get("stages") or {}
    if not isinstance(stages, dict):
        die("status.stages must be dict")
    if "stageA" not in stages and "asr" not in stages:
        die("status missing stageA/asr stage entries")

    if no_audio_track:
        print("OK: stageA_contract_check passed (no audio track; wav/srt/vtt not required)")
    else:
        print("OK: stageA_contract_check passed")


if __name__ == "__main__":
    main()