#!/usr/bin/env bash
# PUBLIC ARCHIVE SAFETY GATE: this historical installer/server is not maintained.
# Do not remove this guard and deploy directly. See README.md and SECURITY.md.
printf '%s\n' 'REFUSED: legacy OSF pipeline bootstrap is archived and unsafe to deploy. See SECURITY.md.' >&2
exit 78

# Phase 0 infrastructure setup — run once per fresh pod
# Installs: PostgreSQL 16, Redis 7, MinIO, Qdrant
# All persistent data on /workspace/.pod-persist/

set -o pipefail
PERSIST=/workspace/.pod-persist

echo "=== Phase 0 Infrastructure Setup ==="

# ── 1. PostgreSQL 16 ──────────────────────────────────────────
echo "[1/4] PostgreSQL 16..."
if ! command -v psql &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq postgresql-16 postgresql-client-16
fi

PG_DATA="$PERSIST/postgres/data"
PG_LOG="$PERSIST/postgres/pg.log"
mkdir -p "$(dirname "$PG_LOG")"

if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    echo "  Initialising PG data dir at $PG_DATA"
    mkdir -p "$PG_DATA"
    chown -R postgres:postgres "$PG_DATA"
    sudo -u postgres /usr/lib/postgresql/16/bin/initdb -D "$PG_DATA" --auth=md5 --pwfile=<(echo "postgres")
fi

# Configure pg_hba for local password auth
cat > "$PG_DATA/pg_hba.conf" <<'HBA'
local   all             postgres                                peer
local   all             all                                     md5
host    all             all             127.0.0.1/32            md5
host    all             all             ::1/128                 md5
HBA

# Start postgres
if ! pg_isready -q 2>/dev/null; then
    sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl \
        -D "$PG_DATA" -l "$PG_LOG" start -w
    sleep 2
fi

# Create DB user and database
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='media'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER media WITH PASSWORD 'media';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='media_evidence'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE media_evidence OWNER media;"
sudo -u postgres psql -d media_evidence -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null || true
sudo -u postgres psql -d media_evidence -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;" 2>/dev/null || true
echo "  PostgreSQL OK"

# ── 2. Redis 7 ────────────────────────────────────────────────
echo "[2/4] Redis 7..."
if ! command -v redis-server &>/dev/null; then
    apt-get install -y -qq redis-server
fi

REDIS_DATA="$PERSIST/redis"
mkdir -p "$REDIS_DATA"

if ! redis-cli ping &>/dev/null; then
    redis-server \
        --daemonize yes \
        --dir "$REDIS_DATA" \
        --logfile "$REDIS_DATA/redis.log" \
        --save 900 1 --save 300 10 \
        --maxmemory 512mb \
        --maxmemory-policy allkeys-lru
    sleep 1
fi
redis-cli ping | grep -q PONG && echo "  Redis OK" || echo "  Redis FAILED"

# ── 3. MinIO ──────────────────────────────────────────────────
echo "[3/4] MinIO..."
MINIO_BIN="$PERSIST/bin/minio"
if [ ! -f "$MINIO_BIN" ]; then
    echo "  Downloading MinIO..."
    curl -sSL "https://dl.min.io/server/minio/release/linux-amd64/minio" -o "$MINIO_BIN"
    chmod +x "$MINIO_BIN"
fi

MINIO_DATA="$PERSIST/minio-data"
mkdir -p "$MINIO_DATA"

if ! pgrep -f "minio server" &>/dev/null; then
    export MINIO_ROOT_USER=minioadmin
    export MINIO_ROOT_PASSWORD=minioadmin
    nohup "$MINIO_BIN" server "$MINIO_DATA" \
        --address :9000 --console-address :9001 \
        >> "$PERSIST/logs/minio.log" 2>&1 &
    sleep 2
fi
curl -sf http://localhost:9000/minio/health/live && echo "  MinIO OK" || echo "  MinIO FAILED"

# ── 4. Qdrant ─────────────────────────────────────────────────
echo "[4/4] Qdrant..."
QDRANT_BIN="$PERSIST/bin/qdrant"
if [ ! -f "$QDRANT_BIN" ]; then
    echo "  Downloading Qdrant..."
    QDRANT_VER="v1.13.4"
    TMP=$(mktemp -d)
    curl -sSL "https://github.com/qdrant/qdrant/releases/download/${QDRANT_VER}/qdrant-x86_64-unknown-linux-musl.tar.gz" \
        -o "$TMP/qdrant.tar.gz"
    tar -xzf "$TMP/qdrant.tar.gz" -C "$TMP"
    mv "$TMP/qdrant" "$QDRANT_BIN"
    chmod +x "$QDRANT_BIN"
    rm -rf "$TMP"
fi

QDRANT_DATA="$PERSIST/qdrant"
mkdir -p "$QDRANT_DATA"

if ! pgrep -f "qdrant" &>/dev/null; then
    nohup "$QDRANT_BIN" \
        --storage-path "$QDRANT_DATA" \
        --port 6333 \
        >> "$PERSIST/logs/qdrant.log" 2>&1 &
    sleep 3
fi
curl -sf http://localhost:6333/healthz && echo "  Qdrant OK" || echo "  Qdrant FAILED"

echo ""
echo "=== Infrastructure setup complete ==="
echo "  Postgres: postgresql://media:media@localhost:5432/media_evidence"
echo "  Redis:    redis://localhost:6379/0"
echo "  MinIO:    http://localhost:9000 (console: :9001)"
echo "  Qdrant:   http://localhost:6333"
