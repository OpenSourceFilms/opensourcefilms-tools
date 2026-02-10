#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Contract flags (explicit, machine-readable)
# ============================================================================
export MEDIA_WEBROOT_READ_ONLY="1"
export MEDIA_WEBROOT_SNAPSHOT_ONLY="1"
export MEDIA_WEBROOT_NEVER_OPEN_CATALOG_DB="1"

# ============================================================================
# Canonical operational log root (NON-NEGOTIABLE)
# All operational writes for this service MUST stay under this root.
# ============================================================================
export OP_LOGROOT="/workspace/pipeline/logs.all"
export SERVICE="media_webroot"
export SERVICE_DIR="${OP_LOGROOT}/${SERVICE}"

mkdir -p "${SERVICE_DIR}"

# ============================================================================
# Run identity (explicit)
# ============================================================================
export RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_$$"
export TS_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

export RUNSTATE_JSON="${SERVICE_DIR}/service_runstate_${RUN_ID}.json"
export RUNSTATE_TMP="${SERVICE_DIR}/.service_runstate_${RUN_ID}.json.tmp"
export ERROR_JSON="${SERVICE_DIR}/service_error_${RUN_ID}.json"
export ERROR_TMP="${SERVICE_DIR}/.service_error_${RUN_ID}.json.tmp"
export DAEMON_LOG="${SERVICE_DIR}/daemon.log"

# ============================================================================
# Identity / provenance
# ============================================================================
export PY="/workspace/venvs/cpu/bin/python"
export APP_DIR="/workspace/pipeline/bin"
export APP_FILE="/workspace/pipeline/bin/webroot_server.py"

export APP_SHA256="$("$PY" - <<'PY'
import hashlib, pathlib
p = pathlib.Path("/workspace/pipeline/bin/webroot_server.py")
print(hashlib.sha256(p.read_bytes()).hexdigest())
PY
)"

# Snapshot and catalog paths (explicit even if missing)
export CATALOG_DB="/workspace/catalog/milvus/moments.db"
export SNAPSHOT_DB="/workspace/webroot/milvus/moments.snapshot.db"

# ============================================================================
# Minimal forensic log line (append + best-effort durability)
# ============================================================================
log_line () {
  local msg="$1"
  printf "%s %s\n" "$TS_UTC" "$msg" >> "$DAEMON_LOG"
  sync "$DAEMON_LOG" >/dev/null 2>&1 || true
}

cleanup_tmp () {
  rm -f "$RUNSTATE_TMP" "$ERROR_TMP" >/dev/null 2>&1 || true
}

# ============================================================================
# Atomic write helper: requires non-empty tmp, then mv
# ============================================================================
atomic_move_nonempty () {
  local tmp="$1"
  local final="$2"
  if [ ! -s "$tmp" ]; then
    return 1
  fi
  mv -f "$tmp" "$final"
}

# ============================================================================
# Contract-governed error artifact writer (atomic, never 0 bytes)
# ============================================================================
write_service_error () {
  local reason="$1"
  local msg="$2"

  "$PY" - <<PY > "$ERROR_TMP" || true
import json, os
obj = {
  "record_type": "service_error",
  "service": os.environ.get("SERVICE"),
  "ts_utc": os.environ.get("TS_UTC"),
  "run_id": os.environ.get("RUN_ID"),
  "ok": False,
  "invalid": True,
  "reasons": ["$reason"],
  "message": "$msg",
  "logroot": os.environ.get("OP_LOGROOT"),
}
print(json.dumps(obj, indent=2, sort_keys=True))
PY

  if ! atomic_move_nonempty "$ERROR_TMP" "$ERROR_JSON"; then
    rm -f "$ERROR_TMP" >/dev/null 2>&1 || true
  fi
}

# If we fail before exec, we emit a service_error and clean up tmp files.
trap 'write_service_error "TOOL_EXIT_NONZERO" "run_media_webroot.sh failed before exec uvicorn"; cleanup_tmp' ERR

# ============================================================================
# Preflight checks (fail fast, explicit reason codes)
# ============================================================================
if [ ! -x "$PY" ]; then
  write_service_error "TOOL_PRECONDITION_FAILED" "python executable not found or not executable: PY=${PY}"
  cleanup_tmp
  exit 1
