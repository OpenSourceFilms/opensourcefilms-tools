#!/usr/bin/env python3
"""
Artifact-derived audit index for the Media Intelligence Pipeline.

Contracts:
- Artifacts are the only source of truth.
- Status JSON is optional metadata and is ignored here.
- Writes a JSONL index, one record per media_id.
- Optionally runs authoritative contract checkers.

Important:
- Different checkers may require different Python environments.
  This script allows explicitly selecting the interpreter used for each checker.

Additions:
- --summary produces machine-readable JSON (to stdout) describing:
  - record counts
  - artifact presence counts
  - contract checker pass/fail/not-ran counts
  - readiness tiers for downstream consumers (LLM, Milvus later, editorial tools)
- Per-record provenance is included for key toolchain identity.
- Per-run provenance is emitted to /workspace/catalog/audit/provenance_<UTC>.json
"""

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

MID_RE = re.compile(r"^[0-9a-f]{64}$")


# Pinned ASR provenance (verified in this environment)
ASR_PROVENANCE = {
    "engine": "whisper.cpp",
    "whispercpp_repo_commit": "e9898ddfb908ffaa7026c66852a023889a5a7202",
    "whisper_cli_path": "/workspace/pipeline/third_party/whisper.cpp/build/bin/whisper-cli",
    "whisper_cli_sha256": "8c236b1dca161b3e3ac2436f7acd3dcb31607d555638d9196f277c91e392d3f3",
    "model_path": "/workspace/pipeline/models/whispercpp/ggml-small.en.bin",
    "model_sha256": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
    "vad_model_path": "/workspace/pipeline/third_party/whisper.cpp/models/for-tests-silero-v6.2.0-ggml.bin",
    "vad_model_sha256": "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
    "defaults": {
        "enable_vad_default": 1,
        "whisper_threads_default": 4,
        "vad": {
            "threshold": 0.50,
            "min_speech_ms": 250,
            "min_silence_ms": 100,
            "speech_pad_ms": 30,
            "overlap_s": 0.10,
        },
        "decoder_safety": {
            "no_fallback_default": 1,
            "entropy_thold": 2.40,
            "logprob_thold": -1.00,
            "no_speech_thold": 0.60,
        },
        "outputs": ["srt", "vtt"],
    },
}


@dataclass
class Paths:
    catalog: Path
    pipeline_bin: Path

    media_manifest: Path
    asr_dir: Path
    chunks_dir: Path
    embeddings_dir: Path
    enrich_dir: Path
    keyframes_dir: Path


def fail(msg: str) -> None:
    print("FAIL:", msg, file=sys.stderr)
    raise SystemExit(2)


def is_mid(s: str) -> bool:
    return bool(MID_RE.match(s))


def load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_mids_from_dir(dir_path: Path, suffix: str) -> Set[str]:
    mids: Set[str] = set()
    if not dir_path.exists():
        return mids
    for p in dir_path.glob(f"*{suffix}"):
        name = p.name
        if name.endswith(suffix):
            mid = name[: -len(suffix)]
            if is_mid(mid):
                mids.add(mid)
    return mids


def list_mids_from_keyframes_dir(keyframes_root: Path) -> Set[str]:
    mids: Set[str] = set()
    if not keyframes_root.exists():
        return mids
    for p in keyframes_root.iterdir():
        if p.is_dir() and is_mid(p.name):
            mids.add(p.name)
    return mids


def all_media_ids(paths: Paths) -> Set[str]:
    mids: Set[str] = set()
    mids |= list_mids_from_dir(paths.media_manifest, ".json")
    mids |= list_mids_from_dir(paths.asr_dir, ".json")        # canonical ASR json
    mids |= list_mids_from_dir(paths.embeddings_dir, ".json")
    mids |= list_mids_from_dir(paths.enrich_dir, ".json")
    mids |= list_mids_from_dir(paths.chunks_dir, ".jsonl")    # chunk jsonl
    mids |= list_mids_from_keyframes_dir(paths.keyframes_dir)
    return mids


