#!/usr/bin/env bash
set -uo pipefail   # NOTE: no -e on purpose

source /workspace/pipeline/bin/lib_safe.sh

require_var IN

TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/workspace/catalog/logs/pipeline_${TS}.log"
mkdir -p /workspace/catalog/logs

echo "START: $TS" | tee -a "$LOG"
echo "IN=$IN" | tee -a "$LOG"

# Stage A
run_step "stageA_one" bash -lc 'IN="$0" /workspace/pipeline/bin/stageA_one.sh' "$IN" \
  2>&1 | tee -a "$LOG"
rc_a=${PIPESTATUS[0]}
if [[ $rc_a -ne 0 ]]; then
  echo "WARN: Stage A failed rc=$rc_a, skipping Stage B" | tee -a "$LOG"
  echo "DONE: $TS" | tee -a "$LOG"
  echo "OK: log $LOG"
  exit $rc_a
fi

# Extract MEDIA_ID from the pipeline log (Stage A prints OK: MEDIA_ID=...)
MEDIA_ID="$(grep -Eo 'OK: MEDIA_ID=[0-9a-f]{64}' "$LOG" | tail -n 1 | cut -d= -f2 || true)"

if [[ -z "${MEDIA_ID:-}" ]]; then
  echo "WARN: could not determine MEDIA_ID from log, skipping Stage B" | tee -a "$LOG"
else
  echo "MEDIA_ID=$MEDIA_ID" | tee -a "$LOG"
  run_step "stageB_one" bash -lc 'MEDIA_ID="$0" /workspace/pipeline/bin/stageB_one.sh' "$MEDIA_ID" \
    2>&1 | tee -a "$LOG"
  rc_b=${PIPESTATUS[0]}
  if [[ $rc_b -ne 0 ]]; then
    echo "WARN: Stage B failed rc=$rc_b" | tee -a "$LOG"
    echo "DONE: $TS" | tee -a "$LOG"
    echo "OK: log $LOG"
    exit $rc_b
  fi
fi

echo "DONE: $TS" | tee -a "$LOG"
echo "OK: log $LOG"