fi

if [ ! -d "$APP_DIR" ]; then
  write_service_error "TOOL_PRECONDITION_FAILED" "app dir missing: APP_DIR=${APP_DIR}"
  cleanup_tmp
  exit 1
fi

if [ ! -r "$APP_FILE" ]; then
  write_service_error "TOOL_PRECONDITION_FAILED" "app file missing or not readable: APP_FILE=${APP_FILE}"
  cleanup_tmp
  exit 1
fi

# Optional but useful: prove uvicorn import works under this interpreter.
# If you don't want this, delete this block.
"$PY" - <<'PY' >/dev/null 2>&1 || {
import uvicorn  # noqa: F401
PY
  write_service_error "TOOL_PRECONDITION_FAILED" "uvicorn not importable under PY=${PY}"
  cleanup_tmp
  exit 1
}

# ============================================================================
# Write service_runstate artifact atomically (contract-governed)
# ============================================================================
"$PY" - <<'PY' > "$RUNSTATE_TMP"
import json, os, pathlib, platform

def stat(p):
    p = pathlib.Path(p)
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    return {"exists": True, "size": int(st.st_size), "mtime_epoch": int(st.st_mtime)}

obj = {
  "record_type": "service_runstate",
  "service": os.environ.get("SERVICE"),
  "ts_utc": os.environ.get("TS_UTC"),
  "run_id": os.environ.get("RUN_ID"),
  "pid": os.getpid(),
  "cwd": os.getcwd(),
  "python_executable": os.environ.get("PY"),
  "python_version": platform.python_version(),
  "policy_flags": {
    "MEDIA_WEBROOT_READ_ONLY": os.environ.get("MEDIA_WEBROOT_READ_ONLY"),
    "MEDIA_WEBROOT_SNAPSHOT_ONLY": os.environ.get("MEDIA_WEBROOT_SNAPSHOT_ONLY"),
    "MEDIA_WEBROOT_NEVER_OPEN_CATALOG_DB": os.environ.get("MEDIA_WEBROOT_NEVER_OPEN_CATALOG_DB"),
  },
  "code_identity": {
    "app_file": os.environ.get("APP_FILE"),
    "app_sha256": os.environ.get("APP_SHA256"),
  },
  "key_path_stats": {
    "catalog_db": os.environ.get("CATALOG_DB"),
    "snapshot_db": os.environ.get("SNAPSHOT_DB"),
    "catalog_db_stat": stat(os.environ.get("CATALOG_DB")),
    "snapshot_db_stat": stat(os.environ.get("SNAPSHOT_DB")),
  },
  "command": [
    os.environ.get("PY"), "-m", "uvicorn", "webroot_server:app",
    "--host", "0.0.0.0", "--port", "3000",
    "--app-dir", os.environ.get("APP_DIR"),
    "--log-level", "info", "--access-log"
  ],
  "logroot": os.environ.get("OP_LOGROOT"),
}
print(json.dumps(obj, indent=2, sort_keys=True))
PY

if [ ! -s "$RUNSTATE_TMP" ]; then
  write_service_error "ATOMIC_WRITE_FAILED" "service_runstate temp file was empty"
  cleanup_tmp
  exit 1
fi

if ! atomic_move_nonempty "$RUNSTATE_TMP" "$RUNSTATE_JSON"; then
  write_service_error "ATOMIC_WRITE_FAILED" "service_runstate atomic move failed"
  cleanup_tmp
  exit 1
fi

# From here, wrapper succeeded. Disable ERR trap.
trap - ERR

log_line "SERVICE_START run_id=${RUN_ID} app_sha256=${APP_SHA256} snapshot_db=${SNAPSHOT_DB} catalog_db=${CATALOG_DB}"

# ============================================================================
# Start uvicorn (must be last; supervisor supervises this PID)
# ============================================================================
exec "$PY" -m uvicorn webroot_server:app \
  --host 0.0.0.0 --port 3000 \
  --app-dir "$APP_DIR" \
  --log-level info --access-log