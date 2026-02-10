# Stage A State Machine (Authoritative)

Stage A is an orchestration layer that produces a deterministic ASR layer with explicit handling of silence, VAD behavior, and schema stability.

Canonical semantics are carried only in:
- `/workspace/catalog/asr/{media_id}.json` (schema_version = 2)

SRT/VTT are presentation artifacts only.

## Inputs

- `IN` (absolute path to media file)
- `RUN_ID` optional (defaults to UTC timestamp)

## Identity

- `media_id = sha256(full input bytes)` (64 lowercase hex)

## Outputs (flat layout)

- Manifest: `/workspace/catalog/media_manifest/{media_id}.json`
- WAV: `/workspace/catalog/asr/{media_id}.wav`
- SRT: `/workspace/catalog/asr/{media_id}.srt`
- VTT: `/workspace/catalog/asr/{media_id}.vtt`
- ASR JSON (canonical): `/workspace/catalog/asr/{media_id}.json`
- ASR QC JSON: `/workspace/catalog/asr/{media_id}.qc.json`
- Status: `/workspace/catalog/status/{media_id}.json`

## State Machine

START
|
v
DERIVE_MEDIA_ID (sha256 full file)
|
v
WRITE_MANIFEST (make_manifest_one.sh)
|
v
EXTRACT_AUDIO (ffmpeg -> mono 16k wav)
|
v
ASR_PASS1_VAD (ENABLE_VAD=1, asr_whispercpp.sh)
|
| __ rc=0 and SRT/VTT non-empty --> NORMALIZE
|
____ rc=12 OR SRT/VTT empty --> ASR_PASS2_NO_VAD (fallback)
|
v
ASR_PASS2_NO_VAD (ENABLE_VAD=0)
|
v
NORMALIZE
|
v
CONTRACT_CHECK
|
v
DONE


### Notes on transitions

- **Fallback is triggered only by tool/output failure conditions**:
  - `rc == 12` (ASR wrapper contract: missing/empty SRT)
  - or missing/empty SRT/VTT after pass1
- Fallback is **not triggered by transcript content**.

## Normalization

Normalization is performed only by:

- `/workspace/pipeline/bin/asr_normalize_srt.py`

Rules:

- `[BLANK_AUDIO]` is treated as *non-dialogue* and is removed from machine segments.
- If the SRT contains only `[BLANK_AUDIO]` cues, canonical JSON must be:
  - `segments: []`
  - `empty_transcript: true`
  - `reason: "blank_audio"`
  - `non_dialogue_tokens_removed` contains `"BLANK_AUDIO"`

Canonical JSON schema:

- `schema_version: 2`
- `media_id`
- `engine`
- `created_utc`
- `segments[] {start,end,text}`
- `empty_transcript` (must equal `len(segments)==0`)
- `reason` (may be empty string)
- `vad_used`
- optional `non_dialogue_tokens_removed`

## Contract enforcement

Stage A must run:

- `/workspace/pipeline/bin/stageA_contract_check.py --in "$IN" --mid "$media_id"`

This enforces:

- required artifacts exist and are non-empty
- manifest media_id/source_path match
- ASR JSON schema_version == 2
- no `[BLANK_AUDIO]` inside machine segments
- empty_transcript consistency
- segment timestamp sanity

Any contract failure makes Stage A fail.

## Regression

Use:

- `/workspace/pipeline/bin/stageA_regression_smoke.sh`

It runs:
- one speech fixture
- one blank-audio fixture
- and asserts schema_version == 2 for both
