#!/usr/bin/env bash
set -uo pipefail
source /workspace/pipeline/bin/lib_safe.sh

# Require variables to prevent -u exits
require_var IN

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUTBASE="/workspace/catalog/asr/example_${TS}"
LOG="/workspace/catalog/logs/example_${TS}.log"
WAV="${OUTBASE}.wav"

mkdir -p /workspace/catalog/asr /workspace/catalog/logs

run_step "extract_audio" ffmpeg -nostdin -y -i "$IN" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$WAV" \
  2>&1 | tee -a "$LOG"

# IMPORTANT: keep the pipeline inside bash -lc if you want the full tee logged as a unit
run_step "asr_logged" bash -lc '/workspace/pipeline/bin/asr_whispercpp.sh "$0" "$1" 2>&1 | tee -a "$2"' "$WAV" "$OUTBASE" "$LOG"

run_step "list_outputs" ls -lah "${OUTBASE}."* | tee -a "$LOG"

echo "OK: log at $LOG"
