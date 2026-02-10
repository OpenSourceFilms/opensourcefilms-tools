MASTER.md

Version: 2.7
Evidence horizon: validated through 2026-02-03 (UTC), re-validation scheduled 2026-02-04 (UTC)

====================================================================
CANONICAL ROLE OF THIS DOCUMENT
This document defines:
• Pipeline purpose and guarantees
• Stage responsibilities and boundaries
• Non-negotiable invariants
• Execution and governance rules

This document is canonical. All other pipeline documentation MUST conform to this document.

====================================================================
PIPELINE INTENT
The pipeline exists to deterministically transform media inputs into:
• Searchable semantic representations
• Editorially useful artifacts
• Auditable, provenance-safe records

The pipeline prioritises, in strict order:
Explicit outcomes over inferred state
Determinism over convenience
Traceability over compactness
Offline correctness over online availability

No stage or service may rely on absence, omission, or emptiness to convey meaning.

The pipeline treats unedited, continuous footage as a first-class and expected input.

====================================================================
CANONICAL CODE SURFACE (IMPLEMENTATION LOCATION)
Canonical stage implementations currently live under:
• /workspace/pipeline/bin

/workspace/pipeline/stages is reserved for possible future migration. Until it exists and is adopted, it is NOT a canonical surface.

Files with names like -CopyN. are treated as non-canonical backups and MUST NOT be used as entrypoints.

====================================================================
CORE INVARIANTS (NON-NEGOTIABLE)

Explicit outcomes only
Every stage invocation MUST explicitly record:
• What was attempted
• What was produced
• Why nothing was produced, if applicable

Absence is never a signal. Missing files MUST NOT be used to infer success or failure.
Zero-byte files are prohibited unless explicitly defined as valid artifacts.

MEDIA_ID scoping
All artifacts are MEDIA_ID-scoped unless explicitly declared otherwise by the stage definition.
Exceptions MUST be explicit and documented in this file.

Atomicity
All JSON writes MUST be atomic (temporary file followed by replace).
Partial writes are forbidden.
If a write cannot complete, an explicit error artifact MUST be written instead.

Single-writer principle
Each write surface has exactly one canonical writer.

Exclusive access
Artifacts requiring exclusive access (e.g. local databases) MUST NOT be opened by multiple processes concurrently.
Any stage requiring such access MUST explicitly enforce it.

Readiness is not liveness
Service “RUNNING” state, process existence, and port listeners are not readiness signals.
Readiness MUST be determined only by explicit readiness endpoints (e.g. /api/health returning 200 with ok=true).
Callers MUST poll readiness, not guess delays.

====================================================================
CANONICAL OPERATIONAL LOGGING (SINGLE ROOT)

Canonical operational log root (the only allowed operational write surface):
• /workspace/pipeline/logs.all

All components, including stages, orchestration, watchers, and online services, MUST write all operational logs and runstate only under /workspace/pipeline/logs.all.

Operational logs and runstate MUST NOT be written under any other paths (including /workspace/logs and /workspace/runstate). Any such writes are defects.

Operational logging requirements:
• Logs MUST be sufficient to answer any question about what ran, when, why, with what inputs, producing what outputs.
• All operational writes MUST be atomic or append-only JSONL with explicit event envelopes.
• Zero-byte operational log files are prohibited unless explicitly defined as valid.
• Each stage invocation MUST have a stable log directory under logs.all that includes:
  – per-stage stdout/stderr capture
  – per-stage meta (exit_code, duration, cmd, log_sha256, log_bytes)
  – the run_id and media_id

Decision truthfulness requirement:
• “Compliant, nothing to do” and “ineligible, intentionally skipped” MUST be represented as distinct reason codes.
• Aggregated counters MUST count them separately.
• start_stage="NONE" MUST NOT be treated as meaningfully uniform. Reason codes are authoritative.

====================================================================
COPYPARTY ACCESS AND FRESHNESS GUARANTEE

The logs.all folder MUST be readable via Copyparty without stubs, caching ambiguity, or partial content.

The system MUST maintain explicit freshness and integrity signals under logs.all:
• /workspace/pipeline/logs.all/_latest/heartbeat.json
• /workspace/pipeline/logs.all/manifest/latest.json

heartbeat.json requirements:
• Updated at least once per watcher scan loop.
• Contains scan_id, ts_utc, and summary counters.

