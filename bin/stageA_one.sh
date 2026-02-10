#!/usr/bin/env bash
# /workspace/pipeline/bin/stageA_one.sh
#
# Stage A: One media file -> media manifest + extracted mono 16k wav + ASR artifacts (+ canonical ASR JSON + QC)
#
# This script is an orchestrator. Under the current logging protocol it must:
# - Emit operational logs to stdout/stderr only (captured by /logs.all wrapper)
# - NOT write ad hoc *.log files anywhere (especially not under catalog)
#
# Contracts:
# - media_id is sha256 of full input bytes (get_media_id from lib_paths.sh)
# - manifest written by make_manifest_one.sh (IN via env)
# - ASR via asr_whispercpp.sh (VAD-first, fallback to no-VAD on rc=12 or empty outputs)
# - canonical machine transcript JSON is ALWAYS schema v2 (including explicit empty transcript outcomes)
# - Stage A outputs validated by stageA_contract_check.py
#
# Outcome classes (must be explicit and contract-complete):
# 1) NORMAL_SPEECH:
#    - audio present
#    - wav/srt/vtt present and non-empty
#    - asr.json segments non-empty, empty_transcript=false
# 2) EMPTY_TRANSCRIPT:
#    - audio present
#    - wav present and non-empty
#    - srt/vtt present and non-empty (explicit placeholders)
#    - asr.json segments=[], empty_transcript=true, reason is contract-enumerated
# 3) NO_AUDIO_TRACK:
#    - no audio stream
#    - wav/srt/vtt not required (unless contract demands placeholders)
#    - asr.json segments=[], empty_transcript=true with reason NO_AUDIO_TRACK
#
set -uo pipefail

source /workspace/pipeline/bin/lib_safe.sh
source /workspace/pipeline/bin/lib_paths.sh

log_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log_info() { printf '%s %s\n' "stageA|INFO|$(log_ts)" "$*" >&2; }
log_warn() { printf '%s %s\n' "stageA|WARN|$(log_ts)" "$*" >&2; }
log_err()  { printf '%s %s\n' "stageA|ERROR|$(log_ts)" "$*" >&2; }

step_start() { printf '%s %s\n' "stageA|STEP|$(log_ts)" "start|$*" >&2; }
step_end()   { printf '%s %s\n' "stageA|STEP|$(log_ts)" "end|$*" >&2; }

fatal() {
  log_err "FATAL|$*"
  exit 2
}

drop_cache_for_in() {
  local why="$1"
  if [[ -n "${IN:-}" && -f "${IN:-}" ]]; then
    if [[ -x /workspace/pipeline/bin/fadvise_drop.py ]]; then
      log_info "cache_drop|why=${why}|in=${IN}"
      python3 /workspace/pipeline/bin/fadvise_drop.py "$IN" >/dev/null 2>&1 || true
    fi
  fi
}

has_audio_stream() {
  local in="$1"
  if command -v ffprobe >/dev/null 2>&1; then
    ffprobe -v error -select_streams a:0 -show_entries stream=codec_type -of csv=p=0 "$in" 2>/dev/null \
      | grep -q "^audio$"
    return $?
  fi
  ffmpeg -hide_banner -i "$in" 2>&1 | grep -q "Stream #.*: Audio:"
  return $?
}

# Contract-enumerated reasons (must match the contract enum set)
REASON_NO_AUDIO_TRACK="NO_AUDIO_TRACK"
REASON_SILENT_AUDIO="SILENT_AUDIO"
REASON_NON_SPEECH_AUDIO="NON_SPEECH_AUDIO"

write_empty_transcript_sidecars() {
  # Ensure non-empty placeholders that satisfy stageA_contract_check.py.
  # This is only valid in the "audio present but empty transcript" outcome class.
  log_info "empty_transcript|writing_placeholders|srt=${SRT}|vtt=${VTT}"

  cat > "$SRT" <<'EOF'
1
00:00:00,000 --> 00:00:00,001
[empty transcript]
EOF

  cat > "$VTT" <<'EOF'
WEBVTT

00:00:00.000 --> 00:00:00.001
[empty transcript]
EOF
}

