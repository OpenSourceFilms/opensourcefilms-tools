#!/usr/bin/env bash
# /workspace/pipeline/bin/stageA_launch_detached.sh
set -uo pipefail

IN="${1:?usage: stageA_launch_detached.sh /abs/path/to/media}"

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
LOG="/workspace/catalog/logs/stageA_detached_${RUN_ID}.log"
PIDFILE="/workspace/catalog/logs/stageA_detached_${RUN_ID}.pid"

mkdir -p /workspace/catalog/logs
: > "$LOG"

echo "Launching Stage A detached" | tee -a "$LOG"
echo "IN=$IN" | tee -a "$LOG"
echo "RUN_ID=$RUN_ID" | tee -a "$LOG"
echo "LOG=$LOG" | tee -a "$LOG"
echo "PIDFILE=$PIDFILE" | tee -a "$LOG"
echo | tee -a "$LOG"

# Force line-buffered output so tail shows progress as it happens.
# shellcheck disable=SC2002
nohup stdbuf -oL -eL bash -lc '
  set -u
  export IN="$0"
  export RUN_ID="$1"
  /workspace/pipeline/bin/stageA_one.sh
' "$IN" "$RUN_ID" >>"$LOG" 2>&1 &

PID=$!
echo "$PID" > "$PIDFILE"

echo "OK: stageA_one.sh PID=$PID" | tee -a "$LOG"
echo "OK: now tail it with: tail -f $LOG" | tee -a "$LOG"
