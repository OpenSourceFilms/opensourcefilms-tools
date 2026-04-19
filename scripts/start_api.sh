#!/usr/bin/env bash
# Start FastAPI + RQ workers (CPU ingest + GPU ingest_gpu)
set -o pipefail
VENV=/workspace/.pod-persist/venvs/pipeline-v2
LOGS=/workspace/pipeline/logs.all
mkdir -p "$LOGS"

cd /workspace/pipeline
source "$VENV/bin/activate"
export PYTHONPATH=/workspace/pipeline

echo "Starting RQ worker (ingest queue — CPU ops)..."
rq worker ingest \
    --url "$(python3 -c 'from config.settings import settings; print(settings.redis_url)')" \
    --name "rq-ingest-$$" \
    >> "$LOGS/rq-ingest.log" 2>&1 &
echo "  RQ ingest PID: $!"

echo "Starting RQ worker (ingest_gpu queue — GPU ops)..."
rq worker ingest_gpu \
    --url "$(python3 -c 'from config.settings import settings; print(settings.redis_url)')" \
    --name "rq-ingest-gpu-$$" \
    >> "$LOGS/rq-ingest-gpu.log" 2>&1 &
echo "  RQ ingest_gpu PID: $!"

echo "Starting FastAPI..."
uvicorn api.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level info \
    --access-log \
    2>&1 | tee "$LOGS/api.log"