write_asr_json_empty_transcript() {
  # Writes canonical ASR JSON schema v2 for the EMPTY_TRANSCRIPT outcome class.
  local outjson="$1"
  local mid="$2"
  local vad_used="$3"
  local reason="$4"
  local reason_raw="$5"

  python3 - "$outjson" "$mid" "$vad_used" "$reason" "$reason_raw" <<'PY'
import json, sys, time

outjson, mid, vad_used, reason, reason_raw = sys.argv[1:6]
created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

allowed = {"SILENT_AUDIO", "NON_SPEECH_AUDIO", "NO_AUDIO_TRACK"}
if reason not in allowed:
    reason = "NON_SPEECH_AUDIO"

payload = {
  "schema_version": 2,
  "media_id": mid,
  "engine": "whispercpp",
  "created_utc": created,
  "segments": [],
  "empty_transcript": True,
  "reason": reason,
  "reason_raw": (reason_raw or ""),
  "vad_used": (vad_used == "1"),
  "policy": {
    "allow_indexing": True,
    "allow_serving": True,
  },
  "quality": {
    "status": "fail",
    "reasons": ["ASR_EMPTY"],
    "stats": {
      "segments_raw": 0,
      "segments_kept": 0,
      "non_dialogue_tokens_removed_count": 0,
    },
  },
}

with open(outjson, "w", encoding="utf-8") as f:
  json.dump(payload, f, ensure_ascii=False, indent=2)
  f.write("\n")

print("stageA|INFO|wrote_asr_json_empty_transcript", outjson)
PY
}

write_asr_qc_json() {
  local outjson="$1"
  local vad_used="$2"
  local fallback="$3"
  local fallback_reason="$4"
  local pass1_rc="$5"
  local pass2_rc="$6"
  local backend_line="$7"

  python3 - "$outjson" "$MEDIA_ID" "$vad_used" "$fallback" "$fallback_reason" "$pass1_rc" "$pass2_rc" "$backend_line" <<'PY'
import json, sys, time
outjson, media_id, vad_used, fallback, fallback_reason, pass1_rc, pass2_rc, backend_line = sys.argv[1:9]
payload = {
  "media_id": media_id,
  "engine": "whispercpp",
  "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "vad_used": (vad_used == "1"),
  "vad_fallback": (fallback == "1"),
  "vad_fallback_reason": fallback_reason,
  "asr_pass1_rc": int(pass1_rc),
  "asr_pass2_rc": int(pass2_rc),
  "backend_line": (backend_line or ""),
  "backend_line_source": "stdout_capture",
}
with open(outjson, "w", encoding="utf-8") as f:
  json.dump(payload, f, ensure_ascii=False, indent=2)
  f.write("\n")
print("stageA|INFO|wrote_asr_qc_json", outjson)
PY
}

require_var IN || exit 2
if [[ ! -f "$IN" ]]; then
  fatal "IN does not exist: $IN"
fi

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

if command -v get_media_id >/dev/null 2>&1; then
  MEDIA_ID="$(get_media_id "$IN")" || fatal "get_media_id failed for IN=$IN"
else
  MEDIA_ID="$(sha256sum "$IN" | awk '{print $1}')" || fatal "sha256sum failed for IN=$IN"
fi

CATALOG_ROOT="${CATALOG_ROOT:-/workspace/catalog}"
ASR_DIR="${ASR_DIR:-${CATALOG_ROOT}/asr}"
MANIFEST_DIR="${MANIFEST_DIR:-${CATALOG_ROOT}/media_manifest}"
STATUS_DIR="${STATUS_DIR:-${CATALOG_ROOT}/status}"

mkdir -p "$ASR_DIR" "$MANIFEST_DIR" "$STATUS_DIR" || fatal "failed to create catalog directories"

MANIFEST_JSON="${MANIFEST_DIR}/${MEDIA_ID}.json"
STATUS_JSON="${STATUS_DIR}/${MEDIA_ID}.json"