manifest/latest.json requirements:
• Contains sha256 and byte-length for:
  – heartbeat.json
  – the primary daemon event stream(s)
  – the most recent stage log written
  – the current versions of the master documents (including their sha256)
• Serves as the authoritative anti-stub integrity reference.

Verification requirement:
• Any automation or operator verification MUST fetch heartbeat.json and manifest/latest.json via Copyparty using a no-cache query parameter.
• Content-length and sha256 MUST be validated against manifest values when diagnosing staleness or correctness.
• Failure of freshness or integrity verification MUST produce an explicit operational error record under logs.all.

====================================================================
ARTIFACT FINGERPRINTING (DEPRECATED-TOOL DETECTION)

Canonical artifacts MUST be able to declare, explicitly and uniformly, whether they were produced by a deprecated tool version or semantic configuration.

For each stage that produces a canonical artifact, the stage MUST record an explicit artifact_fingerprint in the canonical output:
• For single-JSON artifacts: as a top-level field "artifact_fingerprint".
• For JSONL artifacts: in the stage outcome record (the last record by contract).

artifact_fingerprint MUST be schema-versioned and MUST include at minimum:
• schema_version
• stage
• producer (entrypoint name)
• producer_sha256 (sha256 of the producing stage script bytes)
• fingerprint_id (stable string identifying the semantic method family)
• fingerprint_version (stable string identifying the semantic method revision)
• config_sha256 (sha256 of a canonical config blob defining semantic meaning)
• config (the canonical config blob itself)

Consumers and services MUST treat missing or mismatched artifact_fingerprint as meaningfully deprecated and MUST NOT infer currency from partial cues.
Successful retroactive application of a valid artifact_fingerprint immediately restores artifact currency without requiring downstream invalidation.

====================================================================
CANONICAL VS DERIVED SEMANTICS (POLICY)

This pipeline makes a strict distinction between canonical semantic truth and derived or accelerated representations.

Canonical semantics:
• Are explicitly recorded, auditable JSON artifacts.
• Are deterministic given identical inputs and configuration.
• Are the only source of truth for meaning, evidence, and provenance.
• MUST be sufficient to reconstruct all higher-level interpretations.

Derived semantics:
• Exist to accelerate querying, navigation, or visualization.
• MUST be fully reconstructable from canonical artifacts.
• MUST NOT introduce new meaning not present in canonical records.
• MUST NOT be required for pipeline correctness.

Indexing and search policy:
• No raw or intermediate artifact may be indexed unless explicitly declared canonical for indexing.
• Stage F0 entity mentions are canonical evidence artifacts but are NOT canonical indexing surfaces.
• Stage F1 entity nodes are the canonical entity abstraction intended for indexing and retrieval.
• Stage C vector materialisation is a derived acceleration layer only.

Any consumer, service, or script that indexes or searches semantic content MUST do so using canonical indexing surfaces (e.g. Stage F1 outputs), not raw mention layers.

Violations of this policy are considered pipeline defects, not usage errors.

====================================================================
STAGE OVERVIEW
The pipeline is divided into explicit, named stages.
Stages may be executed individually or via batch orchestration, but stage responsibilities and guarantees do not change based on invocation method.

====================================================================
STAGE A — INGEST AND ASR
Responsibilities:
• Media normalization
• Audio extraction
• Speech-to-text
• Initial quality control
• Media manifest creation

Guaranteed outputs:
• media_manifest/.json
• asr/.json
• asr/.qc.json
• status/.json

Conditional required outputs (based on detected audio stream):
• If the input has an audio stream:
  – asr/.wav
  – asr/.srt
  – asr/.vtt
• If the input has no audio stream:
  – asr/.wav is not required
  – asr/.srt is not required
  – asr/.vtt is not required

Rules:
• Stage A MUST emit artifacts even when no audio or no speech is detected.
• Silent or non-speech media MUST result in explicit QC outcomes.
• ASR reason codes MUST use the enumerated contract vocabulary (e.g. NO_AUDIO_TRACK, SILENT_AUDIO, NON_SPEECH_AUDIO).
• Traceability fields such as reason_raw MAY exist, but MUST NOT replace enumerated reason codes.
• Any failure MUST be recorded explicitly in both QC and status artifacts.
• Stage A outputs MUST include artifact_fingerprint in the canonical ASR JSON and/or status artifact once implemented. Until implemented, Stage A artifacts without artifact_fingerprint MUST be treated as eligible for reprocessing under the deprecated-tool policy.

