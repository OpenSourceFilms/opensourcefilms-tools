# Stage B.0 Chunking Contract (Canonical)

Authoritative date: 2026-01-23  
Stage: B.0 (Deterministic Transformer)  
Purpose: Convert canonical Stage A ASR JSON (schema v2) into canonical chunk JSONL for indexing/retrieval.

## Inputs

Canonical input artifact (required):
- /workspace/catalog/asr/{media_id}.json

Input schema requirements:
- Must pass `/workspace/pipeline/bin/stageA_contract_check.py` for the corresponding media.
- Must have `schema_version == 2`.

## Output

Canonical output artifact (required):
- /workspace/catalog/chunks/{media_id}.jsonl

Empty transcript case:
- Output file MUST exist and contain zero lines.

## Chunk JSONL schema (schema_version = 1)

Each line is one JSON object:

Required fields:
- schema_version: int (must be 1)
- media_id: string (64 hex)
- chunk_id: string (64 hex sha256)
- asr_schema_version: int (must be 2)
- asr_artifact_sha256: string (64 hex sha256 of the ASR JSON bytes)
- chunking_config_sha256: string (64 hex sha256 of canonical chunking config JSON)
- start_s: float (seconds from media start)
- end_s: float (seconds from media start)
- text: string (concatenation of included segment texts, single spaces between)
- source_segments: array[int] (indices into ASR segments[], increasing)

Forbidden:
- Empty chunks (no emitted record may have empty `text` after trimming)
- Missing required fields
- Non-monotonic or inverted times (end_s < start_s)

## Deterministic chunking rules

Defaults (schema v1):
- TARGET_CHUNK_S = 20.0
- MAX_CHUNK_S = 30.0
- SILENCE_GAP_S = 1.5
- MAX_CHARS = 800
- OVERLAP_S = 0.0 (disabled)

Source segments are processed in order and grouped into chunks.

A new chunk begins:
- at the start of processing, and
- whenever adding the next segment would violate a hard cap, or
- whenever a silence boundary triggers before the next segment.

Hard caps:
- MAX_CHUNK_S: do not exceed, except when a single ASR segment itself exceeds MAX_CHUNK_S, in which case that segment forms its own chunk.
- MAX_CHARS: do not exceed, except when a single ASR segment text itself exceeds MAX_CHARS, in which case that segment forms its own chunk.

Silence boundary:
- If next.start - current.end >= SILENCE_GAP_S, end the current chunk before next segment.

Coverage invariant (no-overlap mode):
- Every ASR segment index MUST appear in exactly one chunk when `segments` is non-empty.

## Identity and hashing

Canonical chunking config JSON is:

{
  "schema_version": 1,
  "target_chunk_s": 20.0,
  "max_chunk_s": 30.0,
  "silence_gap_s": 1.5,
  "max_chars": 800,
  "overlap_s": 0.0
}

- Serialize with sorted keys, no whitespace changes beyond standard JSON canonicalization.
- chunking_config_sha256 = sha256(canonical_json_bytes)

asr_artifact_sha256:
- sha256 of the exact bytes of the ASR JSON file as stored.

chunk_id:
- sha256 of a canonical JSON object containing:
  - media_id
  - asr_artifact_sha256
  - chunk_schema_version
  - chunking_config_sha256
  - start_s
  - end_s
  - source_segments

Text is excluded from chunk_id to avoid identity drift from trivial formatting changes.

## Normative enforcement

The authoritative gate is:
- /workspace/pipeline/bin/stageB0_contract_check.py

If docs and checker disagree, the checker wins.
