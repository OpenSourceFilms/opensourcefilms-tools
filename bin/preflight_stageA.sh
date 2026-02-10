#!/usr/bin/env bash
set -u

ok=1
warn() { echo "WARN: $*" >&2; }
fail() { echo "FAIL: $*" >&2; ok=0; }

echo "=== Preflight Stage A ==="

# Required dirs
for d in /workspace/ingest /workspace/state /workspace/catalog /workspace/index /workspace/tmp; do
  [ -d "$d" ] || fail "Missing dir: $d"
done

# Required subdirs (create if missing, but do not fail)
for d in \
  /workspace/catalog/asr \
  /workspace/catalog/logs \
  /workspace/catalog/media_manifest \
  /workspace/catalog/chunks \
  /workspace/catalog/frames \
  /workspace/state/queue_cpu \
  /workspace/state/processing_cpu \
  /workspace/state/done_cpu \
  /workspace/state/failed_cpu
do
  if [ ! -d "$d" ]; then
    warn "Missing $d, creating it"
    mkdir -p "$d" || fail "Could not create $d"
  fi
done

# Binaries
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg not in PATH"
command -v ffprobe >/dev/null 2>&1 || fail "ffprobe not in PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 not in PATH"

# whisper.cpp binary + model
BIN="/workspace/pipeline/third_party/whisper.cpp/build/bin/whisper-cli"
MODEL="/workspace/pipeline/models/whispercpp/ggml-small.en.bin"
VAD_MODEL="/workspace/pipeline/third_party/whisper.cpp/models/for-tests-silero-v6.2.0-ggml.bin"

[ -x "$BIN" ] || fail "Missing whisper-cli executable: $BIN"
[ -f "$MODEL" ] || fail "Missing model file: $MODEL"
[ -f "$VAD_MODEL" ] || fail "Missing Silero VAD model file: $VAD_MODEL"

# Stage A helpers (authoritative)
NORM="/workspace/pipeline/bin/asr_normalize_srt.py"
CHK="/workspace/pipeline/bin/stageA_contract_check.py"
[ -x "$NORM" ] || fail "Missing or not executable: $NORM"
[ -f "$CHK" ] || fail "Missing: $CHK"

# Quick smoke that scripts at least import and show help
python3 "$NORM" --help >/dev/null 2>&1 || fail "asr_normalize_srt.py not runnable"
python3 "$CHK" --help >/dev/null 2>&1 || fail "stageA_contract_check.py not runnable"

# Write permission sanity
touch /workspace/catalog/logs/.write_test 2>/dev/null && rm -f /workspace/catalog/logs/.write_test || fail "No write permission in /workspace/catalog/logs"

# Report
if [ "$ok" -eq 1 ]; then
  echo "OK: Preflight passed"
  exit 0
else
  echo "ERROR: Preflight failed, see FAIL lines above" >&2
  exit 2
fi
