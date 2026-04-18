#!/usr/bin/env bash
# Start FastAPI server and Dramatiq CPU worker
set -o pipefail
VENV=/workspace/.pod-persist/venvs/pipeline-v2

cd /workspace/pipeline
source "$VENV/bin/activate"

export PYTHONPATH=/workspace/pipeline

echo "Starting Dramatiq worker (ingest queue)..."
python -m dramatiq workers --queues ingest --processes 2 --threads 4 \
    >> /workspace/logs/dramatiq-cpu.log 2>&1 &
WORKER_PID=$!
echo "  Worker PID: $WORKER_PID"

echo "Starting FastAPI..."
uvicorn api.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info \
    --access-log \
    2>&1 | tee /workspace/logs/api.log
