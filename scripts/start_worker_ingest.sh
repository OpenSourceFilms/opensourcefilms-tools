#!/usr/bin/env bash
# RQ workers for ingest + ingest_gpu queues (probe, segment, keyframes, ASR, OCR)
# Uses the pipeline venv which has all ingest dependencies.
set -o pipefail

VENV=/workspace/.pod-persist/venvs/pipeline-v2
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
LOGS=/workspace/pipeline/logs.all
PIPELINE_DIR=/workspace/pipeline

export PYTHONPATH="$PIPELINE_DIR:$PYTHONPATH"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

echo "[start_worker_ingest] Starting ingest workers (PID $$)"

# CPU worker: probe, segment, keyframes, OCR scheduling
"$VENV/bin/python" "$VENV/bin/rq" worker ingest \
  --url "$REDIS_URL" \
  --name "rq-ingest-cpu-$$" \
  >> "$LOGS/rq-ingest.log" 2>&1 &
CPU_PID=$!

# GPU worker: ASR, OCR
"$VENV/bin/python" "$VENV/bin/rq" worker ingest_gpu \
  --url "$REDIS_URL" \
  --name "rq-ingest-gpu-$$" \
  >> "$LOGS/rq-ingest-gpu.log" 2>&1 &
GPU_PID=$!

echo "[start_worker_ingest] ingest PID=$CPU_PID  ingest_gpu PID=$GPU_PID"
wait
