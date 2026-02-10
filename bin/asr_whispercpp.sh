#!/usr/bin/env bash
# /workspace/pipeline/bin/asr_whispercpp.sh
set -uo pipefail

# Usage:
#   asr_whispercpp.sh <input_audio_or_video> <out_base_path_without_ext>
#
# Writes (on success):
#   <out>.srt
#   <out>.vtt
#
# Design goals:
# - Deterministic, conservative ASR
# - Optional VAD gating to reduce hallucinations on noise
# - Loud, early failures on missing binaries/models
# - No silent guessing, silence is allowed
#
# Controls:
#   ENABLE_VAD=1|0        (default 1)
#   WHISPER_THREADS=4     (default 4)

IN="${1:?input path required}"
OUTBASE="${2:?output base path required}"

BIN="/workspace/pipeline/third_party/whisper.cpp/build/bin/whisper-cli"
MODEL="/workspace/pipeline/models/whispercpp/ggml-small.en.bin"

# Whisper.cpp Silero VAD (ggml)
VAD_MODEL="${VAD_MODEL:-/workspace/pipeline/third_party/whisper.cpp/models/for-tests-silero-v6.2.0-ggml.bin}"

# -----------------------------
# Preflight checks (authoritative)
# -----------------------------

if [[ ! -x "$BIN" ]]; then
  echo "ERROR: whisper.cpp binary not found or not executable: $BIN" >&2
  echo "HINT: expected build output under /workspace/pipeline/third_party/whisper.cpp/build/bin" >&2
  exit 2
fi

if [[ ! -f "$MODEL" ]]; then
  echo "ERROR: whisper.cpp model not found: $MODEL" >&2
  echo "HINT: expected ggml model under /workspace/pipeline/models/whispercpp" >&2
  exit 2
fi

ENABLE_VAD="${ENABLE_VAD:-1}"
if [[ "$ENABLE_VAD" != "0" && "$ENABLE_VAD" != "1" ]]; then
  echo "ERROR: ENABLE_VAD must be 0 or 1, got: $ENABLE_VAD" >&2
  exit 2
fi

if [[ "$ENABLE_VAD" = "1" ]]; then
  if [[ ! -f "$VAD_MODEL" ]]; then
    echo "ERROR: VAD model not found: $VAD_MODEL" >&2
    echo "HINT: expected Silero ggml VAD model from whisper.cpp" >&2
    exit 2
  fi
fi

echo "ASR: using BIN=$BIN" >&2
echo "ASR: using MODEL=$MODEL" >&2
echo "ASR: ENABLE_VAD=$ENABLE_VAD" >&2
if [[ "$ENABLE_VAD" = "1" ]]; then
  echo "ASR: using VAD_MODEL=$VAD_MODEL" >&2
fi

# -----------------------------
# Conservative, CPU-first policy
# -----------------------------

THREADS="${WHISPER_THREADS:-4}"

# VAD parameters (configurable, pinned defaults)
VAD_THRESHOLD="${VAD_THRESHOLD:-0.50}"
VAD_MIN_SPEECH_MS="${VAD_MIN_SPEECH_MS:-250}"
VAD_MIN_SILENCE_MS="${VAD_MIN_SILENCE_MS:-100}"
VAD_SPEECH_PAD_MS="${VAD_SPEECH_PAD_MS:-30}"
VAD_OVERLAP_S="${VAD_OVERLAP_S:-0.10}"

# Decoder safety rails (apply regardless of VAD)
NO_FALLBACK="${NO_FALLBACK:-1}"
ENTROPY_THOLD="${ENTROPY_THOLD:-2.40}"
LOGPROB_THOLD="${LOGPROB_THOLD:--1.00}"
NO_SPEECH_THOLD="${NO_SPEECH_THOLD:-0.60}"

# -----------------------------
# Run whisper.cpp
# -----------------------------

# Build optional VAD flag list without eval
VAD_ARGS=()
if [[ "$ENABLE_VAD" = "1" ]]; then
  VAD_ARGS+=(
    --vad
    --vad-model "$VAD_MODEL"
    --vad-threshold "$VAD_THRESHOLD"
    --vad-min-speech-duration-ms "$VAD_MIN_SPEECH_MS"
    --vad-min-silence-duration-ms "$VAD_MIN_SILENCE_MS"
    --vad-speech-pad-ms "$VAD_SPEECH_PAD_MS"
    --vad-samples-overlap "$VAD_OVERLAP_S"
  )
fi

# Decoder safety rails as a separate list (keeps intent clear)
SAFE_ARGS=(
  --entropy-thold "$ENTROPY_THOLD"
  --logprob-thold "$LOGPROB_THOLD"
  --no-speech-thold "$NO_SPEECH_THOLD"
)
if [[ "$NO_FALLBACK" = "1" ]]; then
  SAFE_ARGS+=(--no-fallback)
fi

# Capture stderr so we can log which backend was actually used.
BACKEND_LOG="$(mktemp -t whisper_backend.XXXXXX)"
cleanup_backend_log() { rm -f "$BACKEND_LOG" >/dev/null 2>&1 || true; }
trap cleanup_backend_log EXIT

"$BIN" \
  -m "$MODEL" \
  -f "$IN" \
  -t "$THREADS" \
  "${VAD_ARGS[@]}" \
  "${SAFE_ARGS[@]}" \
  --output-srt \
  --output-vtt \
  --print-progress \
  -of "$OUTBASE" \
  2> >(tee "$BACKEND_LOG" >&2)

rc=$?
if [[ "$rc" -ne 0 ]]; then
  echo "ERROR: whisper-cli failed rc=$rc" >&2
  exit "$rc"
fi

# Summarize actual backend used (best effort based on whisper.cpp logs).
if grep -q "no GPU found" "$BACKEND_LOG"; then
  echo "ASR: backend=CPU (no GPU found)" >&2
elif grep -q "device 0: CPU" "$BACKEND_LOG"; then
  echo "ASR: backend=CPU" >&2
else
  echo "ASR: backend=Unknown (no backend line matched)" >&2
fi

# -----------------------------
# Output contract enforcement
# -----------------------------

if [[ ! -s "${OUTBASE}.vtt" ]]; then
  echo "ERROR: ASR produced missing/empty VTT: ${OUTBASE}.vtt" >&2
  exit 11
fi

if [[ ! -s "${OUTBASE}.srt" ]]; then
  echo "ERROR: ASR produced missing/empty SRT: ${OUTBASE}.srt" >&2
  exit 12
fi

echo "OK: wrote ${OUTBASE}.srt and ${OUTBASE}.vtt"
