#!/usr/bin/env bash
# Start all services on pod wake-up.
# Safe to run repeatedly — skips already-running services.

set -o pipefail
PERSIST=/workspace/.pod-persist
mkdir -p "$PERSIST/logs"

echo "=== Starting services ==="

# PostgreSQL
PG_DATA="$PERSIST/postgres/data"
if [ -f "$PG_DATA/PG_VERSION" ]; then
    if ! pg_isready -q 2>/dev/null; then
        sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl \
            -D "$PG_DATA" -l "$PERSIST/postgres/pg.log" start -w
        echo "  PostgreSQL started"
    else
        echo "  PostgreSQL already running"
    fi
else
    echo "  PostgreSQL not initialised — run setup_infrastructure.sh first"
fi

# Redis
if ! redis-cli ping &>/dev/null 2>&1; then
    REDIS_DATA="$PERSIST/redis"
    mkdir -p "$REDIS_DATA"
    redis-server \
        --daemonize yes \
        --dir "$REDIS_DATA" \
        --logfile "$REDIS_DATA/redis.log" \
        --save 900 1 --save 300 10 \
        --maxmemory 512mb \
        --maxmemory-policy allkeys-lru
    echo "  Redis started"
else
    echo "  Redis already running"
fi

# MinIO
MINIO_BIN="$PERSIST/bin/minio"
if [ -f "$MINIO_BIN" ] && ! pgrep -f "minio server" &>/dev/null; then
    export MINIO_ROOT_USER=minioadmin
    export MINIO_ROOT_PASSWORD=minioadmin
    nohup "$MINIO_BIN" server "$PERSIST/minio-data" \
        --address :9000 --console-address :9001 \
        >> "$PERSIST/logs/minio.log" 2>&1 &
    sleep 1
    echo "  MinIO started"
else
    echo "  MinIO already running or not installed"
fi

# Qdrant
QDRANT_BIN="$PERSIST/bin/qdrant"
if [ -f "$QDRANT_BIN" ] && ! pgrep -f "qdrant" &>/dev/null; then
    nohup "$QDRANT_BIN" \
        --config-path "$PERSIST/qdrant/config.yaml" \
        >> "$PERSIST/logs/qdrant.log" 2>&1 &
    sleep 2
    echo "  Qdrant started"
else
    echo "  Qdrant already running or not installed"
fi

echo ""
echo "=== Service status ==="
pg_isready -q && echo "  Postgres:  UP" || echo "  Postgres:  DOWN"
redis-cli ping 2>/dev/null | grep -q PONG && echo "  Redis:     UP" || echo "  Redis:     DOWN"
curl -sf http://localhost:9000/minio/health/live && echo "  MinIO:     UP" || echo "  MinIO:     DOWN"
curl -sf http://localhost:6333/healthz && echo "  Qdrant:    UP" || echo "  Qdrant:    DOWN"
