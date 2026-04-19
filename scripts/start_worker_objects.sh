#!/usr/bin/env bash
# RQ worker for the objects queue — uses the GroundingDINO venv.
# This venv has: groundingdino, transformers==4.37.2, torch, rq, redis,
#                sqlalchemy, psycopg2, asyncpg, pydantic-settings, boto3.
# Isolated because GroundingDINO requires transformers<5 (4.37.x pinned).
set -o pipefail

VENV=/workspace/venvs/groundingdino
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
LOGS=/workspace/pipeline/logs.all
PIPELINE_DIR=/workspace/pipeline

export PYTHONPATH="$PIPELINE_DIR:$PYTHONPATH"

if [ ! -d "$VENV" ]; then
    echo "[start_worker_objects] ERROR: venv not found at $VENV" >&2
    exit 1
fi

echo "[start_worker_objects] Starting objects worker (PID $$) using venv $VENV"

exec "$VENV/bin/python" "$VENV/bin/rq" worker objects \
  --with-scheduler \
  --url "$REDIS_URL" \
  --name "rq-objects-$$" \
  >> "$LOGS/rq-objects.log" 2>&1
