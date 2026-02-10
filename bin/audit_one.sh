#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<EOF >&2
Usage:
  audit_one.sh --in <absolute_media_path> --run
  audit_one.sh --in <absolute_media_path> --snap

Behavior:
  --run  : runs Stage A then Stage B, then snapshots hashes, then diffs vs previous (if any)
  --snap : snapshots hashes only (no stage rerun), then diffs vs previous (if any)

Outputs:
  Writes /workspace/catalog/audit/<MID>_<RUN_ID>.sha256
EOF
  exit 2
}

IN=""
MODE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --in) IN="${2:-}"; shift 2 ;;
    --run) MODE="run"; shift 1 ;;
    --snap) MODE="snap"; shift 1 ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

if [ -z "${IN:-}" ] || [ -z "${MODE:-}" ]; then
  usage
fi

if [ ! -f "$IN" ]; then
  echo "FATAL: input not found: $IN" >&2
  exit 3
fi

echo "=== AUDIT_ONE START ==="
date -u +"%Y-%m-%dT%H:%M:%SZ"
echo "MODE=$MODE"
echo "IN=$IN"
echo

export IN

if [ "$MODE" = "run" ]; then
  echo "=== STAGE A ==="
  /workspace/pipeline/bin/stageA_one.sh
  echo
fi

echo "=== RESOLVE MEDIA_ID from manifest by source_path ==="
MID="$(jq -r --arg p "$IN" 'select(.source_path==$p) | .media_id' /workspace/catalog/media_manifest/*.json | grep -v "^null$" | tail -n 1 || true)"
echo "Resolved MID raw: [$MID]"

if [ -z "${MID:-}" ] || [ "$MID" = "null" ]; then
  echo "FATAL: could not resolve MID from manifests for source_path=$IN" >&2
  echo "Candidates:" >&2
  jq -r '.source_path + " -> " + (.media_id|tostring)' /workspace/catalog/media_manifest/*.json | grep -F "$IN" >&2 || true
  exit 4
fi

if printf "%s" "$MID" | grep -q "[[:space:]]"; then
  echo "FATAL: MID contains whitespace/newlines, refusing: [$MID]" >&2
  exit 5
fi

export MEDIA_ID="$MID"
echo "OK: MEDIA_ID=$MEDIA_ID"
echo

if [ "$MODE" = "run" ]; then
  echo "=== STAGE B ==="
  /workspace/pipeline/bin/stageB_one.sh
  echo
fi

echo "=== VERIFY REQUIRED ARTIFACTS EXIST ==="
req=(
  "/workspace/catalog/media_manifest/${MID}.json"
  "/workspace/catalog/asr/${MID}.wav"
  "/workspace/catalog/asr/${MID}.json"
  "/workspace/catalog/asr/${MID}.srt"
  "/workspace/catalog/asr/${MID}.vtt"
  "/workspace/catalog/enrich/${MID}.json"
)
for f in "${req[@]}"; do
  test -f "$f" || { echo "FATAL: missing file: $f" >&2; exit 6; }
done
test -d "/workspace/catalog/keyframes/${MID}" || { echo "FATAL: missing dir: /workspace/catalog/keyframes/${MID}" >&2; exit 7; }
ls -1 "/workspace/catalog/keyframes/${MID}"/frame_*.jpg >/dev/null || { echo "FATAL: no keyframes present for ${MID}" >&2; exit 8; }
echo "OK: required artifacts present"
echo "=== CONTRACT CHECK ==="
KF_FPS_VAL="${KF_FPS:-1}"
python3 /workspace/pipeline/bin/contract_check.py --in "$IN" --mid "$MID" --kf-fps "$KF_FPS_VAL"
echo
echo

echo "=== WRITE CANONICAL SHA256 SET (sorted) ==="

write_toolchain_snapshot() {
  local ts="$1"
  local out="/workspace/catalog/audit/${MID}_${ts}.toolchain.txt"
  {
    echo "=== TIME / HOST ==="
    date -u +"%Y-%m-%dT%H:%M:%SZ"
    uname -a
    echo
    echo "=== SHELL ==="
    bash --version | head -n 1
    echo "PATH=${PATH}"
    echo
    echo "=== VERSIONS ==="
    python3 --version || true
    ffmpeg -version | head -n 2 || true
    ffprobe -version | head -n 2 || true
    jq --version || true
    echo
    echo "=== WHISPER.CPP ==="
    ls -l /workspace/pipeline/third_party/whisper.cpp/build/bin/whisper-cli || true
    ls -l /workspace/pipeline/models/whispercpp/ggml-small.en.bin || true
    sha256sum /workspace/pipeline/third_party/whisper.cpp/build/bin/whisper-cli /workspace/pipeline/models/whispercpp/ggml-small.en.bin 2>/dev/null || true
    echo
    echo "=== PIPELINE SCRIPT HASHES ==="
    sha256sum \
      /workspace/pipeline/bin/stageA_one.sh \
      /workspace/pipeline/bin/stageB_one.sh \
      /workspace/pipeline/bin/status_write.py \
      /workspace/pipeline/bin/lib_safe.sh \
      /workspace/pipeline/bin/lib_paths.sh \
      /workspace/pipeline/bin/audit_one.sh \
      /workspace/pipeline/bin/contract_check.py 2>/dev/null || true
  } | tee "$out" >/dev/null
  echo "WROTE: $out"
}

echo "=== TOOLCHAIN SNAPSHOT ==="
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
write_toolchain_snapshot "$RUN_ID"
OUTDIR="/workspace/catalog/audit"
mkdir -p "$OUTDIR"
OUTFILE="${OUTDIR}/${MID}_${RUN_ID}.sha256"

sha256sum \
  "/workspace/catalog/media_manifest/${MID}.json" \
  "/workspace/catalog/asr/${MID}.wav" \
  "/workspace/catalog/asr/${MID}.json" \
  "/workspace/catalog/asr/${MID}.srt" \
  "/workspace/catalog/asr/${MID}.vtt" \
  "/workspace/catalog/enrich/${MID}.json" \
  "/workspace/catalog/keyframes/${MID}/"frame_*.jpg \
| sort | tee "$OUTFILE"

echo
echo "WROTE: $OUTFILE"
echo

echo "=== DIFF AGAINST PREVIOUS (if any) ==="
cd "$OUTDIR"
NEW_BASENAME="$(basename "$OUTFILE")"
PREV_BASENAME="$(ls -1t "${MID}"_*.sha256 | grep -v -F "$NEW_BASENAME" | head -n 1 || true)"

if [ -z "${PREV_BASENAME:-}" ]; then
  echo "OK: no previous audit file to diff against"
else
  echo "PREV=$PREV_BASENAME"
  echo "NEW=$NEW_BASENAME"
  if diff -q "$PREV_BASENAME" "$NEW_BASENAME" >/dev/null; then
    echo "OK: audit sets identical"
  else
    echo "NOT OK: audit sets differ"
    diff -u "$PREV_BASENAME" "$NEW_BASENAME" || true
    exit 9
  fi
fi

echo "=== AUDIT_ONE END OK ==="
