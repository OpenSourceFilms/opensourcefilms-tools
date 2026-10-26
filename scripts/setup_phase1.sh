#!/usr/bin/env bash
# PUBLIC ARCHIVE SAFETY GATE: this historical installer/server is not maintained.
# Do not remove this guard and deploy directly. See README.md and SECURITY.md.
printf '%s\n' 'REFUSED: legacy OSF pipeline bootstrap is archived and unsafe to deploy. See SECURITY.md.' >&2
exit 78

# Phase 1 setup: install services, deps, GPU models, run migrations
# Run on a fresh B200 pod. Safe to re-run.
set -o pipefail

PERSIST=/workspace/.pod-persist
VENV="$PERSIST/venvs/pipeline-v2"
WEIGHTS=/workspace/weights
LOGS=/workspace/pipeline/logs.all
mkdir -p "$LOGS" "$WEIGHTS"

log() { echo "[setup] $*" | tee -a "$LOGS/setup_phase1.log"; }
die() { log "ERROR: $*"; exit 1; }

log "=== Phase 1 Setup — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
log "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'none')"

# ── 1. System packages ───────────────────────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    postgresql-16 postgresql-client-16 \
    redis-server \
    git curl wget unzip ffmpeg \
    python3-dev libpq-dev \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender-dev \
    > /dev/null 2>&1
log "System packages installed"

# Add pg binaries to PATH for this session
export PATH="/usr/lib/postgresql/16/bin:$PATH"

# ── 2. PostgreSQL — try /workspace/.pgdata (Option A) ───────────────────────
PG_DATA=/workspace/.pgdata

if pg_isready -q 2>/dev/null; then
    log "PostgreSQL already running"
else
    if [ ! -f "$PG_DATA/PG_VERSION" ]; then
        log "Initialising PostgreSQL at $PG_DATA ..."
        mkdir -p "$PG_DATA"
        chmod 700 "$PG_DATA" 2>/dev/null || true

        # Try initdb as postgres user
        if sudo -u postgres /usr/lib/postgresql/16/bin/initdb -D "$PG_DATA" \
            --auth=md5 --pwprompt=no -U postgres 2>&1 | tee -a "$LOGS/setup_phase1.log"; then
            log "initdb succeeded at $PG_DATA"
        else
            # Fallback: /var/lib/postgresql (ephemeral container path, no chmod issues)
            log "initdb at /workspace/.pgdata failed — falling back to /var/lib/postgresql/data"
            PG_DATA=/var/lib/postgresql/data
            mkdir -p "$PG_DATA"
            sudo -u postgres /usr/lib/postgresql/16/bin/initdb -D "$PG_DATA" \
                --auth=trust -U postgres 2>&1 | tee -a "$LOGS/setup_phase1.log" || die "initdb failed"
            log "initdb succeeded at $PG_DATA (ephemeral — using backup/restore strategy)"
            # Enable Option B: update pg_backup/restore to use this path
            sed -i "s|/workspace/.pgdata|$PG_DATA|g" /workspace/pipeline/scripts/pg_backup.sh 2>/dev/null || true
        fi
    else
        log "PostgreSQL data dir exists at $PG_DATA"
    fi

    # Start PostgreSQL
    sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl \
        -D "$PG_DATA" \
        -l "$LOGS/postgresql.log" \
        start -w -t 30 2>&1 | tee -a "$LOGS/setup_phase1.log" || die "PG failed to start"
    log "PostgreSQL started from $PG_DATA"
fi

# Create DB user + database
psql -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='media'" | grep -q 1 || \
    psql -U postgres -c "CREATE USER media WITH PASSWORD 'media';" 2>/dev/null
psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='media_evidence'" | grep -q 1 || {
    psql -U postgres -c "CREATE DATABASE media_evidence OWNER media;" 2>/dev/null
    psql -U postgres -d media_evidence \
        -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pgcrypto;" 2>/dev/null
    log "Database media_evidence created"
}
log "PostgreSQL ready"

# Record which PG_DATA we ended up using
echo "$PG_DATA" > /workspace/.pgdata_path
echo "export PG_DATA=$PG_DATA" >> /etc/profile.d/zz-claude-persist.sh

# ── 3. Redis ────────────────────────────────────────────────────────────────
if ! redis-cli ping &>/dev/null 2>&1; then
    log "Starting Redis..."
    mkdir -p "$PERSIST/redis"
    redis-server \
        --daemonize yes \
        --dir "$PERSIST/redis" \
        --logfile "$PERSIST/redis/redis.log" \
        --save 900 1 --save 300 10 \
        --maxmemory 1gb \
        --maxmemory-policy allkeys-lru
    sleep 1
fi
redis-cli ping | grep -q PONG && log "Redis UP" || die "Redis failed to start"

