PIPELINE_WRITE_AND_INTERACTION_MAP.md

Version: 2.8
Validated through 2026-02-11 (UTC), re-validation scheduled 2026-02-12 (UTC)

====================================================================
SUMMARY OF ALTERATIONS (SURGICAL)

Operational logging unification retained:
All operational records, runstate, and tool audits MUST be written under:
/workspace/pipeline/logs.all
Operational writes under /workspace/logs or /workspace/runstate remain prohibited.

Watcher and service operational ownership retained:
• /workspace/pipeline/logs.all/media_watch/*
• /workspace/pipeline/logs.all/media_webroot/*

Launcher/preflight control-surface governance added:
The launcher and preflight scripts are now explicitly mapped as contract-critical operational tooling:
• /workspace/pipeline/bin/media_watch_launch_detached.sh
• /workspace/pipeline/bin/preflight_guard.sh

Preflight/launcher operational evidence placement clarified:
Contract/preflight run evidence MUST be written under logs.all (tool manifests/outcomes), with append-only canonical audit mirrors under /workspace/catalog/audit when invoked as audited tooling.

Repository-root resolution rule added for operational guards:
Guard tooling MUST resolve repo root dynamically (git top-level), not by hardcoded alternate roots.

No stage write ownership changed.
No canonical stage semantics changed.
No catalog directory ownership changed.

====================================================================
PURPOSE OF THIS DOCUMENT
This document defines:
• Which stages may read or write which paths
• Which stages may interact with which services
• Which non-stage tooling may write audited artifacts
• What guarantees exist around exclusivity, ordering, and isolation
• What MUST NEVER happen, even transiently

This document is normative.
Violations are bugs, not edge cases.

====================================================================
GLOBAL PRINCIPLES
Single-writer, multi-reader
At any given time, a path has at most one writer.

Build-then-publish
No stage may mutate a “live” artifact in place.

No inference from presence
Existence, absence, emptiness, or zero counts MUST NOT be used to infer success, failure, or skip.

No empty enumerations
Enumerated fields (reason, reasons entries, decision_reason, etc) MUST NOT be empty strings.
If a field is present, it MUST be a valid enumerated code.
If not applicable, it MUST be omitted, or set to an explicit enumerated sentinel (preferred).

Explicit scope
Stages operate either at MEDIA scope or CATALOG scope, never both implicitly.

Readiness is explicit
Service readiness MUST be determined by readiness endpoints, not by time delays, supervisor state, or process existence.

Canonical code surface
Canonical stage implementations currently live under:
• /workspace/pipeline/bin
Non-canonical backups (e.g. -CopyN.) MUST NOT be used as entrypoints.

Canonical operational log root
All operational logs, operational runstate, and tool operational audit artifacts MUST live under:
• /workspace/pipeline/logs.all
Operational artifacts MUST NOT be written under /workspace/logs or /workspace/runstate.

Operational control-surface guardrails
Launcher/preflight operational controls are contract-critical:
• media_watch_launch_detached.sh and preflight_guard.sh are governed tooling surfaces.
• Guard tooling MUST resolve repository root via git top-level.
• Guard tooling MUST fail explicitly on invariant violations and must not silently continue on partial checks.

====================================================================
DIRECTORY OWNERSHIP

/workspace/pipeline/bin
Writer: Humans (code authoring)
Readers: All stages and orchestration (read-only)
Rules:
• Code is not pipeline state.
• Stages and services MUST treat code as read-only at runtime.
• Any automated rewriting of code in place is forbidden unless it is an explicit, audited tooling action outside canonical stage execution.

/workspace/pipeline/config
Writer: Humans (configuration authoring)
Readers: Orchestration, non-stage tooling, always-on watchers (read-only)
Rules:
• Configuration is not pipeline state.
• Configuration MUST be treated as read-only at runtime.
• Configuration changes are governance actions and should be audited externally when relevant.
• Example: reprocess_policy.json is a non-canonical operational policy file used by always-on watchers to decide reprocessing based on explicit artifact fingerprinting.

CANONICAL PIPELINE DATA (CATALOG)

/workspace/catalog/raw
Writer: Stage A (ingest)
Readers: Stage A, Stage B2, diagnostics
Immutable after write.

/workspace/catalog/media_manifest
Writer: Stage A
Readers: Stage B, Stage B2, Stage B3, Stage P, Stage E0, Stage F0, Stage F1, diagnostics, media_webroot (read-only)
Rules:
• Manifest presence is not a success signal, but Stage A is the canonical writer.
• Manifest is MEDIA-scoped.

/workspace/catalog/status
Writer: Stage A and orchestration/status tooling
Readers: Humans, orchestration, diagnostics, media_webroot (read-only)
Rules:
• Status is not a gate for other stages unless explicitly defined by MASTER.
• Presence/absence is not a success signal.

/workspace/catalog/asr
Writer: Stage A
Readers: Stage B0, diagnostics, media_webroot (read-only)
Rules:
• May contain empty or silent ASR outputs.
• MUST always be paired with explicit QC and outcome records.
• Enumerated reason fields in ASR/QC outputs MUST NOT be empty strings. Use omission or enumerated sentinels (typically OK) as appropriate.

/workspace/catalog/logs
Writer: Stage A
Readers: Humans, diagnostics, tooling
Rules:
• Run logs and preflight logs for Stage A are written here.
• This directory is not canonical semantic output, but is pipeline-run evidence and must remain MEDIA_ID / RUN_ID traceable.
• Zero-byte files are prohibited unless explicitly defined as valid.
• Presence/absence MUST NOT be treated as a stage success signal.

/workspace/catalog/chunks
Writer: Stage B0
Readers: Stage B1, Stage E0, Stage F0
Rules:
• jsonl files MUST NOT be empty.
• Zero-chunk cases MUST emit chunking_outcome records.
• One logical media_id == one chunks artifact.
• The final record MUST be record_type="chunking_outcome" for every invocation.

/workspace/catalog/embeddings
Writer: Stage B1
Readers: Stage C, Stage E0 (optional, read-only), Stage F0 (optional, read-only), Stage F1 (optional, read-only)
Rules:
• Each media_id MUST have exactly one embeddings artifact.
• Absence is forbidden.
• Embedding identity MUST be represented using authoritative hashes (onnx_model_sha256, tokenizer_sha256).
• embeddings.<media_id>.json MUST include chunks_artifact_sha256 and it MUST equal the sha256 of the current chunks/<media_id>.jsonl bytes.
• A model_id string may exist but is non-authoritative and MUST NOT replace authoritative hashes.

/workspace/catalog/frames
Writer: Stage B
Readers: Editorial tooling, diagnostics, media_webroot (read-only)
Rules:
• Frames are MEDIA-scoped under frames/<media_id>/.
• Missing frames MUST be explicitly recorded by Stage B (not inferred from absence).

/workspace/catalog/enrich
Writer: Stage B
Readers: Editorial tooling, diagnostics, media_webroot (read-only)
Rules:
• Enrich is MEDIA-scoped.
• Missing enrich MUST be explicitly recorded by Stage B.

/workspace/catalog/motion
Writer: Stage B2
Readers: Stage B3 (temporal segmentation), Stage E0 (optional), diagnostics, media_webroot (read-only)
Rules:
• motion artifacts MUST exist after invocation.
• Zero or near-zero motion is a valid outcome and MUST be explicit.
• Absence MUST NOT be used to infer skip or failure.

/workspace/catalog/scenes
Writer: Stage B3
Readers: Editorial tooling, Stage E0, diagnostics, media_webroot (read-only)
Rules:
• Scene/temporal segmentation artifacts MUST exist after invocation.
• Zero scenes (continuous footage) is a valid outcome.
• “No cuts detected” or “no temporal boundaries” MUST be explicit, never inferred.
• Cut detection is optional and MUST NOT be assumed to be present or meaningful.
• For learned detectors (e.g. TransNetV2), artifacts MUST include detector identity fields (torch_version, detector package version, weights_sha256, device).
Note: This directory remains named “scenes” for compatibility with existing tooling and MASTER guarantees.

/workspace/catalog/events
Writer: Stage E0
Readers: Stage C, Stage F0 (optional), Editorial tooling (optional), diagnostics, media_webroot (read-only)
Rules:
• Events are derived, MEDIA-scoped artifacts under events/<media_id>/.
• Event artifacts MUST exist after invocation, even when zero events are produced.
• Zero events is a valid outcome and MUST be explicit via events_outcome.
• Absence MUST NOT be used to infer skip or failure.
• Event records MUST provide evidence pointers to canonical artifacts (e.g. chunks, scenes, motion).
• event_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Event derivation MAY use embeddings as a read-only input to create deterministic boundaries, but MUST NOT require semantic services or Stage C.

NOTE:
• Stage E0 reads embeddings from /workspace/catalog/embeddings as an optional input for deterministic splitting, but this does not grant Stage E0 any write authority over embeddings and MUST NOT introduce cross-stage write coupling.

/workspace/catalog/entities
Writer: Stage F0
Readers: Stage F1, diagnostics, media_webroot (read-only)
Rules:
• Entities are derived, MEDIA-scoped artifacts under entities/<media_id>/.
• Entities artifacts MUST exist after invocation, even when zero entity_record entries are produced.
• Zero entities is a valid outcome and MUST be explicit via entities_outcome.
• Absence MUST NOT be used to infer skip or failure.
• Entity records MUST provide evidence pointers to canonical artifacts (e.g. events, chunks).
• entity_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Stage F0 MUST prefer event-scoped extraction when events artifacts exist and are valid, and MUST fall back to chunk-scoped extraction otherwise.
• Stage F0 MUST NOT require semantic services or Stage C.

/workspace/catalog/entity_nodes
Writer: Stage F1
Readers: Stage C, diagnostics, media_webroot (read-only)
Rules:
• Entity node artifacts are MEDIA-scoped.
• Entity nodes MUST exist after invocation, even when zero nodes are produced.
• Zero nodes is a valid outcome and MUST be explicit via entity_link_outcome.
• Absence MUST NOT be used to infer skip or failure.
• entity_node_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Stage F1 MUST NOT require semantic services or Stage C.

/workspace/catalog/entity_mentions
Writer: Stage F1
Readers: Stage C, diagnostics, media_webroot (read-only)
Rules:
• Entity mention artifacts are MEDIA-scoped.
• Entity mentions MUST exist after invocation, even when zero mentions are produced.
• mention_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Entity mentions MUST contain evidence.chunk_ids suitable for deterministic vector derivation in Stage C.
• Stage F1 MUST NOT require semantic services or Stage C.

/workspace/catalog/proxies
Writer: Stage P
Readers: Stage B3, Editorial tooling, media_webroot (read-only)
Rules:
• Proxies are MEDIA-scoped (proxies/<media_id>.mp4).
• Missing proxies are not an error unless Stage P was invoked, must be explicit in status.

/workspace/catalog/milvus
Writer: Stage C (exclusive)
Readers: media_webroot (read-only)
Rules:
• moments.db is a single-writer resource.
• No stage other than Stage C may replace or mutate it.
• Readers MUST tolerate the DB being temporarily unavailable.

Important clarification:
• media_webroot MUST NOT open moments.db as a database.
• media_webroot MAY read moments.db as a file for byte-copy snapshotting only.
• media_webroot MUST open only the snapshot DB as a database (moments.snapshot.db).

/workspace/catalog/audit
Writer: All stages and contract/audit tooling (append-only)
Readers: Humans, tooling
Rules:
• Append-only.
• Never rewritten.
• File naming MUST include run_id and UTC timestamp.

OPERATIONAL LOGS AND RUNSTATE (NON-CANONICAL, CONTRACT-GOVERNED)

/workspace/pipeline/logs.all
Writer: Service/tool operators and always-on services (operational only)
Readers: Humans, tooling, diagnostics
Rules:
• Operational logs and operational runstate only.
• Not canonical pipeline state.
• Must be treated as non-authoritative evidence that references canonical artifacts under /workspace/catalog.

/workspace/pipeline/logs.all/media_webroot/logs
Writer: media_webroot (operational only)
Readers: Humans, tooling
Rules:
• Operational logs only.
• Not pipeline state.
• Must exist and be rotated per supervisor policy.

/workspace/pipeline/logs.all/media_webroot/runstate
Writer: media_webroot wrapper (operational only)
Readers: Humans, tooling
Rules:
• Atomic JSON runstate artifacts.
• Zero-byte forbidden.
• Invalid artifacts must be quarantined with explicit reason records.

/workspace/pipeline/logs.all/media_watch/stage_logs
Writer: media_watch daemon (operational only)
Readers: Humans, tooling
Rules:
• Operational logs only.
• Not pipeline state.
• MUST NOT contain canonical pipeline artifacts.
• MUST be treated as non-authoritative diagnostics.
• Per-stage combined stdout/stderr logs MAY exist here for forensic inspection and are referenced by sha256 and byte length from audit JSONs.
• Zero-byte stage logs are permitted only when the invoked stage produces no stdout/stderr. This does not imply the stage did nothing.

/workspace/pipeline/logs.all/media_watch/audit
Writer: media_watch daemon (operational only)
Readers: Humans, tooling
Rules:
• Atomic JSON per-media orchestration outcome artifacts (tool_media_watch_outcome family).
• Zero-byte forbidden.
• MUST NOT be treated as a pipeline write surface or as canonical truth.
• MUST reference canonical /workspace/catalog/audit entries when stages are invoked (run manifests/outcomes may also be mirrored operationally).
• Archiving, moving, or rotating this directory while the daemon is running is forbidden. Rotation MUST occur only while the daemon is stopped.

/workspace/pipeline/logs.all/media_watch/state.json
Writer: media_watch daemon (operational only)
Readers: Humans, tooling
Rules:
• Atomic JSON state artifact.
• Zero-byte forbidden.
• Tracks watched roots, scan configuration, and per-path processing state.
• MUST NOT be treated as a pipeline write surface.

/workspace/pipeline/logs.all/media_watch/media_watch.pid
Writer: media_watch launcher / operator tooling (operational only)
Readers: Humans, tooling
Rules:
• PID file only.
• Presence/absence MUST NOT be used as pipeline readiness.

/workspace/pipeline/logs.all/tools/preflight
Writer: preflight_guard and audited operational tooling
Readers: Humans, tooling
Rules:
• Operational preflight evidence only.
• MUST include explicit pass/fail outcomes and invariant details when invoked.
• MUST NOT be treated as semantic stage truth.

/workspace/pipeline/logs.all/tools/launch
Writer: launcher/operator tooling
Readers: Humans, tooling
Rules:
• Operational launcher evidence only (start/stop/status attempts, command identity, outcome).
• MUST use explicit non-zero outcomes on invariant violations.

RUN-SCOPED EXECUTION ARTIFACTS (OPTIONAL)

/workspace/catalog/runs
Writer: Orchestration (batch)
Readers: Humans, tooling
Rules:
• Run-scoped execution artifacts live under runs/<run_id>/.
• Presence/absence is not a stage success signal.

====================================================================
STAGE-BY-STAGE INTERACTION MAP

STAGE A – INGEST / ASR
Writes:
• raw/*
• media_manifest/*
• asr/*
• status/*
• catalog/logs/*
• audit/*

Reads:
• source media

Constraints:
• May replace prior canonical ASR artifacts for the same media_id via atomic write+replace only. No in-place mutation.
• MUST emit explicit outcomes for silent or non-speech audio.
• Enumerated fields in ASR/QC outputs MUST NOT be empty strings. If applicable, use OK or other enumerated codes, otherwise omit.

STAGE B – VISUAL ENRICHMENT
Writes:
• frames/*
• enrich/*
• status/* (optional metadata, not gating)
• audit/*

Reads:
• raw/*
• media_manifest/*

Constraints:
• MUST NOT infer anything from missing frames, must be explicit in enrich/status.

STAGE B0 – CHUNKING
Writes:
• chunks/*
• audit/*

Reads:
• asr/*

Constraints:
• MUST NOT infer chunk absence from empty ASR.
• MUST emit chunking_outcome when no chunks are produced.
• MUST emit chunking_outcome for all invocations as the final record.

STAGE B1 – EMBEDDING
Writes:
• embeddings/*
• audit/*

Reads:
• chunks/*
• models (read-only)

Constraints:
• MUST emit embedding_outcome if no vectors are produced.
• MUST record model identity and embedding dimensionality.
• MUST record authoritative model artifact hashes (onnx_model_sha256, tokenizer_sha256).
• MUST record chunks_artifact_sha256 and it MUST match the sha256 of the current chunks/<media_id>.jsonl bytes.

STAGE B2 – MOTION ANALYSIS
Writes:
• motion/*
• audit/*

Reads:
• raw/*
• media_manifest/* (optional path discovery)

Constraints:
• MUST handle very short clips explicitly.
• Zero motion is not a failure condition.
• MUST emit explicit outcomes when skipped or degraded.
Stage B2 is media-scoped.

STAGE B3 – TEMPORAL SEGMENTATION (SCENES / STRUCTURE)
Writes:
• scenes/*
• audit/*

Reads:
• proxies/*
• motion/* (optional, read-only)
• media_manifest/* (optional path discovery)

Constraints:
• MUST tolerate unedited single-take footage.
• Zero scenes/segments is a valid, expected outcome.
• MUST emit explicit “no cuts detected” / “no temporal boundaries” outcomes.
• MUST NOT infer failure from zero scenes.
• Cut detection is optional and MUST NOT be assumed to be present or meaningful.
• For learned detectors (e.g. TransNetV2), artifacts MUST include detector identity fields (torch_version, detector package version, weights_sha256, device).
Stage B3 is media-scoped.

STAGE E0 – EVENT DERIVATION
Writes:
• events/*
• audit/*

Reads:
• chunks/*
• embeddings/* (optional, read-only)
• scenes/* (optional, read-only)
• motion/* (optional, read-only)
• media_manifest/* (optional path discovery)

Constraints:
• Events are derived artifacts and MUST NOT be treated as canonical truth for any upstream fact.
• MUST write events_outcome for every invocation, even when zero event_record entries are produced.
• MUST NOT infer “no events” from missing inputs, it MUST be explicit via reasons.
• event_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Evidence pointers MUST reference canonical artifacts (e.g. chunks, scenes, motion) and MUST be sufficient to rehydrate and validate the event without semantic indexes.
• If embeddings are used for deterministic boundary detection, Stage E0 MUST treat them as read-only inputs and MUST NOT require Stage C or any semantic service.
Stage E0 is media-scoped.

STAGE F0 – ENTITY EXTRACTION
Writes:
• entities/*
• audit/*

Reads:
• events/* (preferred, read-only)
• chunks/* (fallback, read-only)
• embeddings/* (optional, read-only)
• media_manifest/* (optional path discovery)

Constraints:
• Entities are derived artifacts and MUST NOT be treated as canonical truth for any upstream fact.
• MUST write entities_outcome for every invocation, even when zero entity_record entries are produced.
• MUST NOT infer “no entities” from missing inputs, it MUST be explicit via reasons.
• entity_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Evidence pointers MUST reference canonical artifacts (e.g. events, chunks) and MUST be sufficient to rehydrate and validate the mention without semantic indexes.
• MUST prefer event-scoped extraction when events exist and are valid, fall back to chunk-scoped otherwise.
Stage F0 is media-scoped.

STAGE F1 – ENTITY LINKING
Writes:
• entity_nodes/*
• entity_mentions/*
• audit/*

Reads:
• entities/*
• embeddings/* (optional, read-only)
• events/* (optional, read-only)
• chunks/* (optional, read-only)
• media_manifest/* (optional path discovery)

Constraints:
• Entity nodes and mentions are derived artifacts and MUST NOT be treated as canonical truth for any upstream fact.
• MUST write entity_link_outcome for every invocation, even when zero entity_node entries are produced.
• MUST NOT infer “no nodes” from missing inputs, it MUST be explicit via reasons.
• entity_node_id and mention_id MUST be deterministically derived from stable inputs and MUST be reproducible across runs given identical inputs.
• Evidence pointers MUST be sufficient for Stage C to derive node vectors deterministically (chunk_ids required).
Stage F1 is media-scoped.

STAGE C – MILVUS MATERIALISATION
Writes:
• milvus/m_build_*.db (build artifacts)
• milvus/moments.db (publish target, atomic swap only)
• audit/
• stageC_build_manifest

Reads:
• embeddings/*
• events/* (if present and included in build policy)
• entity_nodes/* (if present and included in build policy)
• entity_mentions/* (if present and included in build policy)
• milvus/moments.db (existence and lock state only)

Constraints:
• MUST NOT write to moments.db directly (no in-place mutation).
• MUST build to a temporary DB and publish atomically.
• MUST detect OS-level file usage before publish.
• MUST retry publish according to configured policy.
• MUST emit explicit skip outcomes when blocked.
• Stage C MAY operate deterministically over an explicit media_id list when invoked accordingly (e.g. for regression builds), but this does not change its catalog-scoped write authority or locking requirements.
Stage C is catalog-scoped. media_id MUST be "catalog".

STAGE P – PROXY GENERATION
Writes:
• proxies/*
• status/* (optional metadata)
• audit/*

Reads:
• raw/*
• media_manifest/*

Constraints:
• Optional stage.
• Skips or failures MUST be explicit in status or audit.

TOOLING (NON-STAGE) – AUDITED ACTIONS (ENV GUARDS, SERVICE SETUP, SYSTEM OPS)
Writes:
• Operational only:
– /workspace/pipeline/logs.all/* (tool run manifests, tool contracts outcomes, preflight/launcher operational outcomes)
• Canonical audit (append-only only):
– /workspace/catalog/audit/*

Reads:
• pipeline code and configuration (read-only)

Constraints:
• MUST NOT write to any /workspace/catalog/* paths except append-only writes under /workspace/catalog/audit/*.
• MUST NOT mutate or replace canonical artifacts.
• MUST NOT infer success from side effects, it MUST write explicit audit artifacts.
• Guard tooling MUST resolve repository root dynamically via git top-level before applying path-based checks.

MEDIA_WEBROOT SERVICE
Writes:
• None to pipeline or catalog state
• Operational only (under logs.all):
– /workspace/pipeline/logs.all/media_webroot/logs/*
– /workspace/pipeline/logs.all/media_webroot/runstate/*

Reads:
• /workspace/webroot/*
• /workspace/pipeline/* (static docs)
• /workspace/catalog/enrich/*
• /workspace/catalog/frames/*
• /workspace/catalog/scenes/*
• /workspace/catalog/motion/*
• /workspace/catalog/events/*
• /workspace/catalog/entities/*
• /workspace/catalog/entity_nodes/*
• /workspace/catalog/entity_mentions/*
• /workspace/catalog/proxies/*
• /workspace/catalog/milvus/moments.db (file byte-copy only)
• /workspace/webroot/milvus/moments.snapshot.db (database open only)
• /workspace/catalog/media_manifest/* (read-only)
• /workspace/catalog/status/* (read-only)

Constraints:
• Supervisor is the single owner of port 3000.
• Readiness via /api/health only.
• MUST tolerate incomplete or in-progress catalog state.
• Snapshot refresh attempts MUST be explicitly reported.

====================================================================
MILVUS-SPECIFIC INTERACTION RULES
• Milvus Lite backend may spawn child processes.
• Those processes may hold OS file handles open.
• File handle presence is authoritative.
• Service readiness != publish readiness.

Lazy mode semantics:
• DB may open on demand.
• DB may remain open after last request.
• Idle close is best-effort only.

Therefore:
• Stage C MUST rely on lock detection, not timing.

Observed schema note (informational, not a permanent guarantee):
• Collection names and fields are controlled by Stage C.
• As of this session, moments_asr_v1 fields include: pk, media_id, start_s, end_s, text, vector (dim 384).
• As of this session, moments_events_v1 fields include: pk (event_id), media_id, t0_ms, t1_ms, start_s, end_s, text, vector (dim 384).
• As of this session, moments_entity_nodes_v1 fields include: pk (entity_node_id), media_id, start_s, end_s, entity_type, text, vector (dim 384).
• pk is structured as media_id:chunk_id for moments_asr_v1 to support deterministic traceability.

====================================================================
LOCKING AND EXCLUSIVITY
Locks are detected by observation, not coordination.

Valid signals:
• lsof / open file handles
• explicit filesystem locks

Invalid signals:
• process existence
• service health endpoints
• time since last request

No stage may force-release another stage’s lock.

====================================================================
READ-ONLY MODE
When READ_ONLY_API is enabled:
• media_webroot MUST NOT mutate pipeline or catalog artifacts.
• Stage C MUST still record attempted publish outcomes.
• Read-only does NOT imply inactivity.

====================================================================
FORBIDDEN INTERACTIONS
The following are forbidden:
• Stage C mutating moments.db in place.
• Any stage deleting another stage’s outputs.
• Any stage inferring outcome from missing files.
• Any service holding write locks while advertising readiness.
• Publishing based on service state instead of file state.
• Any process other than media_webroot binding to port 3000.
• media_webroot opening moments.db as a database.

Additionally forbidden:
• Any non-canonical tooling (e.g. vision_gpu exploratory work) writing into /workspace/catalog/* directly, except append-only writes under /workspace/catalog/audit/* as explicitly permitted.
• Any non-canonical tooling mutating or replacing canonical artifacts.
• Moving or rotating /workspace/pipeline/logs.all/media_watch/audit while media_watch is running.
• Using empty strings for enumerated fields (reason, decision_reason, reasons entries, etc).
• Writing operational logs or operational runstate anywhere outside /workspace/pipeline/logs.all.
• Treating launcher/preflight invariant failures as warnings-only conditions.

====================================================================
APPENDIX A — NON-CANONICAL COMPUTE AND WRITE BOUNDARIES (INFORMATIONAL)
This appendix is informational and does not grant new write permissions, directory ownership, or stage authority.
It clarifies how optional, non-canonical compute (e.g. GPU-based vision tooling) may interact with the filesystem without violating single-writer and build-then-publish principles.

VISION_GPU / HEAVY VISION TOOLING
Certain tooling (e.g. segmentation, matting, object extraction, dense vision analysis) may execute outside canonical pipeline environments, typically in:
• GPU-enabled containers
• CUDA-dependent virtual environments
• Experimental or exploratory compute contexts

These environments are collectively referred to as vision_gpu.
vision_gpu is NOT a pipeline stage.

WRITE PERMISSIONS AND CONSTRAINTS
vision_gpu tooling:
• MUST NOT write directly into canonical pipeline directories as an owner.
• MUST NOT mutate existing pipeline artifacts in place.
• MUST NOT replace or delete artifacts written by canonical stages.

If vision_gpu outputs are to be incorporated into the pipeline, they MUST do so via one of the following permitted patterns:
• Write to a non-pipeline scratch or staging path, followed by ingestion through an existing canonical stage.
• Write artifacts that conform exactly to an existing schema, under the authority of the canonical stage that owns that directory.

In all cases, the canonical stage remains the single writer of record.

DIRECTORY INTERACTION RULES
vision_gpu tooling MUST NOT directly write to:
• /workspace/catalog/*
• /workspace/catalog/milvus/*
• /workspace/catalog/audit/*
• Any directory owned by a canonical stage

vision_gpu tooling MAY write to:
• Explicit scratch paths outside /workspace/catalog
• Temporary build directories not referenced by orchestration
• Paths explicitly designated as non-pipeline by deployment configuration

Any movement of artifacts into pipeline directories MUST be performed by the canonical stage responsible for that directory.

INTERACTION WITH SERVICES
vision_gpu tooling:
• MUST NOT interact directly with media_webroot
• MUST NOT open moments.db (or snapshots) as a database
• MUST NOT rely on service readiness, liveness, or ports as signals

All service interactions remain governed by the canonical rules in this document.

PROHIBITIONS
The following are explicitly forbidden:
• Treating vision_gpu as a writer for any pipeline directory
• Bypassing canonical stages to “inject” artifacts
• Writing artifacts that require special-case readers
• Inferring pipeline correctness from GPU execution
• Allowing non-canonical tooling to weaken exclusivity or locking rules

RATIONALE
This appendix ensures that optional GPU-accelerated tooling can be used for experimentation and acceleration without:
• Violating single-writer guarantees
• Expanding the pipeline contract surface
• Introducing hidden dependencies
• Weakening auditability or determinism

vision_gpu exists to assist the pipeline, not redefine it.

====================================================================
CHANGE CONTROL
This document MUST be updated when:
• A new directory is introduced.
• A stage gains write access to a new path.
• Locking semantics change.
• A service changes read/write guarantees.
• Non-canonical tooling is promoted into a canonical stage with defined write surfaces.
• A non-stage tooling write surface under /workspace/catalog/audit is introduced or altered.

Version increments are mandatory.

====================================================================
END OF MAP