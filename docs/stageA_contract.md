# Stage A Contract (ASR / Audio Ingest)

This document is the authoritative contract for **Stage A** of the Media Intelligence Pipeline.

Stage A’s job is to produce a deterministic, auditable ASR layer that can be indexed downstream (Milvus, Resolve markers, editorial tooling). Silence and “no speech” are valid outcomes and must not be treated as failure.

## Scope

Stage A is responsible for:

- Computing `media_id` as **SHA256 of full input file bytes**
- Writing a media manifest
- Extracting mono 16 kHz PCM WAV audio
- Running ASR via whisper.cpp (VAD-first with safe fallback)
- Writing canonical ASR JSON (machine-readable transcript layer)
- Writing ASR QC JSON
- Writing Stage A status

Stage A is not responsible for:

- Semantic indexing
- Diarization
- Editorial decisions
- Video modification
- Assuming speech exists

## Identity

### media_id

- `media_id` is the **SHA256 hash of the full input media file bytes**
- Format: **64 lowercase hex characters** (`[0-9a-f]{64}`)
- This ID is stable across renames/moves and must be the primary key for downstream indexing.

Derivation is implemented by:

- `/workspace/pipeline/bin/lib_paths.sh:get_media_id()` (sha256sum of full file)

## Inputs

Stage A requires:

- `IN` (environment variable): absolute path to input media file

Optional:

- `RUN_ID` (environment variable): defaults to UTC timestamp if not provided

## Outputs

All outputs are written in a **flat layout** and are keyed by `media_id`.

### Manifest

- Path: `/workspace/catalog/media_manifest/{media_id}.json`
- Producer: `/workspace/pipeline/bin/make_manifest_one.sh`
- Requirements:
  - Must exist and be non-empty
  - Must contain:
    - `media_id` matching filename
    - `source_path` exactly equal to `IN`

### Audio extract

- Path: `/workspace/catalog/asr/{media_id}.wav`
- Mono, 16 kHz, PCM S16LE
- Must exist and be non-empty

### Subtitle artifacts (human-readable)

- `/workspace/catalog/asr/{media_id}.srt`
- `/workspace/catalog/asr/{media_id}.vtt`

These are considered **presentation artifacts**, not canonical semantics. They may include tokens like `[BLANK_AUDIO]`.

They must exist and be non-empty after ASR (including fallback), or Stage A fails.

### Canonical ASR JSON (machine-readable)

- Path: `/workspace/catalog/asr/{media_id}.json`
- Producer: `/workspace/pipeline/bin/asr_normalize_srt.py`
- Must exist and be non-empty
- Must validate via Stage A contract check

#### ASR JSON schema (current)

- `schema_version`: **2**
- `media_id`: 64-hex sha256
- `engine`: string (current: `whispercpp`)
- `created_utc`: RFC3339-ish UTC timestamp `YYYY-MM-DDTHH:MM:SSZ`
- `segments`: list of segments
  - each segment has:
    - `start`: float seconds
    - `end`: float seconds
    - `text`: string
- `empty_transcript`: boolean
- `reason`: string (may be empty string)
- `vad_used`: boolean
- optional `non_dialogue_tokens_removed`: list of strings

#### Semantic rules

- `[BLANK_AUDIO]` is **not speech** and must never appear in `segments[*].text`
- If SRT/VTT contain only `[BLANK_AUDIO]` cues, the canonical ASR JSON must be:
  - `segments: []`
  - `empty_transcript: true`
  - `reason: "blank_audio"`
  - `non_dialogue_tokens_removed` includes `"BLANK_AUDIO"`

### ASR QC JSON

- Path: `/workspace/catalog/asr/{media_id}.qc.json`
- Producer: embedded QC writer in Stage A
- Contains:
  - `vad_used`, `vad_fallback`, fallback reason
  - ASR pass return codes
  - backend line (best-effort)

### Status JSON

- Path: `/workspace/catalog/status/{media_id}.json`
- Producer: `/workspace/pipeline/bin/status_write.py`
- Must exist and be non-empty

## ASR Engine and Fallback Policy

ASR is run via:

- `/workspace/pipeline/bin/asr_whispercpp.sh`

Policy:

1) Pass 1: VAD enabled (`ENABLE_VAD=1`)
- If rc == 0 and SRT/VTT are present, keep result.
- If rc == 12 or SRT/VTT are missing/empty, trigger fallback.

2) Pass 2: VAD disabled (`ENABLE_VAD=0`)
- Must succeed (rc == 0)
- Must yield SRT/VTT (can include `[BLANK_AUDIO]`)

Canonical transcript semantics are derived only from normalization (`asr_normalize_srt.py`), not from file size.

## Contract Enforcement

Stage A must run:

- `/workspace/pipeline/bin/stageA_contract_check.py --in "$IN" --mid "$media_id"`

It enforces, at minimum:

- required artifacts exist and are non-empty
- `manifest.media_id` and `manifest.source_path` match
- ASR JSON schema version lock (`schema_version == 2`)
- `empty_transcript` is consistent with segment count
- no `[BLANK_AUDIO]` in machine segments
- timestamp sanity for each segment

If contract check fails, Stage A is considered failed.

## Preflight

- `/workspace/pipeline/bin/preflight_stageA.sh` must pass before batch runs.
- Preflight checks:
  - ffmpeg/ffprobe/python3 available
  - whisper-cli exists
  - ggml model exists
  - Silero VAD model exists
  - normalizer and contract checker scripts exist and run

## Regression Smoke Test

- `/workspace/pipeline/bin/stageA_regression_smoke.sh`
- Runs:
  - a known “speech” clip
  - a known “blank/no-speech” clip
- Must pass prior to any production batch run after code changes

## Non-negotiables

- Stage A must never silently write artifacts with an empty `media_id`
- Silence / no speech is valid and must not be treated as failure
- Downstream indexing must rely on canonical ASR JSON only
- Any schema change must bump `schema_version` and be reflected in contract check
