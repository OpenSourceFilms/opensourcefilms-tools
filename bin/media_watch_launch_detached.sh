cd /workspace/opensourcefilms-tools || exit 1

cat > bin/media_watch_launch_detached.sh <<'EOF'
#!/usr/bin/env bash
set -o pipefail
umask 022

# Canonical operational log root (contract)
LOGROOT="/workspace/pipeline/logs.all"

# media_watch operational subtree
MW_ROOT="${LOGROOT}/media_watch"
MW_RUNS="${MW_ROOT}/runs"
MW_SCANS="${MW_ROOT}/scans"
MW_STAGE_LOGS="${MW_ROOT}/stage_logs"
MW_RUNSTATE="${MW_ROOT}/runstate"

PIDFILE="${MW_RUNSTATE}/media_watch.pid"
LOGFILE="${MW_RUNSTATE}/media_watch.log"

PY="/workspace/venvs/cpu/bin/python"
DAEMON="/workspace/pipeline/bin/media_watch_daemon.py"
POLICY="/workspace/pipeline/config/reprocess_policy.json"

WATCH_DIR="/workspace/BTS"
SCAN_INTERVAL_S="30"
SETTLE_SECONDS="90"
MAX_PER_SCAN="2"

# Refuse to run if legacy operational roots contain any files.
# These paths may exist as empty "straggler traps" only.
assert_no_legacy_writes() {
  bad=0

  if [[ -d /workspace/logs ]]; then
    if find /workspace/logs -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
      echo "ERROR: legacy artifacts detected under /workspace/logs (forbidden)."
      find /workspace/logs -mindepth 1 -maxdepth 5 -print 2>/dev/null | head -n 100
      bad=1
    fi
  fi

  if [[ -d /workspace/runstate ]]; then
    if find /workspace/runstate -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
      echo "ERROR: legacy artifacts detected under /workspace/runstate (forbidden)."
      find /workspace/runstate -mindepth 1 -maxdepth 5 -print 2>/dev/null | head -n 100
      bad=1
    fi
  fi

  if [[ "$bad" -ne 0 ]]; then
    echo "Refusing to start media_watch until legacy artifacts are eliminated."
    return 3
  fi
}

ensure_dirs() {
  mkdir -p "$MW_RUNS" "$MW_SCANS" "$MW_STAGE_LOGS" "$MW_RUNSTATE"
  chmod 755 "$LOGROOT" "$MW_ROOT" "$MW_RUNS" "$MW_SCANS" "$MW_STAGE_LOGS" "$MW_RUNSTATE" 2>/dev/null || true
}

bootstrap_contract_surfaces() {
  heartbeat_file="${LOGROOT}/_latest/heartbeat.json"
  manifest_file="${LOGROOT}/manifest/latest.json"
  now_utc=""
  tmp=""

  mkdir -p "${LOGROOT}/_latest" "${LOGROOT}/manifest"

  if [[ ! -f "$heartbeat_file" ]]; then
    now_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    tmp="$(mktemp "${heartbeat_file}.tmp.XXXXXX")"
    cat >"$tmp" <<EOF2
{
  "record_type": "heartbeat",
  "schema_version": 1,
  "status": "bootstrap",
  "updated_utc": "${now_utc}"
}
EOF2
    mv -f "$tmp" "$heartbeat_file"
  fi

  if [[ ! -f "$manifest_file" ]]; then
    now_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    tmp="$(mktemp "${manifest_file}.tmp.XXXXXX")"
    cat >"$tmp" <<EOF2
{
  "record_type": "manifest_latest",
  "schema_version": 1,
  "status": "bootstrap",
  "updated_utc": "${now_utc}"
}
EOF2
    mv -f "$tmp" "$manifest_file"
  fi
}

read_pid() {
  if [[ -f "$PIDFILE" ]]; then
    cat "$PIDFILE" 2>/dev/null || true
  fi
}

pid_is_running() {
  pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

cmd_start() {
  assert_no_legacy_writes || return $?
  ensure_dirs
  bootstrap_contract_surfaces

  pid="$(read_pid)"

  if pid_is_running "$pid"; then
    echo "already running pid=$pid"
    return 0
  fi

  # Clean stale pidfile if present
  rm -f "$PIDFILE" 2>/dev/null || true

  nohup "$PY" "$DAEMON" \
    --watch "$WATCH_DIR" \
    --policy "$POLICY" \
    --scan-interval-s "$SCAN_INTERVAL_S" \
    --settle-seconds "$SETTLE_SECONDS" \
    --max-per-scan "$MAX_PER_SCAN" \
    --publish-stageC \
    --state "${MW_RUNSTATE}/media_watch_state.json" \
    --log "$LOGFILE" \
    >/dev/null 2>&1 &

  pid=$!
  echo "$pid" > "$PIDFILE"

  echo "started pid=$pid"
  echo "logroot=$LOGROOT"
  echo "pidfile=$PIDFILE"
  echo "daemon_log=$LOGFILE"
}

cmd_status() {
  pid="$(read_pid)"

  if [[ -z "$pid" ]]; then
    echo "not running (no pidfile or empty pidfile)"
    return 1
  fi

  if pid_is_running "$pid"; then
    echo "running pid=$pid"
    return 0
  fi

  echo "not running (stale pidfile pid=$pid)"
  return 1
}

cmd_stop() {
  pid="$(read_pid)"

  if [[ -z "$pid" ]]; then
    echo "not running (no pidfile or empty pidfile)"
    rm -f "$PIDFILE" 2>/dev/null || true
    return 0
  fi

  if pid_is_running "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true

    # wait up to ~10s for graceful exit
    for _ in {1..20}; do
      if ! pid_is_running "$pid"; then
        break
      fi
      sleep 0.5
    done

    if pid_is_running "$pid"; then
      echo "WARN: still running after TERM, sending SIGKILL pid=$pid"
      kill -KILL "$pid" 2>/dev/null || true
    fi

    echo "stopped pid=$pid"
  else
    echo "not running (stale pidfile pid=$pid)"
  fi

  rm -f "$PIDFILE" 2>/dev/null || true
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 2
    ;;
esac
EOF
