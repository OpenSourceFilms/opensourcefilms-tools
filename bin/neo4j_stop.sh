#!/usr/bin/env bash
# neo4j_stop.sh
# Stop Neo4j and verify ports are clear.
# No set -e / -u.

exec /workspace/pipeline/bin/in_cpu_vision.sh neo4j_stop "stop neo4j and verify ports are clear" -- bash -c '
  set +e
  NEO="/workspace/services/neo4j/current"

  "$NEO/bin/neo4j" stop
  echo "neo4j_stop_exit=$?"

  sleep 2
  echo "listeners_after:"
  ss -ltnp | egrep ":(7474|7687)\b" || true
'