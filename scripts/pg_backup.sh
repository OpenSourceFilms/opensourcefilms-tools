#!/usr/bin/env bash
# Run on pod shutdown or periodically to persist postgres to workspace volume
DUMP=/workspace/.pod-persist/postgres/media_evidence.sql.gz
pg_dump -U postgres media_evidence | gzip > "$DUMP"
echo "[pg_backup] Dumped to $DUMP ($(du -sh $DUMP | cut -f1))"
