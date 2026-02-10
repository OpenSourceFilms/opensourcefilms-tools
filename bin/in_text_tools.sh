#!/usr/bin/env bash
# in_text_tools.sh
# Purpose: venv guard for /workspace/venvs/text_tools that never closes the terminal.
# Behaviour:
# - If wrong venv: DO NOT run the command, write an explicit guard outcome record, exit 0.
# - If correct venv: delegate to run_tools.sh.

REQ_VENV="/workspace/venvs/text_tools"
AUDIT_DIR="/workspace/catalog/audit/tools_runs"
mkdir -p "$AUDIT_DIR" >/dev/null 2>&1

write_guard_record () {
  local tool_name="$1"
  local desc="$2"
  local cmd_str="$3"
  local active_venv="${VIRTUAL_ENV:-<none>}"

  local hostname ts_utc ts_id run_id out_json out_log
  hostname="$(hostname 2>/dev/null)"
  ts_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)"
  ts_id="$(date -u +"%Y%m%dT%H%M%SZ" 2>/dev/null)"
  run_id="guard_${tool_name}_${ts_id}"
  out_json="${AUDIT_DIR}/${run_id}.json"
  out_log="${AUDIT_DIR}/${run_id}.log"

  {
    echo "=== RUN_ID: ${run_id} ==="
    echo "TS_UTC: ${ts_utc}"
    echo "HOST: ${hostname}"
    echo "GUARD: venv_mismatch"
    echo "REQUIRED_VENV: ${REQ_VENV}"
    echo "ACTIVE_VENV: ${active_venv}"
    echo "TOOL: ${tool_name}"
    echo "DESC: ${desc}"
    echo "CMD: ${cmd_str}"
    echo "RESULT: blocked"
    echo "EXIT_CODE: 0"
  } >"$out_log" 2>&1

  cat >"$out_json" <<EOF
{
  "record_type": "tools_run_outcome",
  "run_id": "${run_id}",
  "ts_utc": "${ts_utc}",
  "tool_name": "${tool_name}",
  "description": "${desc}",
  "command": "${cmd_str}",
  "exit_code": 0,
  "execution": {
    "env_id": "guard_cpu_text_tools",
    "host": "${hostname}",
    "venv_path": "${active_venv}",
    "required_venv_path": "${REQ_VENV}"
  },
  "flags": {
    "system.non_canonical_compute": true,
    "system.gpu_accelerated": false,
    "stage.experimental": true
  },
  "reason_code": "wrong_execution_env",
  "outputs": {
    "log_path": "${out_log}",
    "json_path": "${out_json}"
  }
}
EOF

  echo "Guard blocked run, wrote: $out_json"
  echo "Guard log: $out_log"
}

if [ -z "${VIRTUAL_ENV:-}" ] || [ "$VIRTUAL_ENV" != "$REQ_VENV" ]; then
  cmd_str=""
  for arg in "$@"; do
    if [ -z "$cmd_str" ]; then
      cmd_str="$arg"
    else
      cmd_str="$cmd_str $(printf "%q" "$arg")"
    fi
  done

  echo "Error: wrong venv active."
  echo "Required: $REQ_VENV"
  echo "Active:   ${VIRTUAL_ENV:-<none>}"
  echo "Not running command."
  write_guard_record "text_tools_guard" "blocked run due to wrong venv" "$cmd_str"
  exit 0
fi

/workspace/pipeline/bin/run_tools.sh "$@"
exit 0