#!/usr/bin/env bash
# run_tools.sh
# Purpose: Run non-canonical "tools" commands with rigorous, explicit outcome records.
# - Does NOT use set -e or set -u (per your terminal behaviour constraint)
# - Emits a JSON outcome record under /workspace/catalog/audit/tools_runs
# - Captures execution context (host, venv, python, gpu presence heuristic)
# - Never assumes success from absence; always writes a record

if [ "$#" -lt 4 ]; then
  echo "Usage:"
  echo "  run_tools.sh <tool_name> <description> -- <command...>"
  echo "Example:"
  echo "  run_tools.sh faster_whisper \"asr pass\" -- python -m faster_whisper --help"
  exit 2
fi

TOOL_NAME="$1"
DESCRIPTION="$2"
shift 2

if [ "$1" != "--" ]; then
  echo "Error: expected -- separator before command"
  exit 2
fi
shift 1

AUDIT_DIR="/workspace/catalog/audit/tools_runs"
mkdir -p "$AUDIT_DIR"

HOSTNAME="$(hostname 2>/dev/null)"
TS_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null)"
TS_ID="$(date -u +"%Y%m%dT%H%M%SZ" 2>/dev/null)"
RUN_ID="tools_${TOOL_NAME}_${TS_ID}"

# Detect venv path if active
VENV_PATH="${VIRTUAL_ENV:-null}"

# Python identity if available
PY_EXE="$(command -v python 2>/dev/null || true)"
PY_VER="$($PY_EXE --version 2>/dev/null | tr -d '\r' || true)"

# GPU heuristic: presence of nvidia-smi and at least one GPU listed
GPU_PRESENT="false"
GPU_NAME=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_LINE="$(nvidia-smi -L 2>/dev/null | head -n 1 | tr -d '\r')"
  if [ -n "$GPU_LINE" ]; then
    GPU_PRESENT="true"
    GPU_NAME="$GPU_LINE"
  fi
fi

# Derive a stable env_id from venv path + gpu presence.
# This prevents future ambiguity when multiple venvs/pods exist.
GPU_PREFIX="cpu"
if [ "$GPU_PRESENT" = "true" ]; then
  GPU_PREFIX="gpu"
fi

ENV_ID="${GPU_PREFIX}_unknown"
if [ "$VENV_PATH" = "/workspace/venvs/cpu" ]; then
  ENV_ID="${GPU_PREFIX}_canonical"
elif [ "$VENV_PATH" = "/workspace/venvs/cpu_vision" ]; then
  ENV_ID="${GPU_PREFIX}_vision_tools"
elif [ "$VENV_PATH" = "/workspace/venvs/text_tools" ]; then
  ENV_ID="${GPU_PREFIX}_text_tools"
fi

# Non-canonical compute flags: always true for this wrapper
NON_CANONICAL="true"
GPU_ACCELERATED="$GPU_PRESENT"
EXPERIMENTAL="true"

# Capture command as a single string for audit readability
CMD_STR=""
for arg in "$@"; do
  if [ -z "$CMD_STR" ]; then
    CMD_STR="$arg"
  else
    CMD_STR="$CMD_STR $(printf "%q" "$arg")"
  fi
done

OUT_JSON="${AUDIT_DIR}/${RUN_ID}.json"
OUT_LOG="${AUDIT_DIR}/${RUN_ID}.log"

# Run
START_EPOCH="$(date -u +%s 2>/dev/null)"
(
  echo "=== RUN_ID: $RUN_ID ==="
  echo "TS_UTC: $TS_UTC"
  echo "HOST: $HOSTNAME"
  echo "ENV_ID: $ENV_ID"
  echo "VENV: $VENV_PATH"
  echo "PYTHON: $PY_EXE"
  echo "PY_VER: $PY_VER"
  echo "GPU_PRESENT: $GPU_PRESENT"
  if [ -n "$GPU_NAME" ]; then echo "GPU_NAME: $GPU_NAME"; fi
  echo "TOOL: $TOOL_NAME"
  echo "DESC: $DESCRIPTION"
  echo "CMD: $CMD_STR"
  echo "=== BEGIN STDOUT/STDERR ==="
  "$@"
  EXIT_CODE="$?"
  echo "=== END STDOUT/STDERR ==="
  echo "EXIT_CODE: $EXIT_CODE"
  exit "$EXIT_CODE"
) >"$OUT_LOG" 2>&1
EXIT_CODE="$?"
END_EPOCH="$(date -u +%s 2>/dev/null)"
DURATION_S=""
if [ -n "$START_EPOCH" ] && [ -n "$END_EPOCH" ]; then
  DURATION_S=$((END_EPOCH - START_EPOCH))
else
  DURATION_S=""
fi

# Minimal JSON writer (no jq dependency)
# Note: we keep fields simple and deterministic.
cat >"$OUT_JSON" <<EOF
{
  "record_type": "tools_run_outcome",
  "run_id": "${RUN_ID}",
  "ts_utc": "${TS_UTC}",
  "tool_name": "${TOOL_NAME}",
  "description": "${DESCRIPTION}",
  "command": "${CMD_STR}",
  "exit_code": ${EXIT_CODE},
  "duration_s": ${DURATION_S},
  "execution": {
    "env_id": "${ENV_ID}",
    "host": "${HOSTNAME}",
    "venv_path": "${VENV_PATH}",
    "python": "${PY_EXE}",
    "python_version": "${PY_VER}",
    "gpu_present": ${GPU_PRESENT},
    "gpu_name": "${GPU_NAME}"
  },
  "flags": {
    "system.non_canonical_compute": ${NON_CANONICAL},
    "system.gpu_accelerated": ${GPU_ACCELERATED},
    "stage.experimental": ${EXPERIMENTAL}
  },
  "outputs": {
    "log_path": "${OUT_LOG}",
    "json_path": "${OUT_JSON}"
  }
}
EOF

echo "Wrote: $OUT_JSON"
echo "Log:   $OUT_LOG"
exit "$EXIT_CODE"