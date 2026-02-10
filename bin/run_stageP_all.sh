#!/usr/bin/env bash
# Batch-generate MP4 proxies for every manifest in /workspace/catalog/media_manifest
set -euo pipefail

CATALOG_ROOT="${CATALOG_ROOT:-/workspace/catalog}"
MANIFEST_DIR="${CATALOG_ROOT}/media_manifest"
OUTDIR="${CATALOG_ROOT}/proxies"
STAGEP="/workspace/pipeline/bin/stageP_proxy_mp4.sh"

if [[ ! -x "$STAGEP" ]]; then
  echo "FAIL: stage script not executable: $STAGEP" >&2
  exit 2
fi
if [[ ! -d "$MANIFEST_DIR" ]]; then
  echo "FAIL: missing manifest dir: $MANIFEST_DIR" >&2
  exit 2
fi

mkdir -p "$OUTDIR"

total=0
ok=0
skipped=0
fail=0

echo "RUN_STAGEP_ALL: start"
echo "RUN_STAGEP_ALL: CATALOG_ROOT=$CATALOG_ROOT"
echo "RUN_STAGEP_ALL: MANIFEST_DIR=$MANIFEST_DIR"
echo "RUN_STAGEP_ALL: OUTDIR=$OUTDIR"
echo "RUN_STAGEP_ALL: STAGEP=$STAGEP"
echo "RUN_STAGEP_ALL: PROXY_HEIGHT=${PROXY_HEIGHT:-720}"
echo

# Deterministic ordering
mapfile -t manifests < <(find "$MANIFEST_DIR" -maxdepth 1 -type f -name "*.json" | sort)

for m in "${manifests[@]}"; do
  ((total+=1))
  mid="$(basename "$m" .json)"
  out="${OUTDIR}/${mid}.mp4"

  if [[ -s "$out" ]]; then
    echo "SKIP: proxy exists: $mid"
    ((skipped+=1))
    continue
  fi

  echo "DO: $mid"
  if MEDIA_ID="$mid" CATALOG_ROOT="$CATALOG_ROOT" PROXY_HEIGHT="${PROXY_HEIGHT:-720}" "$STAGEP" 1>/dev/null; then
    if [[ -s "$out" ]]; then
      echo "OK: $mid"
      ((ok+=1))
    else
      echo "FAIL: stage returned success but proxy missing: $mid" >&2
      ((fail+=1))
    fi
  else
    echo "FAIL: $mid" >&2
    ((fail+=1))
  fi
done

echo
echo "RUN_STAGEP_ALL: done"
echo "RUN_STAGEP_ALL: total=$total ok=$ok skipped=$skipped fail=$fail"

# Nonzero exit if anything failed, so callers can detect trouble.
if [[ "$fail" -ne 0 ]]; then
  exit 3
fi