Outcome completeness requirements (explicit, non-inferential):
• NORMAL_SPEECH (audio present, speech present):
  – asr/.wav non-empty
  – asr/.srt non-empty
  – asr/.vtt non-empty
  – asr/.json schema v2 with segments non-empty and empty_transcript=false
• EMPTY_TRANSCRIPT (audio present, no speech detected, or transcript empty after all passes):
  – asr/.wav non-empty
  – asr/.srt MUST exist and MUST be non-empty (explicit placeholder allowed)
  – asr/.vtt MUST exist and MUST be non-empty (explicit placeholder allowed)
  – asr/.json schema v2 with segments=[], empty_transcript=true
  – asr/.json MUST include an enumerated reason code (SILENT_AUDIO or NON_SPEECH_AUDIO)
  – qc and status MUST explicitly record the empty outcome and any fallback path taken
• NO_AUDIO_TRACK (no audio stream):
  – asr/.json schema v2 with segments=[], empty_transcript=true and reason=NO_AUDIO_TRACK
  – asr/.qc.json MUST exist and MUST explicitly record the no-audio outcome
  – wav, srt, vtt are not required and MUST NOT be required by contract in this case

====================================================================
STAGE B — VISUAL ENRICHMENT
Responsibilities:
• Keyframe extraction
• Visual metadata enrichment

Guaranteed outputs:
• frames//frame_*.jpg
• enrich/.json
• status/.json

Rules:
• Absence of frames or enrichable content MUST be explicitly recorded.
• Stage B MUST NOT silently skip outputs.

====================================================================
STAGE B2 — MOTION (OPTICAL FLOW SUMMARY)
Responsibilities:
• Compute deterministic motion summaries (CPU optical flow)
• Produce motion statistics suitable for indexing and downstream temporal segmentation

Guaranteed outputs:
• motion/.json

Rules:
• motion/.json MUST exist after invocation.
• A “no frames” or “too short” outcome is valid and MUST be explicit in the artifact.
• The artifact MUST include config and runtime identity (python, opencv, numpy).
• Zero-byte motion files are prohibited.

====================================================================
STAGE B3 — TEMPORAL SEGMENTATION (SCENES / STRUCTURE)
Responsibilities:
• Produce explicit temporal segmentation for editorial and semantic use
• Incorporate motion, duration, and optional cut/transition signals
• Represent continuous, unedited footage explicitly and correctly

Guaranteed outputs:
• scenes/.json

Rules:
• scenes/.json MUST exist after invocation.
• A 1-segment result is a valid OK outcome for unedited single-take footage.
• Cut detection is optional and MUST NOT be assumed to be present or meaningful.
• The artifact MUST include detector config and runtime identity (python, torch, transnetv2-pytorch, weights_sha256, device).
• Zero-byte scene files are prohibited.

====================================================================
STAGE B0 — CHUNKING
Responsibilities:
• Convert ASR output into semantic chunks suitable for embedding

Guaranteed outputs:
• chunks/.jsonl

Rules:
• When ASR contains segments, one or more records with record_type="chunk" MUST be written.
• When ASR contains zero segments, the file MUST still exist and MUST contain exactly one explicit non-chunk outcome record:
  record_type="chunking_outcome" reason="ASR_SEGMENTS_EMPTY"
• Zero-byte chunk files are prohibited.
• Chunking outcome records MUST include artifact_fingerprint once implemented. Until implemented, chunk artifacts without artifact_fingerprint MUST be treated as eligible for reprocessing under the deprecated-tool policy.

====================================================================
STAGE B1 — EMBEDDING
Responsibilities:
• Convert chunk records into vector embeddings

Guaranteed outputs:
• embeddings/.json

Rules:
• Embeddings files MUST exist after invocation, regardless of outcome.
• Files MUST explicitly record:
  – Model identifier (non-authoritative label)
  – Model revision hash
  – Embedding dimensionality
  – Normalization flags
  – Embedding count
  – Execution environment
• Canonical embedding identity MUST include authoritative content hashes for:
  – the ONNX model file actually loaded (onnx_model_sha256)
  – the tokenizer artifact actually loaded (tokenizer_sha256)
  – any additional preprocessing config artifact if used (sha256)