WAV="${ASR_DIR}/${MEDIA_ID}.wav"
SRT="${ASR_DIR}/${MEDIA_ID}.srt"
VTT="${ASR_DIR}/${MEDIA_ID}.vtt"
ASR_JSON="${ASR_DIR}/${MEDIA_ID}.json"
ASR_QC_JSON="${ASR_DIR}/${MEDIA_ID}.qc.json"
ASR_OUTBASE="${ASR_DIR}/${MEDIA_ID}"

log_info "start|RUN_ID=${RUN_ID}|MEDIA_ID=${MEDIA_ID}|IN=${IN}|CATALOG_ROOT=${CATALOG_ROOT}"
log_info "paths|ASR_DIR=${ASR_DIR}|MANIFEST_DIR=${MANIFEST_DIR}|STATUS_DIR=${STATUS_DIR}"
log_info "artifacts|MANIFEST_JSON=${MANIFEST_JSON}|STATUS_JSON=${STATUS_JSON}|WAV=${WAV}|SRT=${SRT}|VTT=${VTT}|ASR_JSON=${ASR_JSON}|ASR_QC_JSON=${ASR_QC_JSON}"

drop_cache_for_in "after_media_id"

run_step status_stageA_start \
  /workspace/pipeline/bin/status_write.py \
  "$STATUS_JSON" "$MEDIA_ID" stageA 1 \
  "run_id=$RUN_ID" \
  "in=$IN" \
  "stage=stageA_start" \
  >/dev/null 2>&1 || true

# Preflight emitted to stdout/stderr only
step_start "preflight"
log_info "preflight|date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_info "preflight|hostname=$(hostname)"
log_info "preflight|pwd=$(pwd)"
log_info "preflight|whoami=$(whoami)"
log_info "preflight|uname=$(uname -a || true)"
log_info "preflight|df_begin"
df -h /workspace /tmp 2>/dev/null || true
log_info "preflight|df_end"
log_info "preflight|free_begin"
free -h 2>/dev/null || true
log_info "preflight|free_end"
log_info "preflight|ffmpeg_version_begin"
ffmpeg -version 2>/dev/null | head -n 3 || true
log_info "preflight|ffmpeg_version_end"
if /workspace/pipeline/third_party/whisper.cpp/build/bin/whisper-cli --help >/dev/null 2>&1; then
  log_info "preflight|whisper_cli=OK"
else
  log_warn "preflight|whisper_cli=NOT_RUNNABLE"
fi
python3 --version 2>/dev/null || true
step_end "preflight|rc=0"

# Manifest
step_start "manifest"
IN="$IN" /workspace/pipeline/bin/make_manifest_one.sh
rc=$?
step_end "manifest|rc=${rc}"
if [[ "$rc" -ne 0 ]]; then
  fatal "manifest step failed rc=$rc"
fi
if [[ ! -s "$MANIFEST_JSON" ]]; then
  fatal "manifest missing/empty: $MANIFEST_JSON"
fi

drop_cache_for_in "after_manifest"

# If no audio stream, skip ASR but still emit explicit ASR artifacts
if ! has_audio_stream "$IN"; then
  log_info "audio|no_audio_stream_detected|skipping_asr"

  rm -f "$ASR_JSON" "$ASR_QC_JSON" "$WAV" "$SRT" "$VTT" || true

  python3 - "$ASR_JSON" "$ASR_QC_JSON" "$MEDIA_ID" <<'PY'
import json, sys, time
asr_json, qc_json, mid = sys.argv[1:4]
created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

asr = {
  "schema_version": 2,
  "media_id": mid,
  "engine": "whispercpp",
  "created_utc": created,
  "segments": [],
  "empty_transcript": True,
  "reason": "NO_AUDIO_TRACK",
  "reason_raw": "no_audio_stream",
  "vad_used": False,
  "policy": {"allow_indexing": True, "allow_serving": True},
  "quality": {
    "status": "fail",
    "reasons": ["ASR_EMPTY"],
    "stats": {"segments_raw": 0, "segments_kept": 0, "non_dialogue_tokens_removed_count": 0},
  },
}

