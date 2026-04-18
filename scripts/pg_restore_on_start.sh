#!/usr/bin/env bash
# Run at pod startup if postgres is fresh (no media_evidence DB yet)
DUMP=/workspace/.pod-persist/postgres/media_evidence.sql.gz
psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='media_evidence'" | grep -q 1 && {
    echo "[pg_restore] media_evidence already exists, skipping restore"
    exit 0
}
psql -U postgres -c "CREATE USER media WITH PASSWORD 'media';" 2>/dev/null || true
psql -U postgres -c "CREATE DATABASE media_evidence OWNER media;" 2>/dev/null || true
if [ -f "$DUMP" ]; then
    echo "[pg_restore] Restoring from $DUMP..."
    zcat "$DUMP" | psql -U postgres media_evidence
    echo "[pg_restore] Done"
else
    echo "[pg_restore] No dump found — fresh database"
    psql -U postgres -d media_evidence -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null || true
    psql -U postgres -d media_evidence -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;" 2>/dev/null || true
    psql -U postgres -d media_evidence -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;" 2>/dev/null || true
fi
