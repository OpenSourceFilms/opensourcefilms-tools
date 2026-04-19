#!/usr/bin/env bash
export PATH="/usr/lib/postgresql/16/bin:$PATH"
DUMP=/workspace/.pod-persist/postgres/media_evidence.sql.gz
mkdir -p "$(dirname "$DUMP")"
pg_dump -U postgres media_evidence 2>/dev/null | gzip > "$DUMP" && \
    echo "[pg_backup] $(date -u +%Y-%m-%dT%H:%M:%SZ) — $(du -sh "$DUMP" | cut -f1)"
