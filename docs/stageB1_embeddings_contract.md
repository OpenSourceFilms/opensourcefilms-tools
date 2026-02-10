# Stage B.1 Embeddings Contract (Canonical Artifact)

Authoritative date: 2026-01-27  
Stage: B.1 (Embeddings)  
Purpose: Generate canonical E5 embeddings for Stage B.0 chunk records and store them as rebuildable, on-disk artifacts.  
Scope: This contract describes the **filesystem source of truth** embeddings artifact, not Milvus.

---

## Inputs (required)

- `/workspace/catalog/chunks/{media_id}.jsonl`  (Stage B.0, schema_version=1)

If the chunks JSONL has 0 non-empty lines, Stage B.1 still writes an embeddings artifact with an empty `vectors` list.

---

## Output (required)

- `/workspace/catalog/embeddings/{media_id}.json`  (single JSON document)

This file is the canonical embedding artifact for the media_id.

---

## Canonical embedding space (locked)

All Stage B.1 embeddings must be in the canonical semantic space defined by MASTER.md:

- Model: `intfloat/e5-small-v2`
- Dimension: 384
- Prefix discipline:
  - Stored chunk embedding input must be prefixed with `passage: `
  - Query embedding input must be prefixed with `query: `
- Pooling: mean pooling over the attention mask (mean tokens)
- Normalization: L2 normalized
- MAX_LEN: 256

Mixing embedding spaces is invalid.

---

## Artifact schema (schema_version = 1)

Top-level JSON object required fields:

- `schema_version`: int (must be 1)
- `media_id`: string (64 hex)
- `model`: object with:
  - `model_id`: string (must be `intfloat/e5-small-v2`)
  - `model_revision`: string (non-empty label, ideally HF commit; acceptable alternative: deterministic local snapshot hash)
  - `dimension`: int (must be 384)
- `embed_config`: object with:
  - `normalize_embeddings`: bool (must be true for canonical space)
  - `max_seq_length`: int (must be 256)
  - `pooling`: string (must be `mean_attention`)
  - `prefix_passage`: string (must be `passage: `)
  - `prefix_query`: string (must be `query: `)
  - `batch_size`: int (recorded; does not affect meaning)
- `embed_config_sha256`: string (64 hex)  
  sha256 over a canonical JSON dump of `embed_config` with sorted keys and compact separators.
- `asr_schema_version`: int (current ASR schema version, recorded for traceability)
- `chunks_artifact_sha256`: string (64 hex)  
  sha256 of the input chunks JSONL file bytes.
- `created_utc`: string (RFC3339/ISO format, `...Z`)
- `runtime`: object with:
  - `python_version`: string
  - `torch_version`: string
  - `device`: string (`cpu` or `cuda`)
  - `venv`: string (explicit provenance label, required by policy)
  - `sentence_transformers_version`: string
  - `transformers_version`: string
  - `huggingface_hub_version`: string
- `vectors`: array of vector records (may be empty)

### Vector record schema

Each entry in `vectors[]` must contain:

- `chunk_id`: string (64 hex)
- `embedding_id`: string (64 hex) deterministic id for this embedding
- `start_s`: number (seconds, nullable if unknown)
- `end_s`: number (seconds, nullable if unknown)
- `text`: string (raw chunk text, unprefixed)
- `embed_text`: string (must equal `passage: ` + `text`)
- `vector`: array[number] length 384

---

## Determinism and identity

Notes:

- Bit-identical vectors are not guaranteed across CPU vs GPU or across different BLAS/CUDA kernels.
- Canonical meaning and compatibility are enforced by:
  - locked model_id + model_revision label
  - locked embed_config and embed_config_sha256
  - prefix discipline (`embed_text`)
  - stable linkage to `chunk_id` and `chunks_artifact_sha256`

### embedding_id (normative)

`embedding_id` must be deterministic, derived from:

- `chunk_id`
- `model_id`
- `model_revision`
- `embed_config_sha256`

(Exact concatenation / hashing format is an implementation detail, but must be stable.)

---

## Operational rules (important)

- Stage B.1 is an **offline batch** stage.
- It must be run under the **batch embed venv** (see MASTER.md).
- Provenance must not rely on ambient shell state. The venv label must be explicitly recorded (policy).

---

## Relationship to Milvus

- Milvus Lite is a derived cache.
- `moments.db` may be rebuilt entirely from:
  - `/workspace/catalog/chunks/{media_id}.jsonl`
  - `/workspace/catalog/embeddings/{media_id}.json`
- The embeddings artifact is the source of truth, not the database.

---

End of Stage B.1 embeddings contract
