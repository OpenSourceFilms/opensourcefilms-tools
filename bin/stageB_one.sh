#!/usr/bin/env bash
# /workspace/pipeline/bin/stageB_keyframes_one.sh
#
# Stage B (keyframes v1): extract keyframes at fixed FPS and write enrich JSON.
#
# Constraints:
# - Artifact-driven: requires Stage A manifest JSON (status JSON is optional metadata only).
# - Logging: stdout/stderr only. Do NOT write stage-specific log files, do NOT tee.
# - Idempotent for a given MEDIA_ID: clears prior frame_*.jpg and rewrites enrich JSON.

set -uo pipefail   # deliberately NO -e

source /workspace/pipeline/bin/lib_safe.sh
source /workspace/pipeline/bin/lib_paths.sh

log_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log_info() { printf '%s %s\n' "stageB|INFO|$(log_ts)" "$*" >&2; }
log_warn() { printf '%s %s\n' "stageB|WARN|$(log_ts)" "$*" >&2; }
log_err()  { printf '%s %s\n' "stageB|ERROR|$(log_ts)" "$*" >&2; }

fatal() { log_err "FATAL|$*"; exit 2; }

require_var MEDIA_ID

# Ensure Python (and subprocesses) see these values
export MEDIA_ID

KF_FPS="${KF_FPS:-1}"   # default 1 fps
export KF_FPS

MANIFEST_JSON="${MANIFEST_DIR}/${MEDIA_ID}.json"
STATUS_JSON="${STATUS_DIR}/${MEDIA_ID}.json"   # optional only

ENRICH_DIR="${ENRICH_DIR:-/workspace/catalog/enrich}"
OUT_JSON="${ENRICH_DIR}/${MEDIA_ID}.json"

KF_DIR="${KF_DIR:-/workspace/catalog/keyframes/${MEDIA_ID}}"

mkdir -p "$ENRICH_DIR" "$KF_DIR" || fatal "failed to create output dirs"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${RUN_ID:-$TS}"

log_info "start|RUN_ID=${RUN_ID}|MEDIA_ID=${MEDIA_ID}|KF_FPS=${KF_FPS}"
log_info "paths|MANIFEST_JSON=${MANIFEST_JSON}|STATUS_JSON=${STATUS_JSON}|KF_DIR=${KF_DIR}|OUT_JSON=${OUT_JSON}"

# Stage B is artifact-driven. It requires the manifest, not status.
if [[ ! -f "$MANIFEST_JSON" ]]; then
  log_warn "skip|reason=missing_manifest|path=${MANIFEST_JSON}"
  exit 0
fi

# Status is optional debug metadata, never a gate.
if [[ ! -f "$STATUS_JSON" ]]; then
  log_warn "note|missing_status|path=${STATUS_JSON}"
fi

# Read source_path from manifest
SRC_PATH="$(
  python3 - "$MANIFEST_JSON" <<'PY'
import json, sys
p = sys.argv[1]
with open(p, "r", encoding="utf-8") as f:
  d = json.load(f)
print(d.get("source_path","") or "")
PY
)"
if [[ -z "${SRC_PATH:-}" ]]; then
  log_warn "skip|reason=manifest_missing_source_path|manifest=${MANIFEST_JSON}"
  exit 0
fi
if [[ ! -f "$SRC_PATH" ]]; then
  log_warn "skip|reason=source_not_found|source_path=${SRC_PATH}"
  exit 0
fi
log_info "source|SRC_PATH=${SRC_PATH}"

# Mark Stage B start (optional metadata). If this fails, continue.
run_step status_stageB_start \
  /workspace/pipeline/bin/status_write.py \
  "$STATUS_JSON" "$MEDIA_ID" stageB 1 \
  "run_id=${RUN_ID}" \
  "out_json=${OUT_JSON}" \
  "keyframes_dir=${KF_DIR}" \
  >/dev/null 2>&1 || true

# Extract keyframes at fixed FPS (deterministic)
log_info "extract|begin|fps=${KF_FPS}"
rm -f "$KF_DIR"/frame_*.jpg 2>/dev/null || true

ffmpeg -nostdin -hide_banner -loglevel error -y \
  -i "$SRC_PATH" \
  -vf "fps=${KF_FPS}" \
  -q:v 3 \
  "$KF_DIR/frame_%06d.jpg"
rc=$?
if [[ "$rc" -ne 0 ]]; then
  fatal "extract_keyframes_failed|rc=${rc}"
fi
log_info "extract|end|rc=0"

# Build enrich JSON referencing frames
python3 - "$OUT_JSON" <<'PY'
import os, json, glob, sys

out_json = sys.argv[1]
media_id = os.environ["MEDIA_ID"]
kf_dir = f"/workspace/catalog/keyframes/{media_id}"
frames = sorted(glob.glob(os.path.join(kf_dir, "frame_*.jpg")))

out = {
  "schema_version": 1,
  "record_type": "stageB_enrich_keyframes",
  "media_id": media_id,
  "created_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
  "stage": "B",
  "status": "ok",
  "method": {
    "keyframes": {
      "type": "fps",
      "fps": float(os.environ.get("KF_FPS","1")),
      "pattern": "frame_%06d.jpg",
      "dir": kf_dir,
      "count": len(frames),
    }
  },
  "frames": [{"path": p, "index": i+1} for i, p in enumerate(frames)],
}

with open(out_json, "w", encoding="utf-8") as f:
  json.dump(out, f, ensure_ascii=False, indent=2)
  f.write("\n")

print(f"stageB|INFO|wrote_enrich_json {out_json} frames={len(frames)}")
PY

frames_count="$(ls -1 "$KF_DIR"/frame_*.jpg 2>/dev/null | wc -l | tr -d ' ')"
log_info "ok|wrote_enrich_json=${OUT_JSON}|frames=${frames_count}"

# Mark Stage B done (optional metadata). If this fails, continue.
run_step status_stageB_done \
  /workspace/pipeline/bin/status_write.py \
  "$STATUS_JSON" "$MEDIA_ID" stageB 1 \
  "ok=true" \
  "run_id=${RUN_ID}" \
  "enrich_json=${OUT_JSON}" \
  "keyframes_dir=${KF_DIR}" \
  >/dev/null 2>&1 || true

log_info "done|MEDIA_ID=${MEDIA_ID}"
exit 0