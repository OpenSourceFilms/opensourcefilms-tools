#!/usr/bin/env bash
# RQ worker for the embed queue — uses the Qwen3 clean venv.
# This venv has: sentence-transformers, transformers, torch, qdrant-client,
#                rq, redis, sqlalchemy, psycopg2.
set -o pipefail

VENV=/workspace/venvs/qwen3_embed_clean
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
LOGS=/workspace/pipeline/logs.all
PIPELINE_DIR=/workspace/pipeline

export PYTHONPATH="$PIPELINE_DIR:$PYTHONPATH"
# Suppress PaddleOCR connectivity check (not used in embed worker but imported transitively)
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
# HuggingFace token
if [ -z "$HF_TOKEN" ] && [ -f "/workspace/.cache/huggingface/token" ]; then
    export HF_TOKEN="$(cat /workspace/.cache/huggingface/token)"
fi

echo "[start_worker_embed] Starting embed worker (PID $$) using venv $VENV"

exec "$VENV/bin/python" "$VENV/bin/rq" worker embed \
  --url "$REDIS_URL" \
  --name "rq-embed-$$" \
  >> "$LOGS/rq-embed.log" 2>&1
