#!/usr/bin/env bash
# Run at pod startup — initialises /pgdata if needed, restores from dump
export PATH="/usr/lib/postgresql/16/bin:$PATH"
DUMP=/workspace/.pod-persist/postgres/media_evidence.sql.gz
PG_DATA=/pgdata
mkdir -p "$PG_DATA"
chown postgres:postgres "$PG_DATA"

if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    echo "[pg_restore] Initialising fresh PostgreSQL at $PG_DATA..."
    sudo -u postgres /usr/lib/postgresql/16/bin/initdb -D "$PG_DATA" --auth=trust -U postgres
fi
sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl -D "$PG_DATA" \
    -l /workspace/pipeline/logs.all/postgresql.log start -w -t 30 || exit 1

psql -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='media'" | grep -q 1 || \
    psql -U postgres -c "CREATE USER media WITH PASSWORD 'media';" 2>/dev/null
psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='media_evidence'" | grep -q 1 && {
    echo "[pg_restore] media_evidence already exists, skipping restore"
    exit 0
}
psql -U postgres -c "CREATE DATABASE media_evidence OWNER media;" 2>/dev/null
if [ -f "$DUMP" ]; then
    echo "[pg_restore] Restoring from $DUMP..."
    zcat "$DUMP" | psql -U postgres media_evidence
    echo "[pg_restore] Done"
else
    echo "[pg_restore] No dump — fresh DB"
    psql -U postgres -d media_evidence \
        -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pgcrypto;" 2>/dev/null
fi
