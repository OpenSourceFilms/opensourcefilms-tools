#!/usr/bin/env bash
set -o pipefail

PORT="${1:-3000}"
URL="http://127.0.0.1:${PORT}/api/health"
MAX_SECS="${2:-30}"

echo "Waiting for port ${PORT} (up to ${MAX_SECS}s)..."
for i in $(seq 1 "${MAX_SECS}"); do
  if ss -lptn "sport = :${PORT}" 2>/dev/null | grep -q LISTEN; then
    echo "Port ${PORT} is LISTENING."
    break
  fi
  sleep 1
done

echo "Waiting for health ${URL} (up to ${MAX_SECS}s)..."
for i in $(seq 1 "${MAX_SECS}"); do
  code="$(curl -s -o /dev/null -w "%{http_code}" "${URL}" || true)"
  if [ "${code}" = "200" ]; then
    echo "Health OK."
    curl -s "${URL}" || true
    exit 0
  fi
  sleep 1
done

echo "ERROR: webroot not ready after ${MAX_SECS}s"
exit 1
