#!/usr/bin/env bash
# NOTE: this file is intended to be SOURCED by other scripts.
# Do not set -e here, it would leak errexit into the caller.
set -uo pipefail

# Shared catalog roots
CATALOG_ROOT="/workspace/catalog"
MANIFEST_DIR="${CATALOG_ROOT}/media_manifest"
ASR_DIR="${CATALOG_ROOT}/asr"
STATUS_DIR="${CATALOG_ROOT}/status"
LOG_DIR="${CATALOG_ROOT}/logs"

mkdir -p "$MANIFEST_DIR" "$ASR_DIR" "$STATUS_DIR" "$LOG_DIR"

# Deterministically derive media_id by running make_manifest_one.sh (idempotent).
# Contract:
#   PRINT_MEDIA_ID_ONLY=1 causes make_manifest_one.sh to print ONLY the 64-hex media_id.
#   (No "OK:" prefix in that mode.)
get_media_id() {
  local in_path="$1"

  # Deterministic, constant-memory media id.
  # Hashes file CONTENT (stable across renames/moves).
  if [[ ! -f "$in_path" ]]; then
    echo "FATAL: get_media_id input does not exist: $in_path" >&2
    return 2
  fi

  sha256sum "$in_path" | awk '{print $1}'
}