# ── 4. MinIO ────────────────────────────────────────────────────────────────
if [ ! -f "$PERSIST/bin/minio" ]; then
    log "Installing MinIO..."
    curl -fsSL https://dl.min.io/server/minio/release/linux-amd64/minio \
        -o "$PERSIST/bin/minio"
    chmod +x "$PERSIST/bin/minio"
fi
if ! pgrep -f "minio server" &>/dev/null; then
    export MINIO_ROOT_USER=minioadmin
    export MINIO_ROOT_PASSWORD=minioadmin
    nohup "$PERSIST/bin/minio" server "$PERSIST/minio-data" \
        --address :9000 --console-address :9001 \
        >> "$LOGS/minio.log" 2>&1 &
    sleep 2
fi
curl -sf http://localhost:9000/minio/health/live && log "MinIO UP" || log "WARNING: MinIO not responding"

# ── 5. Qdrant ────────────────────────────────────────────────────────────────
if [ ! -f "$PERSIST/bin/qdrant" ]; then
    log "Installing Qdrant..."
    QDRANT_VER=$(curl -s https://api.github.com/repos/qdrant/qdrant/releases/latest \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "v1.13.0")
    curl -fsSL \
        "https://github.com/qdrant/qdrant/releases/download/${QDRANT_VER}/qdrant-x86_64-unknown-linux-gnu.tar.gz" \
        -o /tmp/qdrant.tar.gz
    tar -xzf /tmp/qdrant.tar.gz -C "$PERSIST/bin/" qdrant
    chmod +x "$PERSIST/bin/qdrant"
    rm -f /tmp/qdrant.tar.gz
fi
mkdir -p "$PERSIST/qdrant"
if [ ! -f "$PERSIST/qdrant/config.yaml" ]; then
    cat > "$PERSIST/qdrant/config.yaml" << 'QDRANT_CFG'
storage:
  storage_path: /workspace/.pod-persist/qdrant/storage
service:
  host: 0.0.0.0
  http_port: 6333
  grpc_port: 6334
QDRANT_CFG
fi
if ! pgrep -f qdrant &>/dev/null; then
    nohup "$PERSIST/bin/qdrant" \
        --config-path "$PERSIST/qdrant/config.yaml" \
        >> "$LOGS/qdrant.log" 2>&1 &
    sleep 3
fi
curl -sf http://localhost:6333/healthz && log "Qdrant UP" || log "WARNING: Qdrant not responding"

# ── 6. Python deps in pipeline-v2 venv ───────────────────────────────────────
log "Creating/updating pipeline-v2 venv..."
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"

log "Installing Python requirements..."
pip install -q --upgrade pip setuptools wheel

# Core deps from requirements.txt
pip install -q \
    "fastapi==0.115.6" "uvicorn[standard]==0.34.0" \
    "pydantic==2.10.4" "pydantic-settings==2.7.1" \
    "sqlalchemy==2.0.36" "asyncpg==0.30.0" "psycopg2-binary==2.9.10" \
    "alembic==1.14.0" \
    "rq==2.3.3" "redis==5.2.1" \
    "boto3==1.36.7" \
    "opencv-python-headless==4.10.0.84" \
    "imagehash==4.3.1" "Pillow==11.1.0" \
    "faster-whisper==1.1.0" "soundfile==0.12.1" \
    "langdetect==1.0.9" \
    "scenedetect==0.6.4" \
    "qdrant-client==1.12.2" \
    "httpx==0.28.1" "aiofiles==24.1.0" \
    "python-multipart==0.0.20" \
    "numpy" \
    2>&1 | tail -5

# Transformers + accelerate for embedding models
log "Installing transformers/accelerate..."
pip install -q "transformers>=4.51.0" "accelerate>=1.4.0" \
    "sentencepiece>=0.2.0" "tokenizers>=0.21.0" \
    2>&1 | tail -3

# PaddleOCR GPU
log "Installing PaddleOCR GPU..."
pip install -q paddlepaddle-gpu paddleocr 2>&1 | tail -3
python3 -c "import paddleocr; print('PaddleOCR OK')" 2>/dev/null || log "WARNING: PaddleOCR install may have issues"

# pyannote.audio for diarization
log "Installing pyannote.audio..."
pip install -q "pyannote.audio==3.3.2" 2>&1 | tail -3 || \
    pip install -q "pyannote.audio" 2>&1 | tail -3
python3 -c "import pyannote.audio; print('pyannote OK')" 2>/dev/null || log "WARNING: pyannote install may have issues"

log "Python dependencies installed"

# ── 7. TransNetV2 weights ────────────────────────────────────────────────────
TNV2_DIR="$WEIGHTS/transnetv2"
mkdir -p "$TNV2_DIR"

if [ ! -d "$TNV2_DIR/repo" ]; then
    log "Cloning TransNetV2..."
    git clone --depth 1 https://github.com/soCzech/TransNetV2 "$TNV2_DIR/repo" 2>&1 | tail -3
fi

# Download weights
TNV2_WEIGHTS="$TNV2_DIR/transnetv2-weights.npz"
if [ ! -f "$TNV2_WEIGHTS" ]; then
    log "Downloading TransNetV2 weights..."
    # Try GitHub releases first
    curl -fsSL \
        "https://github.com/soCzech/TransNetV2/releases/download/v2.0/transnetv2-weights.npz" \
        -o "$TNV2_WEIGHTS" 2>/dev/null || \
    # Fallback: from repo
    wget -q -O "$TNV2_WEIGHTS" \
        "https://github.com/soCzech/TransNetV2/releases/download/v2.0/transnetv2-weights.npz" 2>/dev/null
    [ -f "$TNV2_WEIGHTS" ] && log "TransNetV2 weights downloaded" || log "WARNING: TransNetV2 weights download failed — will use PySceneDetect fallback"
fi

# Copy TransNetV2 repo to third_party
TNV2_THIRD_PARTY=/workspace/pipeline/third_party/transnetv2
if [ ! -d "$TNV2_THIRD_PARTY" ] && [ -d "$TNV2_DIR/repo/inference-pytorch" ]; then
    mkdir -p "$TNV2_THIRD_PARTY"
    cp -r "$TNV2_DIR/repo/inference-pytorch/." "$TNV2_THIRD_PARTY/"
    log "TransNetV2 inference code copied to third_party/"
fi

# ── 8. Faster-whisper large-v3 model pre-download ────────────────────────────
log "Pre-downloading faster-whisper large-v3..."
WHISPER_DIR=/workspace/pipeline/models/whispercpp
mkdir -p "$WHISPER_DIR"
python3 -c "
from faster_whisper import WhisperModel
import os
m = WhisperModel('large-v3', device='cpu', compute_type='int8',
                  download_root='$WHISPER_DIR')
print('faster-whisper large-v3: downloaded OK')
" 2>&1 | tail -5 || log "WARNING: whisper model pre-download failed — will download on first use"

# ── 9. Run DB migrations ──────────────────────────────────────────────────────
log "Running Alembic migrations..."
cd /workspace/pipeline
export PYTHONPATH=/workspace/pipeline
source "$VENV/bin/activate"
alembic upgrade head 2>&1 | tee -a "$LOGS/setup_phase1.log" || die "Alembic migration failed"
log "Migrations applied"

# ── 10. Set up Qdrant collections ────────────────────────────────────────────
log "Setting up Qdrant collections..."
python3 /workspace/pipeline/scripts/setup_qdrant_collections.py 2>&1 | tee -a "$LOGS/setup_phase1.log"

# ── 11. MinIO buckets ─────────────────────────────────────────────────────────
log "Ensuring MinIO buckets..."
python3 -c "
import sys; sys.path.insert(0,'.')
from store.client import ensure_buckets
ensure_buckets()
print('MinIO buckets OK')
" 2>&1 | tail -3

# ── 12. Set up pg_backup cron ─────────────────────────────────────────────────
log "Configuring pg_backup cron..."
# Write wrapper that uses the correct PG_DATA
cat > /workspace/pipeline/scripts/pg_backup_auto.sh << 'PGBAK'
#!/usr/bin/env bash
export PATH="/usr/lib/postgresql/16/bin:$PATH"
DUMP=/workspace/.pod-persist/postgres/media_evidence.sql.gz
mkdir -p "$(dirname "$DUMP")"
pg_dump -U postgres media_evidence 2>/dev/null | gzip > "$DUMP" && \
    echo "[pg_backup] $(date -u +%Y-%m-%dT%H:%M:%SZ) — $(du -sh "$DUMP" | cut -f1)"
PGBAK
chmod +x /workspace/pipeline/scripts/pg_backup_auto.sh

# Install cron job
(crontab -l 2>/dev/null | grep -v pg_backup_auto; \
 echo "*/15 * * * * /workspace/pipeline/scripts/pg_backup_auto.sh >> /workspace/pipeline/logs.all/pg_backup.log 2>&1") | crontab -
log "pg_backup cron set (every 15 min)"

# ── 13. Status check ──────────────────────────────────────────────────────────
log ""
log "=== Service Status ==="
export PATH="/usr/lib/postgresql/16/bin:$PATH"
pg_isready -U postgres && log "  PostgreSQL: UP" || log "  PostgreSQL: DOWN"
redis-cli ping 2>/dev/null | grep -q PONG && log "  Redis:      UP" || log "  Redis:      DOWN"
curl -sf http://localhost:9000/minio/health/live && log "  MinIO:      UP" || log "  MinIO:      DOWN"
curl -sf http://localhost:6333/healthz && log "  Qdrant:     UP" || log "  Qdrant:     DOWN"
python3 -c "import torch; print(f'  Torch:      {torch.__version__} CUDA={torch.cuda.is_available()}')" 2>/dev/null || true

log ""
log "=== Setup Complete ==="
log "Next: bash /workspace/pipeline/scripts/start_api.sh"