A model_id string may remain but MUST be treated as non-authoritative compared to hashes.
• If no chunks are eligible for embedding, the file MUST still exist and MUST explicitly record the reason: NO_ELIGIBLE_CHUNKS.
• Embedding success or failure MUST NEVER be inferred from file presence.
• Embeddings artifacts MUST include artifact_fingerprint as a top-level field. Artifacts missing artifact_fingerprint MUST be treated as eligible for reprocessing under the deprecated-tool policy.

====================================================================
STAGE E0 — EVENTS (DERIVED SEMANTIC AGGREGATION)
Responsibilities:
• Derive explicit semantic events from ordered chunk sequences
• Aggregate contiguous or semantically coherent chunks into event records
• Establish auditable evidence linkage between events and source chunks

Guaranteed outputs:
• events/.jsonl

Rules:
• Stage E0 operates strictly on existing chunks artifacts.
• Stage E0 MUST NOT mutate chunks or upstream artifacts.
• Each event_record MUST include:
  – event_id
  – media_id
  – temporal bounds (t0_ms, t1_ms)
  – event_text
  – explicit evidence linking (chunk_ids)
• Evidence linkage MUST be complete and internally consistent.
• Stage E0 MUST emit an explicit outcome record even if no events are produced.
• Zero-byte events files are prohibited.
• Contract validation MUST be enforceable via a deterministic checker.
• Event derivation MUST be reproducible given identical inputs and configuration.
• events_outcome MUST include artifact_fingerprint once implemented. Until implemented, event artifacts without artifact_fingerprint MUST be treated as eligible for reprocessing under the deprecated-tool policy.

====================================================================
STAGE F0 — ENTITIES (MENTION EXTRACTION)
Responsibilities:
• Derive explicit entity mentions from text evidence for semantic search and later grounding
• Produce span-level entity mentions scoped to events when available, otherwise chunks
• Maintain evidence linkage from mentions back to canonical upstream artifacts

Guaranteed outputs:
• entities/.jsonl

Rules:
• Stage F0 MUST prefer event-scoped extraction when events artifacts exist and are valid.
• If events artifacts are absent or invalid, Stage F0 MUST fall back to chunk-scoped extraction.
• Stage F0 MUST NOT mutate upstream artifacts.
• Each entity_record MUST include:
  – entity_id
  – media_id
  – scope and scope_id (event or chunk)
  – span offsets (start_char, end_char) within the referenced text
  – entity_text and entity_type
  – explicit evidence linking (at minimum, chunk_ids via scope evidence)
• Stage F0 MUST emit an explicit outcome record even if no entities are produced.
• Zero-byte entities files are prohibited.
• Contract validation MUST be enforceable via a deterministic checker.
• Entity extraction MUST be reproducible given identical inputs and configuration.
• entities_outcome MUST include artifact_fingerprint. Artifacts missing artifact_fingerprint MUST be treated as eligible for reprocessing under the deprecated-tool policy.

====================================================================
STAGE F1 — ENTITY LINKING (MENTIONS TO NODES)
Responsibilities:
• Consolidate entity mentions into stable entity nodes suitable for indexing and retrieval
• Resolve repeated mentions to a canonical node ID within a media_id
• Apply deterministic filtering to suppress low-value or obviously spurious nodes
• Preserve evidence linkage to source mentions and, via mentions, to source chunks/events

Guaranteed outputs:
• entity_nodes/.jsonl
• entity_mentions/.jsonl

Rules:
• Stage F1 MUST operate strictly on existing Stage F0 and optional candidate mention streams, but canonical outputs MUST be produced deterministically.
• Stage F1 MUST NOT mutate upstream artifacts.
• Each entity_node MUST include:
  – entity_node_id
  – media_id
  – entity_type
  – display_name (human-readable)
  – mention_ids (the supporting mentions)
• Each entity_mention MUST include:
  – mention_id
  – entity_node_id
  – scope and scope_id (event or chunk)
  – span offsets and mention text
  – evidence linking (chunk_ids)
• Stage F1 MUST emit an explicit outcome record even if no nodes are produced.
• Zero-byte entity_nodes/entity_mentions files are prohibited.
• Contract validation MUST be enforceable via a deterministic checker.
• Entity linking MUST be reproducible given identical inputs and configuration.
• entity_link_outcome MUST include artifact_fingerprint once implemented. Until implemented, F1 artifacts without artifact_fingerprint MUST be treated as eligible for reprocessing under the deprecated-tool policy.