def file_info(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"exists": False}
    try:
        st = path.stat()
        return {
            "exists": True,
            "bytes": int(st.st_size),
            "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        }
    except Exception:
        return {"exists": True}


def dir_info(path: Path) -> Dict[str, object]:
    if not path.exists() or not path.is_dir():
        return {"exists": False}
    return {"exists": True}


def run_checker(cmd: List[str], timeout_s: int = 1200) -> Tuple[bool, str]:
    """
    Returns (ok, summary).
    summary is truncated combined stdout/stderr for diagnostics.
    """
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        out = (p.stdout or "").strip()
        if len(out) > 4000:
            out = out[:4000] + "\n...[truncated]..."
        ok = p.returncode == 0
        return ok, f"cmd={' '.join(cmd)}\nrc={p.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return False, f"cmd={' '.join(cmd)}\ntimeout after {timeout_s}s"
    except Exception as e:
        return False, f"cmd={' '.join(cmd)}\nexception: {e!r}"


def pick_default_embed_python(pipeline_root: Path) -> str:
    cand = pipeline_root / "venv_embed_cpu" / "bin" / "python"
    if cand.exists():
        return str(cand)
    return sys.executable


def extract_embedding_provenance(embed_path: Path) -> Optional[Dict[str, Any]]:
    """
    Best-effort extraction of embedding provenance from the embeddings artifact.
    Returns None if missing/unreadable.
    """
    if not embed_path.exists():
        return None
    emb = load_json(embed_path)
    if not isinstance(emb, dict):
        return None

    model = emb.get("model", {}) if isinstance(emb.get("model", {}), dict) else {}
    cfg = emb.get("embed_config", {}) if isinstance(emb.get("embed_config", {}), dict) else {}
    rt = emb.get("runtime", {}) if isinstance(emb.get("runtime", {}), dict) else {}

    out: Dict[str, Any] = {
        "schema_version": emb.get("schema_version"),
        "model_id": model.get("model_id"),
        "model_revision": model.get("model_revision"),
        "dimension": model.get("dimension"),
        "normalize_embeddings": cfg.get("normalize_embeddings"),
        "embed_config_sha256": emb.get("embed_config_sha256"),
        "runtime": {
            "python_version": rt.get("python_version"),
            "torch_version": rt.get("torch_version"),
            "sentence_transformers_version": rt.get("sentence_transformers_version"),
            "transformers_version": rt.get("transformers_version"),
            "huggingface_hub_version": rt.get("huggingface_hub_version"),
            "device": rt.get("device"),
            "venv": rt.get("venv"),
        },
    }
    return out


def build_record(
    mid: str,
    paths: Paths,
    run_stageA_check: bool,
    run_stageB1_check: bool,
    py_stageA: str,
    py_stageB1: str,
) -> Dict[str, object]:
    manifest_path = paths.media_manifest / f"{mid}.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else None

    source_path = None
    if isinstance(manifest, dict):
        source_path = manifest.get("source_path")

    asr_json = paths.asr_dir / f"{mid}.json"
    asr_qc_json = paths.asr_dir / f"{mid}.qc.json"
    wav = paths.asr_dir / f"{mid}.wav"
    srt = paths.asr_dir / f"{mid}.srt"
    vtt = paths.asr_dir / f"{mid}.vtt"

    chunks_jsonl = paths.chunks_dir / f"{mid}.jsonl"
    embed_json = paths.embeddings_dir / f"{mid}.json"
    enrich_json = paths.enrich_dir / f"{mid}.json"
    kf_dir = paths.keyframes_dir / mid

    rec: Dict[str, object] = {
        "schema_version": 1,
        "media_id": mid,
        "source_path": source_path,
        "paths": {
            "manifest_json": str(manifest_path),
            "asr_json": str(asr_json),
            "asr_qc_json": str(asr_qc_json),
            "wav": str(wav),
            "srt": str(srt),
            "vtt": str(vtt),
            "chunks_jsonl": str(chunks_jsonl),
            "embeddings_json": str(embed_json),
            "enrich_json": str(enrich_json),
            "keyframes_dir": str(kf_dir),
        },
        "exists": {
            "manifest": file_info(manifest_path),
            "asr_json": file_info(asr_json),
            "asr_qc_json": file_info(asr_qc_json),
            "wav": file_info(wav),
            "srt": file_info(srt),
            "vtt": file_info(vtt),
            "chunks_jsonl": file_info(chunks_jsonl),
            "embeddings_json": file_info(embed_json),
            "enrich_json": file_info(enrich_json),
            "keyframes_dir": dir_info(kf_dir),
        },
        "provenance": {
            "asr": ASR_PROVENANCE,
            "embedding": extract_embedding_provenance(embed_json),
        },
        "checks": {
            "stageA_contract_check": {"ran": False},
            "stageB1_contract_check": {"ran": False},
        },
    }

    # StageA contract check (authoritative), only if we can supply --in
    if run_stageA_check:
        checker = paths.pipeline_bin / "stageA_contract_check.py"
        if not checker.exists():
            rec["checks"]["stageA_contract_check"] = {
                "ran": False,
                "ok": False,
                "python": py_stageA,
                "summary": "missing stageA_contract_check.py",
            }
        else:
            if source_path and isinstance(source_path, str) and Path(source_path).exists():
                ok, summary = run_checker([py_stageA, str(checker), "--in", source_path, "--mid", mid])
                rec["checks"]["stageA_contract_check"] = {
                    "ran": True,
                    "ok": ok,
                    "python": py_stageA,
                    "summary": summary,
                }
            else:
                rec["checks"]["stageA_contract_check"] = {
                    "ran": False,
                    "ok": False,
                    "python": py_stageA,
                    "summary": "no valid source_path for --in (manifest missing or file absent)",
                }

    # StageB1 contract check (authoritative)
    if run_stageB1_check:
        checker = paths.pipeline_bin / "stageB1_contract_check.py"
        if not checker.exists():
            rec["checks"]["stageB1_contract_check"] = {
                "ran": False,
                "ok": False,
                "python": py_stageB1,
                "summary": "missing stageB1_contract_check.py",
            }
        else:
            ok, summary = run_checker([py_stageB1, str(checker), "--mid", mid])
            rec["checks"]["stageB1_contract_check"] = {
                "ran": True,
                "ok": ok,
                "python": py_stageB1,
                "summary": summary,
            }

    return rec


def write_jsonl(out_path: Path, records: Iterable[Dict[str, object]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            yield json.loads(s)


def bool_get(d: Dict[str, Any], path: List[str], default: bool = False) -> bool:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return bool(cur)


def get_check_state(rec: Dict[str, Any], check_key: str) -> Tuple[bool, Optional[bool]]:
    """
    Returns (ran, ok_or_none).
    If not ran, ok_or_none is None.
    """
    chk = rec.get("checks", {}).get(check_key, {})
    ran = bool(chk.get("ran", False))
    if not ran:
        return False, None
    return True, bool(chk.get("ok", False))


def build_summary(index_path: Path) -> Dict[str, Any]:
    records = 0

    present = {
        "manifest": 0,
        "asr_json": 0,
        "wav": 0,
        "srt": 0,
        "vtt": 0,
        "chunks": 0,
        "embeddings": 0,
        "enrich": 0,
        "keyframes_dir": 0,
    }

    checks = {
        "stageA": {"ok": 0, "fail": 0, "not_ran": 0},
        "stageB1": {"ok": 0, "fail": 0, "not_ran": 0},
    }

    # Readiness tiers for downstream consumers.
    # stageA_ready: manifest + asr_json + wav + srt + vtt
    # semantic_ready: stageA_ready + chunks + embeddings + stageB1_check_ok
    # editorial_ready: semantic_ready + enrich + keyframes_dir
    tiers_count = {"stageA_ready": 0, "semantic_ready": 0, "editorial_ready": 0}
    tiers_mids: Dict[str, List[str]] = {"stageA_ready": [], "semantic_ready": [], "editorial_ready": []}

    # Legacy completeness (kept for backward compatibility with earlier summary consumers):
    # complete: manifest + asr_json + chunks + embeddings + enrich + keyframes_dir
    # partial: otherwise
    complete_mids = 0
    partial_mids = 0

    for rec in iter_jsonl(index_path):
        records += 1
        mid = str(rec.get("media_id", ""))

        ex_manifest = bool_get(rec, ["exists", "manifest", "exists"])
        ex_asr_json = bool_get(rec, ["exists", "asr_json", "exists"])
        ex_wav = bool_get(rec, ["exists", "wav", "exists"])
        ex_srt = bool_get(rec, ["exists", "srt", "exists"])
        ex_vtt = bool_get(rec, ["exists", "vtt", "exists"])
        ex_chunks = bool_get(rec, ["exists", "chunks_jsonl", "exists"])
        ex_embeddings = bool_get(rec, ["exists", "embeddings_json", "exists"])
        ex_enrich = bool_get(rec, ["exists", "enrich_json", "exists"])
        ex_keyframes = bool_get(rec, ["exists", "keyframes_dir", "exists"])

        if ex_manifest:
            present["manifest"] += 1
        if ex_asr_json:
            present["asr_json"] += 1
        if ex_wav:
            present["wav"] += 1
        if ex_srt:
            present["srt"] += 1
        if ex_vtt:
            present["vtt"] += 1
        if ex_chunks:
            present["chunks"] += 1
        if ex_embeddings:
            present["embeddings"] += 1
        if ex_enrich:
            present["enrich"] += 1
        if ex_keyframes:
            present["keyframes_dir"] += 1

        ran, ok = get_check_state(rec, "stageA_contract_check")
        if not ran:
            checks["stageA"]["not_ran"] += 1
        elif ok:
            checks["stageA"]["ok"] += 1
        else:
            checks["stageA"]["fail"] += 1

        ran_b1, ok_b1 = get_check_state(rec, "stageB1_contract_check")
        if not ran_b1:
            checks["stageB1"]["not_ran"] += 1
        elif ok_b1:
            checks["stageB1"]["ok"] += 1
        else:
            checks["stageB1"]["fail"] += 1

        stageA_ready = ex_manifest and ex_asr_json and ex_wav and ex_srt and ex_vtt
        stageB1_ok = bool(ran_b1 and ok_b1)
        semantic_ready = stageA_ready and ex_chunks and ex_embeddings and stageB1_ok
        editorial_ready = semantic_ready and ex_enrich and ex_keyframes

        if stageA_ready:
            tiers_count["stageA_ready"] += 1
            if mid:
                tiers_mids["stageA_ready"].append(mid)
        if semantic_ready:
            tiers_count["semantic_ready"] += 1
            if mid:
                tiers_mids["semantic_ready"].append(mid)
        if editorial_ready:
            tiers_count["editorial_ready"] += 1
            if mid:
                tiers_mids["editorial_ready"].append(mid)

        is_complete = ex_manifest and ex_asr_json and ex_chunks and ex_embeddings and ex_enrich and ex_keyframes
        if is_complete:
            complete_mids += 1
        else:
            partial_mids += 1

    missing = {k: records - present[k] for k in present.keys()}

    return {
        "schema_version": 2,
        "index_path": str(index_path),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "records": records,
        "present": present,
        "missing": missing,
        "checks": checks,
        "tiers": {
            "counts": tiers_count,
            "media_ids": tiers_mids,
        },
        "media_ids": {
            "complete": complete_mids,
            "partial": partial_mids,
        },
    }


def write_run_provenance(audit_dir: Path, out_index_path: Path, py_stageA: str, py_stageB1: str) -> None:
    """
    Write a small, per-run provenance file so drift can be detected without
    reading every per-media record.
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_path = audit_dir / f"provenance_{ts}.json"
    latest_path = audit_dir / "provenance_latest.json"

    payload = {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_written": str(out_index_path),
        "py_stageA": py_stageA,
        "py_stageB1": py_stageB1,
        "asr": ASR_PROVENANCE,
    }

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        latest_path.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog-root", default="/workspace/catalog", help="Catalog root, default /workspace/catalog")
    ap.add_argument("--pipeline-bin", default="/workspace/pipeline/bin", help="Pipeline bin dir, default /workspace/pipeline/bin")
    ap.add_argument("--out", default="", help="Output JSONL path. If empty, writes to /workspace/catalog/audit/index_<UTC>.jsonl")
    ap.add_argument("--run-stageA-check", action="store_true", help="Run stageA_contract_check where possible (requires manifest source_path exists)")
    ap.add_argument("--run-stageB1-check", action="store_true", help="Run stageB1_contract_check")
    ap.add_argument("--py-stageA", default="", help="Python interpreter for stageA_contract_check (default: current python)")
    ap.add_argument("--py-stageB1", default="", help="Python interpreter for stageB1_contract_check (default: /workspace/pipeline/venv_embed_cpu/bin/python if present)")
    ap.add_argument("--summary", action="store_true", help="Print JSON summary of the latest audit index and exit")
    ap.add_argument("--summary-path", default="", help="Summarise a specific JSONL index file (default: catalog/audit/index_latest.jsonl)")
    args = ap.parse_args()

    catalog = Path(args.catalog_root)
    pipeline_bin = Path(args.pipeline_bin)
    pipeline_root = pipeline_bin.parent

    audit_dir = catalog / "audit"
    latest_path = audit_dir / "index_latest.jsonl"

    if args.summary:
        idx_path = Path(args.summary_path) if args.summary_path.strip() else latest_path
        if not idx_path.exists():
            fail(f"summary requested but index file does not exist: {idx_path}")
        summary = build_summary(idx_path)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    py_stageA = args.py_stageA.strip() or sys.executable
    py_stageB1 = args.py_stageB1.strip() or pick_default_embed_python(pipeline_root)

    paths = Paths(
        catalog=catalog,
        pipeline_bin=pipeline_bin,
        media_manifest=catalog / "media_manifest",
        asr_dir=catalog / "asr",
        chunks_dir=catalog / "chunks",
        embeddings_dir=catalog / "embeddings",
        enrich_dir=catalog / "enrich",
        keyframes_dir=catalog / "keyframes",
    )

    mids = sorted(all_media_ids(paths))
    if not mids:
        print("WARN: no media_ids discovered from catalog")

    records = [
        build_record(
            mid,
            paths,
            args.run_stageA_check,
            args.run_stageB1_check,
            py_stageA=py_stageA,
            py_stageB1=py_stageB1,
        )
        for mid in mids
    ]

    if args.out:
        out_path = Path(args.out)
    else:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out_path = audit_dir / f"index_{ts}.jsonl"

    write_jsonl(out_path, records)
    print(f"OK: wrote {out_path} records={len(records)}")
    print(f"OK: py_stageA={py_stageA}")
    print(f"OK: py_stageB1={py_stageB1}")

    # Stable latest file (no symlink assumptions)
    try:
        latest_path.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"OK: wrote {latest_path}")
    except Exception as e:
        print(f"WARN: failed to write index_latest.jsonl: {e!r}")

    # Per-run provenance (cheap drift detector)
    try:
        write_run_provenance(audit_dir=audit_dir, out_index_path=out_path, py_stageA=py_stageA, py_stageB1=py_stageB1)
        print(f"OK: wrote audit provenance (latest at {audit_dir / 'provenance_latest.json'})")
    except Exception as e:
        print(f"WARN: failed to write audit provenance: {e!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
