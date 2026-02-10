#!/usr/bin/env bash
# /workspace/pipeline/bin/make_manifest_one.sh
set -uo pipefail

source /workspace/pipeline/bin/lib_safe.sh
source /workspace/pipeline/bin/lib_paths.sh

fatal() {
  echo "FATAL: $*" >&2
  exit 2
}

require_var IN
if [[ ! -f "$IN" ]]; then
  fatal "IN does not exist: $IN"
fi

# MEDIA_ID may be provided by caller to avoid redundant reads.
# If not provided, derive from file content (sha256 of full file, per lib_paths.sh).
MEDIA_ID="${MEDIA_ID:-$(get_media_id "$IN")}"

# Hard guards: MEDIA_ID must never be empty and must be a 64-hex sha256.
if [[ -z "${MEDIA_ID}" ]]; then
  fatal "MEDIA_ID resolved to empty string for IN=$IN"
fi
if [[ ! "${MEDIA_ID}" =~ ^[0-9a-f]{64}$ ]]; then
  fatal "MEDIA_ID is not a 64-hex sha256: MEDIA_ID='${MEDIA_ID}' IN=$IN"
fi

MANIFEST_JSON="${MANIFEST_DIR}/${MEDIA_ID}.json"
TMP_JSON="${MANIFEST_JSON}.tmp"

# Guard against the specific failure mode that created /media_manifest/.json
case "$MANIFEST_JSON" in
  */media_manifest/.json|*/media_manifest/.json/*|*/.json)
    fatal "Refusing to write manifest to suspicious path: MANIFEST_JSON=$MANIFEST_JSON (MEDIA_ID='${MEDIA_ID}')"
    ;;
esac

mkdir -p "$MANIFEST_DIR" || fatal "Failed to mkdir -p MANIFEST_DIR=$MANIFEST_DIR"

# ffprobe full json
PROBE_JSON="$(ffprobe -hide_banner -v error -print_format json -show_format -show_streams "$IN")"
if [[ -z "${PROBE_JSON}" ]]; then
  fatal "ffprobe returned empty output for IN=$IN"
fi

# Extract duration from ffprobe format.duration and coerce to float in python
DURATION_S="$(python3 - <<'PY' "$PROBE_JSON"
import json,sys
j=json.loads(sys.argv[1])
d=j.get("format",{}).get("duration",None)
try:
    d=float(d)
    if d<=0: raise ValueError()
    print(d)
except Exception:
    print("")
PY
)"

# Write manifest to a temp file, then atomically move into place
python3 - <<'PY' "$IN" "$MEDIA_ID" "$DURATION_S" "$PROBE_JSON" "$TMP_JSON"
import json,sys,time
src=sys.argv[1]
mid=sys.argv[2]
dur_raw=sys.argv[3]
probe_raw=sys.argv[4]
out=sys.argv[5]

duration_s=None
try:
    if dur_raw.strip():
        duration_s=float(dur_raw)
except Exception:
    duration_s=None

probe=json.loads(probe_raw)

manifest={
  "schema_version": 1,
  "ts_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
  "media_id": mid,
  "source_path": src,
  # Stable contract:
  "duration_s": duration_s,
  # Full ffprobe payload:
  "probe": probe,
}

with open(out, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(f"OK: media_id={mid}")
print(f"OK: wrote {out}")
PY

mv -f "$TMP_JSON" "$MANIFEST_JSON" || fatal "Failed to move TMP_JSON to MANIFEST_JSON: $TMP_JSON -> $MANIFEST_JSON"
echo "OK: manifest_json=$MANIFEST_JSON"