====================================================================
STAGE C — VECTOR MATERIALISATION (MILVUS)
Responsibilities:
• Materialise eligible embeddings, events, and entity nodes into a queryable vector store
• Validate vector search correctness
• Produce an auditable, deterministic vector catalog

Guaranteed outputs:
• catalog/milvus/moments.db (catalog-scoped)
• audit/stageC_build_*.json

Scope:
• Outputs are catalog-scoped and aggregate across MEDIA_IDs.

Rules:
• Stage C MUST read only from existing Stage B1, Stage E0, and Stage F1 artifacts.
• Stage C MUST NOT mutate per-MEDIA_ID artifacts.
• Stage C MUST enforce exclusive access to the published vector database (moments.db) at publish time.
Exclusive access is defined by actual OS-level open handles and locks on moments.db.
• Stage C MAY build and validate a candidate database file while moments.db is in use, provided Stage C does not attempt to publish into moments.db while it is held open.
• If moments.db is in use at publish time, Stage C MUST NOT publish. Stage C MUST record an explicit skipped outcome with enumerated reason codes.
• The database MUST be built into a new file and atomically swapped.
• Any prior database MUST be archived, never overwritten.
• Validation searches MUST be performed and recorded.
• Stage C MUST record the collection schema(s) it produced, and the embedding identity (authoritative hashes) used for materialisation.
• Stage C is optional and non-blocking.
• Pipeline correctness MUST NOT depend on Stage C having been run.

Collection requirements:
• moments_asr_v1 MUST store vectors directly from Stage B1 eligible chunk vectors.
• moments_events_v1 MUST store vectors derived deterministically as the mean of referenced chunk vectors.
• moments_entity_nodes_v1 MUST store vectors derived deterministically as the mean of referenced chunk vectors via entity_mentions evidence.

====================================================================
BATCH ORCHESTRATION
Batch orchestration is responsible for:
• Coordinating multi-stage execution
• Capturing per-stage invocation metadata
• Writing run-scoped operational logs under logs.all

Rules:
• Every stage invocation MUST produce exactly one run-scoped results entry.
• Logs MUST exist even if a stage produces no stdout or stderr.
• Batch execution MUST NOT silently omit stages.

====================================================================
AUDIT AND PROVENANCE
Audit tooling is responsible for:
• Capturing ground truth
• Recording file hashes
• Establishing temporal provenance

Rules:
• Audit artifacts are immutable once written.
• Provenance records MUST remain valid even if downstream artifacts are removed.
• Tooling runs MUST record execution environment, command, exit code, and outputs explicitly.
• Audit and operational records MUST be written under logs.all.

====================================================================
SERVICES AND RUNTIME PROCESSES
Online services (e.g. webroot_server.py):
• Are read-only with respect to canonical pipeline artifacts under /workspace/catalog
• MUST NOT mutate catalog state
• MUST tolerate incomplete, partial, or in-progress pipeline state
• MUST write operational logs and service runstate only under /workspace/pipeline/logs.all

Watcher and orchestration processes:
• Are operational controllers, not semantic stages
• MAY trigger or schedule stage execution
• MUST write operational logs and runstate only under logs.all
• MUST NOT be treated as evidence of semantic completion or correctness

Vector search services:
• Operate only on existing artifacts
• MUST be stoppable without affecting pipeline correctness
• Are not required for pipeline execution

Port binding, process existence, or startup log messages MUST NOT be treated as readiness signals.
Readiness MUST be verified via explicit protocol-level interaction appropriate to the service.

====================================================================
EXECUTION ENVIRONMENTS (VENV POLICY)
[UNCHANGED FROM v2.6 CONTENT, INTENTIONALLY]

====================================================================
ERROR HANDLING PHILOSOPHY
The pipeline prefers:
• Explicit failure records
• Redundant metadata
• Over-reporting rather than under-reporting

Any situation where a downstream consumer would need to infer or guess intent, state, or outcome is considered a pipeline defect.

====================================================================
CHANGE CONTROL
This document MUST be updated when:
• A stage’s guaranteed outputs change
• A new stage is introduced
• A new invariant is enforced
• Execution environments are altered
• Any ambiguity is resolved through enforcement

Version increments are mandatory.

====================================================================
END OF CANONICAL DOCUMENT