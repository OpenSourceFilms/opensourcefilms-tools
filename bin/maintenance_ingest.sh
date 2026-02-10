#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="media_webroot"
VENV_ACTIVATE="/workspace/venvs/cpu/bin/activate"
PIPELINE_DIR="/workspace/pipeline/bin"
INGEST_CMD="./stageB_one.sh"

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(timestamp)] $*"; }

usage() {
  cat <<'USAGE'
Usage:
  maintenance_ingest.sh <MEDIA_ID|MEDIA_ID.json> [KF_FPS]

Notes:
  - You may pass either the bare MEDIA_ID (recommended) or a filename ending in .json.
  - The script will normalize MEDIA_ID by stripping a trailing ".json" if present.

Examples:
  maintenance_ingest.sh 76d7f05592949a53c71b1dcbb57d4f53a6f84c4e3ce761040486fa026005fcfd
  maintenance_ingest.sh 76d7f05592949a53c71b1dcbb57d4f53a6f84c4e3ce761040486fa026005fcfd.json
  maintenance_ingest.sh 76d7f05592949a53c71b1dcbb57d4f53a6f84c4e3ce761040486fa026005fcfd 1
USAGE
}

if [[ "${1:-}" == "" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 1
fi

MEDIA_ID_RAW="$1"
MEDIA_ID="${MEDIA_ID_RAW%.json}"
KF_FPS="${2:-}"

log "Maintenance ingest starting"
log "Service: ${SERVICE_NAME}"
log "Pipeline dir: ${PIPELINE_DIR}"
log "Ingest cmd: ${INGEST_CMD}"
log "MEDIA_ID(raw): ${MEDIA_ID_RAW}"
log "MEDIA_ID(norm): ${MEDIA_ID}"

cd "${PIPELINE_DIR}"

if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  log "ERROR: venv not found at ${VENV_ACTIVATE}"
  exit 1
fi

# shellcheck disable=SC1090
source "${VENV_ACTIVATE}"
log "Python: $(which python) ($(python --version))"

export MEDIA_ID
if [[ -n "${KF_FPS}" ]]; then
  export KF_FPS
  log "KF_FPS: ${KF_FPS}"
else
  log "KF_FPS: (default, StageB uses 1)"
fi

log "Stopping ${SERVICE_NAME} (ok if already stopped)..."
supervisorctl stop "${SERVICE_NAME}" || true

status="$(supervisorctl status "${SERVICE_NAME}" || true)"
log "Supervisor status after stop: ${status}"

log "Running Stage B ingest..."
${INGEST_CMD}

log "Starting ${SERVICE_NAME}..."
supervisorctl start "${SERVICE_NAME}"

status="$(supervisorctl status "${SERVICE_NAME}" || true)"
log "Supervisor status after start: ${status}"

log "Maintenance ingest complete"
