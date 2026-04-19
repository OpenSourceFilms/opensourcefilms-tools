#!/usr/bin/env bash
set -uo pipefail
source /workspace/pipeline/bin/lib_safe.sh

# Require variables to prevent -u exits
require_var IN

# Canonical operational log root (contract)
LOGROOT="/workspace/pipeline/logs.all"
TOOL_DIR="${LOGROOT}/tools/example_asr_run"
RUNS_DIR="${TOOL_DIR}/runs"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="example_${TS}"

OUTBASE="/workspace/catalog/asr/example_${TS}"
RUN_DIR="${RUNS_DIR}/${RUN_ID}"
LOG="${RUN_DIR}/example_asr_run.log"
WAV="${OUTBASE}.wav"

mkdir -p /workspace/catalog/asr "${RUN_DIR}"

run_step "extract_audio" ffmpeg -nostdin -y -i "$IN" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$WAV" \
  2>&1 | tee -a "$LOG"

# IMPORTANT: keep the pipeline inside bash -lc if you want the full tee logged as a unit
run_step "asr_logged" bash -lc '/workspace/pipeline/bin/asr_whispercpp.sh "$0" "$1" 2>&1 | tee -a "$2"' "$WAV" "$OUTBASE" "$LOG"

run_step "list_outputs" ls -lah "${OUTBASE}."* | tee -a "$LOG"

echo "OK: log at $LOG"
echo "OK: run_dir=$RUN_DIR"
echo "OK: logroot=$LOGROOT"