qc = {
  "media_id": mid,
  "engine": "whispercpp",
  "created_utc": created,
  "vad_used": False,
  "vad_fallback": False,
  "vad_fallback_reason": "no_audio_stream",
  "asr_pass1_rc": 0,
  "asr_pass2_rc": 0,
  "backend_line": "ASR: backend=SKIPPED (no audio stream)",
  "backend_line_source": "inline",
}

with open(asr_json, "w", encoding="utf-8") as f:
  json.dump(asr, f, ensure_ascii=False, indent=2); f.write("\n")
with open(qc_json, "w", encoding="utf-8") as f:
  json.dump(qc, f, ensure_ascii=False, indent=2); f.write("\n")

print("stageA|INFO|wrote_no_audio_asr", asr_json)
print("stageA|INFO|wrote_no_audio_qc", qc_json)
PY

  run_step status_asr_skipped_no_audio \
    /workspace/pipeline/bin/status_write.py \
    "$STATUS_JSON" "$MEDIA_ID" asr 1 \
    "run_id=$RUN_ID" \
    "skipped=true" \
    "reason=$REASON_NO_AUDIO_TRACK" \
    >/dev/null 2>&1 || true

  run_step status_stageA_done_no_audio \
    /workspace/pipeline/bin/status_write.py \
    "$STATUS_JSON" "$MEDIA_ID" stageA 1 \
    "run_id=$RUN_ID" \
    "stage=stageA_done" \
    "manifest_json=$MANIFEST_JSON" \
    "asr_skipped_no_audio=true" \
    "asr_json=$ASR_JSON" \
    "asr_qc_json=$ASR_QC_JSON" \
    >/dev/null 2>&1 || true

  log_info "done|outcome_class=NO_AUDIO_TRACK|MEDIA_ID=${MEDIA_ID}"
  exit 0
fi

# Extract audio
step_start "extract_audio"
rm -f "$WAV" || true
ffmpeg -hide_banner -y \
  -i "$IN" \
  -vn \
  -ac 1 \
  -ar 16000 \
  -c:a pcm_s16le \
  "$WAV"
rc=$?
step_end "extract_audio|rc=${rc}"
if [[ "$rc" -ne 0 ]]; then
  fatal "extract_audio failed rc=$rc"
fi
if [[ ! -s "$WAV" ]]; then
  fatal "extract_audio produced missing/empty WAV: $WAV"
fi

drop_cache_for_in "after_extract_audio"

VAD_FALLBACK=0
VAD_FALLBACK_REASON=""
VAD_USED=1
ASR_PASS1_RC=0
ASR_PASS2_RC=0
BACKEND_LINE=""

run_step status_asr_start \
  /workspace/pipeline/bin/status_write.py \
  "$STATUS_JSON" "$MEDIA_ID" asr 1 \
  "run_id=$RUN_ID" \
  "engine=whispercpp" \
  >/dev/null 2>&1 || true

# ASR pass1 (VAD enabled)
step_start "asr_pass1|ENABLE_VAD=1"
rm -f "$SRT" "$VTT" "$ASR_JSON" "$ASR_QC_JSON" || true

export ENABLE_VAD=1
/workspace/pipeline/bin/asr_whispercpp.sh "$WAV" "$ASR_OUTBASE"
ASR_PASS1_RC=$?
unset ENABLE_VAD
step_end "asr_pass1|rc=${ASR_PASS1_RC}"

if [[ "$ASR_PASS1_RC" -eq 12 ]]; then
  log_warn "asr_pass1|rc=12|treating_as_vad_empty_trigger_for_fallback"
  VAD_FALLBACK=1
  VAD_FALLBACK_REASON="vad_empty_transcript_rc12"
elif [[ "$ASR_PASS1_RC" -ne 0 ]]; then
  fatal "asr_pass1 failed rc=$ASR_PASS1_RC"
else
  if [[ ! -s "$SRT" || ! -s "$VTT" ]]; then
    log_warn "asr_pass1|empty_outputs|fallback_to_no_vad"
    VAD_FALLBACK=1
    VAD_FALLBACK_REASON="vad_empty_transcript"
  else
    VAD_FALLBACK=0
    VAD_FALLBACK_REASON=""
  fi
