#!/usr/bin/env bash
# neo4j_start.sh
# Start Neo4j (localhost-only config) and wait for readiness.
# No set -e / -u.

exec /workspace/pipeline/bin/in_cpu_vision.sh neo4j_start "start neo4j and wait for port readiness" -- bash -c '
  set +e
  NEO="/workspace/services/neo4j/current"

  # Start (daemonizes)
  "$NEO/bin/neo4j" start
  echo "neo4j_start_exit=$?"

  # Wait up to 30s for bolt/http listeners
  i=0
  while [ "$i" -lt 30 ]; do
    ss -ltn | grep -q ":7474" && ss -ltn | grep -q ":7687" && break
    sleep 1
    i=$((i+1))
  done

  echo "wait_s=$i"
  echo "listeners:"
  ss -ltnp | egrep ":(7474|7687)\b" || true

  echo "http_head:"
  curl -sS -I http://127.0.0.1:7474/ | head -n 10 || true
'