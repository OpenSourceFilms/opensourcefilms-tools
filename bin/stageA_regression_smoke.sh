#!/usr/bin/env bash
# /workspace/pipeline/bin/stageA_regression_smoke.sh
#
# Runs Stage A regression smoke tests (speech + blank audio) and enforces:
# - Stage A completes successfully
# - Canonical ASR JSON schema_version == 2 for both fixtures
# - Contract checks are implicitly exercised (stageA_one.sh runs them)
#
# Exits non-zero on any failure.

set -uo pipefail

fail() { echo "FATAL: $*" >&2; exit 2; }

SPEECH="/workspace/footage/DJI/DJI_20250922193533_0014_D.MP4"
BLANK="/workspace/footage/DJI/DJI_20250922191721_0007_D.MP4"

[ -f "$SPEECH" ] || fail "missing speech fixture: $SPEECH"
[ -f "$BLANK" ] || fail "missing blank fixture: $BLANK"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/workspace/catalog/logs/stageA_regression_${TS}.log"
: > "$LOG"

echo "=== Stage A regression smoke ===" | tee -a "$LOG"
echo "TS=$TS" | tee -a "$LOG"

echo "--- preflight ---" | tee -a "$LOG"
/workspace/pipeline/bin/preflight_stageA.sh 2>&1 | tee -a "$LOG" || fail "preflight failed (see $LOG)"

echo "--- speech fixture ---" | tee -a "$LOG"
IN="$SPEECH" RUN_ID="stageA_reg_speech_${TS}" /workspace/pipeline/bin/stageA_one.sh 2>&1 | tee -a "$LOG" || fail "speech fixture failed (see $LOG)"
MID_SPEECH="$(sha256sum "$SPEECH" | awk '{print $1}')"
SV_SPEECH="$(python3 -c 'import json; import sys; print(json.load(open(sys.argv[1]))["schema_version"])' "/workspace/catalog/asr/${MID_SPEECH}.json" 2>/dev/null || true)"
[ "$SV_SPEECH" = "2" ] || fail "speech schema_version != 2 (got '$SV_SPEECH') for /workspace/catalog/asr/${MID_SPEECH}.json"

echo "--- blank fixture ---" | tee -a "$LOG"
IN="$BLANK" RUN_ID="stageA_reg_blank_${TS}" /workspace/pipeline/bin/stageA_one.sh 2>&1 | tee -a "$LOG" || fail "blank fixture failed (see $LOG)"
MID_BLANK="$(sha256sum "$BLANK" | awk '{print $1}')"
SV_BLANK="$(python3 -c 'import json; import sys; print(json.load(open(sys.argv[1]))["schema_version"])' "/workspace/catalog/asr/${MID_BLANK}.json" 2>/dev/null || true)"
[ "$SV_BLANK" = "2" ] || fail "blank schema_version != 2 (got '$SV_BLANK') for /workspace/catalog/asr/${MID_BLANK}.json"

echo "OK: regression smoke passed" | tee -a "$LOG"
echo "OK: log $LOG"