fi

# ASR pass2 (no VAD) if needed
if [[ "${VAD_FALLBACK}" -eq 1 ]]; then
  step_start "asr_pass2|ENABLE_VAD=0"
  VAD_USED=0
  rm -f "$SRT" "$VTT" || true

  export ENABLE_VAD=0
  /workspace/pipeline/bin/asr_whispercpp.sh "$WAV" "$ASR_OUTBASE"
  ASR_PASS2_RC=$?
  unset ENABLE_VAD
  step_end "asr_pass2|rc=${ASR_PASS2_RC}"

  if [[ "$ASR_PASS2_RC" -ne 0 ]]; then
    fatal "asr_pass2 failed rc=$ASR_PASS2_RC"
  fi
fi

# Decide between NORMAL_SPEECH and EMPTY_TRANSCRIPT.
if [[ ! -s "$SRT" || ! -s "$VTT" ]]; then
  # EMPTY_TRANSCRIPT outcome class.
  write_empty_transcript_sidecars

  rm -f "$ASR_JSON" || true

  EMPTY_REASON="$REASON_NON_SPEECH_AUDIO"
  if [[ "${VAD_FALLBACK_REASON}" == "vad_empty_transcript_rc12" || "${VAD_FALLBACK_REASON}" == "vad_empty_transcript" ]]; then
    EMPTY_REASON="$REASON_SILENT_AUDIO"
  fi

  write_asr_json_empty_transcript "$ASR_JSON" "$MEDIA_ID" "$VAD_USED" "$EMPTY_REASON" "empty_srt_vtt_from_whispercpp:${VAD_FALLBACK_REASON}"
  OUTCOME_CLASS="EMPTY_TRANSCRIPT"
else
  # NORMAL_SPEECH outcome class (normalize from SRT)
  step_start "normalize_srt_to_json"
  rm -f "$ASR_JSON" || true
  /workspace/pipeline/bin/asr_normalize_srt.py \
    --srt "$SRT" \
    --out "$ASR_JSON" \
    --media-id "$MEDIA_ID" \
    --vad-used "$VAD_USED"
  rc=$?
  step_end "normalize_srt_to_json|rc=${rc}"
  if [[ "$rc" -ne 0 ]]; then
    fatal "normalize_srt_to_json failed rc=$rc"
  fi
  if [[ ! -s "$ASR_JSON" ]]; then
    fatal "ASR_JSON missing/empty after normalization: $ASR_JSON"
  fi
  OUTCOME_CLASS="NORMAL_SPEECH"
fi

# QC JSON (backend_line is best-effort without ad hoc log files)
rm -f "$ASR_QC_JSON" || true
write_asr_qc_json "$ASR_QC_JSON" "$VAD_USED" "$VAD_FALLBACK" "$VAD_FALLBACK_REASON" "$ASR_PASS1_RC" "$ASR_PASS2_RC" "$BACKEND_LINE" || true

# Contract check
step_start "stageA_contract_check"
python3 /workspace/pipeline/bin/stageA_contract_check.py --in "$IN" --mid "$MEDIA_ID"
rc=$?
step_end "stageA_contract_check|rc=${rc}"
if [[ "$rc" -ne 0 ]]; then
  fatal "stageA_contract_check failed rc=$rc"
fi

run_step status_stageA_done \
  /workspace/pipeline/bin/status_write.py \
  "$STATUS_JSON" "$MEDIA_ID" stageA 1 \
  "run_id=$RUN_ID" \
  "stage=stageA_done" \
  "manifest_json=$MANIFEST_JSON" \
  "wav=$WAV" \
  "srt=$SRT" \
  "vtt=$VTT" \
  "asr_json=$ASR_JSON" \
  "asr_qc_json=$ASR_QC_JSON" \
  >/dev/null 2>&1 || true

log_info "done|outcome_class=${OUTCOME_CLASS}|MEDIA_ID=${MEDIA_ID}"
exit 0