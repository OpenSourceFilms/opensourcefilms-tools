#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://127.0.0.1:3000/api/health}"
TIMEOUT_S="${2:-60}"
INTERVAL_S="${3:-1}"

max_tries=$((TIMEOUT_S / INTERVAL_S))
i=0

echo "Waiting for media_webroot health: $URL (timeout ${TIMEOUT_S}s)…"

while [ $i -lt $max_tries ]; do
  if curl -fsS "$URL" >/dev/null 2>&1; then
    echo "media_webroot is healthy"
    exit 0
  fi
  sleep "$INTERVAL_S"
  i=$((i + 1))
done

echo "ERROR: media_webroot did not become healthy within ${TIMEOUT_S}s"
exit 1
