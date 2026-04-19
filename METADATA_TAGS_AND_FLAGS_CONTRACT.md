METADATA_TAGS_AND_FLAGS_CONTRACT.md

Version: 2.8
Evidence horizon: validated through 2026-02-11 (UTC), re-validation scheduled 2026-02-12 (UTC)

====================================================================
SUMMARY OF ALTERATIONS (SURGICAL)

Launcher/preflight guardrail contract hardening:
Added explicit contract-governed launcher invariants and preflight enforcement requirements for:
• bin/media_watch_launch_detached.sh
• bin/preflight_guard.sh

Reason: recent drift allowed forbidden launcher patterns and malformed control shape to pass until late checks. These files are now explicitly treated as contract-critical control surfaces.

Repository-root resolution clarification:
Operational guard scripts MUST resolve repository root dynamically via git top-level rather than assuming a fixed alternate root path.

Reason: avoids false context failures caused by hardcoded repo paths that do not match runtime.

Operational logging unification retained:
All operational records, runstate, and tool audits remain constrained to:
/workspace/pipeline/logs.all
Writes under /workspace/logs or /workspace/runstate remain prohibited.

Watcher reason separation retained:
ALREADY_COMPLIANT and INELIGIBLE_FOR_EMBEDDING remain distinct outcomes even when start_stage="NONE".

No stage semantic vocabularies were changed.
No existing reason meanings were changed.

====================================================================
PURPOSE OF THIS DOCUMENT
This document defines the canonical vocabulary and schema rules for:
• Tags
• Flags
• Reason codes
• Outcome records
• Operational record families that are contract-governed

These constructs exist to make pipeline behaviour explicit, machine-readable, and auditable.
This document is normative.

Stages and services MUST NOT invent ad-hoc flags, overload meanings, or rely on free-text explanations in place of enumerated codes.

====================================================================
DESIGN PRINCIPLES

Explicit over implicit
No outcome may be inferred from absence, emptiness, silence, or defaults.

Enumerated over free-text
Every reason explaining why something occurred MUST be an enumerated code.
Free-text MAY exist only as supplemental detail and MUST NOT carry semantics.

Orthogonality
• Tags classify
• Flags describe state
• Reasons explain outcomes
These roles MUST NOT be conflated.

Stability
Once introduced, a flag or reason code MUST NOT change meaning.
Expansion is allowed only by adding new codes.

Uniform fingerprinting (deprecated-tool detection)
Canonical artifacts MUST be able to declare explicitly whether they were produced by deprecated code or deprecated semantic configuration.

This is done using a uniform field named artifact_fingerprint.

artifact_fingerprint is a field, not a record_type, and MUST be present in canonical outputs as follows:
• Single-JSON canonical artifacts: top-level field "artifact_fingerprint"
• JSONL canonical artifacts: in the stage outcome record (the final record by contract)

artifact_fingerprint MUST be schema-versioned and MUST include at minimum:
• schema_version
• stage
• producer
• producer_sha256
• fingerprint_id
• fingerprint_version
• config_sha256
• config

Rules:
• Consumers MUST treat a missing or mismatched artifact_fingerprint as deprecated.
• Consumers MUST NOT infer tool currency from secondary cues (e.g. model strings, file mtimes, incidental runtime fields).
• Legacy artifacts may exist without artifact_fingerprint, but they are considered eligible for reprocessing under the deprecated-tool policy.

====================================================================
CANONICAL OPERATIONAL LOG ROOT

Single operational log root:
• /workspace/pipeline/logs.all

Rules:
• All operational artifacts and tool audit records MUST be written under /workspace/pipeline/logs.all.
• Operational artifacts MUST NOT be written under /workspace/logs or /workspace/runstate.
• Operational artifacts are contract-governed (schema and reason codes), but they are not semantic truth. They reference canonical semantic artifacts under /workspace/catalog.

Required operational record placement:
• service_runstate, service_error, tool_run_manifest, tool_contracts_outcome, tool_media_watch_outcome MUST be stored under logs.all.
• All referenced log_path values in operational records MUST resolve under logs.all.
• All operational JSON writes MUST be atomic. JSONL streams MUST be append-only and must never be zero-byte.

====================================================================
CONTROL SURFACE CONTRACT (LAUNCHER + PREFLIGHT)

The following files are contract-governed control surfaces:
• bin/media_watch_launch_detached.sh
• bin/preflight_guard.sh

Launcher invariants (mandatory):
• MUST include set -o pipefail.
• MUST NOT include set -e or set -u tokens.
• MUST NOT include forbidden tokens:
– NOHUP_OUT
– nohup_out
– media_watch.nohup
• MUST include required controls:
– PIDFILE=
– LOGFILE=
– exactly one nohup launch stanza
– --log "$LOGFILE" in the nohup daemon command
• MUST contain exactly one esac line.
• MUST NOT include duplicate branch/control fragments that create ambiguous stop/status/start semantics.
• MUST NOT include adjacent duplicate assignment statements for the same variable in the same block.

Preflight enforcement (mandatory):
• preflight_guard.sh MUST run with strict shell safety (set -euo pipefail).
• preflight MUST parse-check launcher syntax before invariant checks.
• preflight forbidden-pattern matcher MUST use explicit token/line-aware matching (not ambiguous broad substrings).
• preflight MUST enforce:
– exactly one nohup invocation (^[[:space:]]*nohup[[:space:]])
– exactly one esac line
– required --log "$LOGFILE" usage
• preflight failures MUST be explicit and non-zero, and MUST NOT silently pass partial checks.
• preflight context checks MUST resolve repo root using git rev-parse --show-toplevel and evaluate launcher path relative to that root.

Operational validation evidence expected from guard runs:
• launcher syntax result
• preflight syntax result
• forbidden-pattern scan output (or explicit absent marker)
• esac count
• nohup count
• HEAD and relevant file hash fingerprints

====================================================================
TOP-LEVEL RECORD TYPES

All JSONL records MUST declare record_type.

Single-JSON canonical artifacts MUST declare record_type unless the stage’s canonical artifact schema is an explicit, schema-versioned family defined by this contract and enforced by deterministic checker(s). In such cases, schema_version + required fields are the canonical identifier, and record_type may be absent.

Canonical pipeline record_type values:
• media_manifest
• asr_result
• asr_qc
• enrich_result
• motion_flow
• scenes
• chunk
• chunking_outcome
• embedding_result
• embedding_outcome
• event_record
• events_outcome
• entity_record
• entities_outcome
• entity_node
• entity_mention
• entity_link_outcome
• stage_status
• stage_run_manifest
• stageC_build_manifest
• audit_record
• tool_run_manifest
• tool_contracts_outcome

Service and tool operational artifacts (contract-governed, written under logs.all):
• service_runstate
• service_error
• tool_media_watch_outcome

Rules:
• If a stage produces no primary records, it MUST emit an explicit *_outcome record instead.
• If a service produces no primary operational records, it MUST emit a service_error record instead.
• Zero-byte artifacts are forbidden for all record types.

====================================================================
TAGS
Tags are descriptive and non-exclusive.
They MUST NOT be used to signal success or failure.

Canonical tag namespaces:
• media.*
• stage.*
• content.*
• quality.*
• system.*
• service.*
• event.*
• entity.*

Examples:
• media.silent
• content.non_speech
• content.unedited
• content.continuous_take
• content.temporally_unsegmented
• quality.low_confidence
• stage.optional
• system.catalog_scoped
• system.non_canonical_compute
• service.read_only
• service.snapshot_only
• event.derived
• event.observed
• event.text_first
• event.fallback_no_scenes
• entity.derived
• entity.candidate
• entity.filtered

Rules:
• Tags MAY be extended.
• Tags MUST follow namespace conventions.
• Tags MUST NOT encode outcome or causality.

====================================================================
FLAGS
Flags are boolean indicators of state.
Flags MUST NOT encode causality.

Canonical flags include:
• ok
• skipped
• partial
• degraded
• invalid
• blocked
• read_only
• catalog_scoped

Rules:
• ok=true does NOT imply “no issues”
• skipped=true MUST be accompanied by explicit reasons
• blocked=true indicates an external constraint, not a logic failure

====================================================================
REASONS (ENUMERATED)
Reasons explain why an outcome occurred.
Reasons MUST be enumerated codes, never prose.

Global rule (no empty enumerations):
• Enumerated fields (reason, reasons entries, decision_reason, etc) MUST NOT be empty strings.
• If a field is present, it MUST be a valid enumerated code.
• If a field is not applicable, it MUST be omitted, or set to an explicit enumerated sentinel (preferred).
• Empty strings MUST NOT be used as “not applicable”.

ASR / INGEST REASONS (STAGE A)
• NO_AUDIO_TRACK
• SILENT_AUDIO
• NON_SPEECH_AUDIO
• ASR_MODEL_ERROR
• ASR_TIMEOUT
• OK

Rules:
• Raw backend traces MAY be recorded separately (e.g. reason_raw)
• Enumerated reason codes are canonical and authoritative
• OK is the canonical success reason when empty_transcript=false

MOTION (STAGE B2) REASONS
• NO_VIDEO_FRAMES
• VIDEO_TOO_SHORT
• FLOW_COMPUTATION_ERROR

Rules:
• Zero motion magnitude is NOT a failure
• Motion artifacts MUST still exist with explicit reasons when skipped

TEMPORAL SEGMENTATION (STAGE B3) REASONS
• NO_CUTS_DETECTED
• NO_TEMPORAL_BOUNDARIES
• VIDEO_TOO_SHORT
• SCENE_DETECTOR_ERROR
• MISSING_INPUT
• DETECT_FAILED

Rules:
• NO_CUTS_DETECTED with ok=true is a valid outcome
• NO_TEMPORAL_BOUNDARIES indicates continuous or structurally uniform footage
• Unedited single-take footage is expected to produce these outcomes frequently

EVENT DERIVATION (STAGE E0) REASONS
• MISSING_INPUT
• INPUT_SCHEMA_INVALID
• NO_ELIGIBLE_SPANS
• ZERO_EVENTS_VALID
• DERIVATION_FAILED
• DERIVATION_SUCCESS_NO_CLASSIFICATION
• OK

Rules:
• ZERO_EVENTS_VALID with ok=true is a valid outcome
• DERIVATION_SUCCESS_NO_CLASSIFICATION SHOULD be preferred over OK when the implementation distinguishes this condition
• OK is permitted as a sentinel success reason when Stage E0 completes successfully and no more specific success reason applies
• Events are derived artifacts, absence of events MUST be explicit
• Failure to derive events MUST NOT prevent upstream artifacts from remaining valid

ENTITY EXTRACTION (STAGE F0) REASONS
• MISSING_INPUT
• INPUT_SCHEMA_INVALID
• TOOL_DEPENDENCY_MISSING
• NO_ELIGIBLE_SPANS
• NO_ELIGIBLE_MENTIONS
• OK

Rules:
• entities_outcome MUST exist even if zero entity_record entries are produced
• Absence of entities MUST NOT be inferred from empty files
• OK is permitted as a sentinel success reason when Stage F0 completes successfully and no more specific success reason applies

ENTITY LINKING (STAGE F1) REASONS
• MISSING_INPUT
• INPUT_SCHEMA_INVALID
• NO_INPUT_MENTIONS
• LINK_FAILED
• FILTER_APPLIED
• OK

Rules:
• entity_link_outcome MUST exist even if zero entity_node entries are produced
• Filtering MUST be explicit and auditable, FILTER_APPLIED is used when deterministic filtering affects results
• NO_INPUT_MENTIONS indicates Stage F1 ran but no mention inputs were present

CHUNKING (STAGE B0) REASONS
• ASR_SEGMENTS_EMPTY
• ASR_FILE_MISSING
• ASR_SCHEMA_INVALID
• OK

Rules:
• If ASR contains zero segments: chunks/.jsonl MUST contain exactly one chunking_outcome record with reason=ASR_SEGMENTS_EMPTY, and MUST NOT contain chunk records.
• If ASR contains one or more segments and chunking completes successfully: chunking_outcome MUST exist as final record with reason=OK.

EMBEDDING (STAGE B1) REASONS
• NO_ELIGIBLE_CHUNKS
• EMBEDDING_MODEL_ERROR
• EMBEDDING_SCHEMA_MISMATCH
• EMBEDDING_DIMENSION_MISMATCH
• OK

Rules:
• embeddings/.json MUST exist even when no vectors are produced
• Canonical embedding identity MUST be represented using authoritative content hashes:
– onnx_model_sha256
– tokenizer_sha256
– any additional preprocessing config sha256 if used
model_id MAY be present but is non-authoritative.

STAGE C (MILVUS MATERIALISATION) REASONS
Publish / materialisation reasons:
• FINAL_DB_IN_USE
• FINAL_DB_LOCKED
• FINAL_DB_PERMISSION_DENIED
• MILVUS_INIT_FAILED
• MILVUS_SCHEMA_MISMATCH
• VALIDATION_FAILED
• PUBLISH_RETRIES_EXHAUSTED

Definitions:
FINAL_DB_IN_USE
The target database file (moments.db) has one or more active OS-level open handles at publish time.

FINAL_DB_LOCKED
An explicit file lock or exclusive lock prevents replacement.

Rules:
• Only concrete, observable conditions are valid reasons
• Service presence, ports, or PID existence MUST NOT be used as reasons

SERVICE / RUNTIME REASONS
Lifecycle:
• READ_ONLY_MODE
• DEPENDENCY_UNAVAILABLE
• IDLE_CLOSED
• SERVICE_SHUTDOWN
• PORT_BIND_IN_USE
• HEALTH_TIMEOUT

Snapshot refresh:
• SNAPSHOT_NO_CATALOG
• SNAPSHOT_UP_TO_DATE
• SNAPSHOT_REFRESHED
• SNAPSHOT_MKDIR_FAILED
• SNAPSHOT_COPY_FAILED
• SNAPSHOT_UNKNOWN_ERROR

Operational hygiene:
• LEGACY_ZERO_BYTE_ARTIFACT
• ATOMIC_WRITE_FAILED
• OP_LOGROOT_INVALID
• OP_LOGWRITE_OUTSIDE_LOGROOT
• LAUNCHER_INVARIANT_FAILED
• PREFLIGHT_INVARIANT_FAILED
• REPO_CONTEXT_INVALID

WATCHER / ORCHESTRATION REASONS (media_watch_daemon and similar tooling)
These reasons are used in operational audit records that describe orchestration decisions. They are not stage semantics and must not be used to infer canonical artifact correctness.

• NEW_FILE
A file was newly discovered and selected for processing.

• FILE_UNSTABLE
A file did not meet stability criteria (size/mtime settling) and was not processed.

• MAX_PER_SCAN_BACKPRESSURE
A file was eligible but was deferred due to max-per-scan throttling.

• ALREADY_COMPLIANT
The file was assessed as already satisfying required pipeline conditions and was not processed.

• INELIGIBLE_FOR_EMBEDDING
chunking_outcome.eligibility indicates eligible_for_embedding=false, downstream semantic stages were intentionally skipped.

• UPSTREAM_IDENTITY_MISMATCH_CHUNKS_SHA
Embeddings exist but reference a different chunks_artifact_sha256 than the current chunks JSONL sha256, B1 must be rerun.

• STAGE_FAILED
One or more invoked stages exited non-zero.

• CONTRACTS_FAILED
run_contracts exited non-zero or reported FAILED checkers.

• POLICY_UNREADABLE
Policy file could not be parsed, orchestration proceeded using an empty/default policy.

• POLICY_SCHEMA_MISMATCH
Policy schema_version mismatch, orchestration proceeded using an empty/default policy.

• CHUNKING_OUTCOME_MISSING
chunking_outcome could not be read or was not present when expected.

Counting rule for summaries:
• ALREADY_COMPLIANT and INELIGIBLE_FOR_EMBEDDING MUST be counted separately even if start_stage="NONE".
Reason codes are authoritative.

TOOLING / EXECUTION REASONS
• WRONG_EXECUTION_ENVIRONMENT
• TOOL_DEPENDENCY_MISSING
• TOOL_EXIT_NONZERO
• TOOL_TIMEOUT
• TOOL_PRECONDITION_FAILED

====================================================================
OUTCOME RECORDS
Outcome records exist to prevent inference.

chunking_outcome
Required fields:
• record_type = "chunking_outcome"
• media_id
• reason (enumerated, including ASR_SEGMENTS_EMPTY or OK)
• asr_schema_version
• asr_artifact_sha256
• chunking_config_sha256
• policy (optional)
• quality (required, schema_versioned object)
• eligibility (required, schema_versioned object)

Rules:
• chunking_outcome MUST exist as the final record in chunks/.jsonl
• chunking_outcome MUST exist even if zero chunk records are produced
• eligibility MUST explicitly include eligible_for_embedding and eligible_for_indexing
• Zero chunks MUST NOT be inferred from empty files

embedding_result (embeddings/<media_id>.json)
Required fields:
• media_id
• model (including model_id and model_revision)
• embed_config
• embed_config_sha256
• chunks_artifact_sha256
• created_utc
• runtime
• vectors
• artifact_fingerprint (required, missing implies deprecated)

embedding_outcome
Required fields:
• record_type = "embedding_outcome"
• media_id
• ok = false
• skipped = true
• model_id (non-authoritative label)
• onnx_model_sha256 (authoritative)
• tokenizer_sha256 (authoritative)
• reasons

events_outcome
Required fields:
• record_type = "events_outcome"
• media_id
• ok
• skipped
• partial
• reasons
• input_identities (source artifact hashes or ids)
• event_count
• artifact_fingerprint (required, missing implies deprecated)

Rules:
• events_outcome MUST exist even if zero event_record entries are produced
• Zero events MUST NOT be inferred from empty files

event_record
Required fields:
• record_type = "event_record"
• media_id
• event_id
• t0_ms
• t1_ms
• event_type
• tags
• flags
• reasons
• evidence (references to canonical artifacts)
• provenance (derivation inputs and hashes)

Rules:
• event_id MUST be deterministically derived from stable inputs
• event_record MUST NOT exist without a corresponding events_outcome

entities_outcome
Required fields:
• record_type = "entities_outcome"
• media_id
• ok
• skipped
• partial
• reasons
• input_identities (source artifact hashes or ids)
• entity_count
• artifact_fingerprint (required, missing implies deprecated)

Rules:
• entities_outcome MUST exist even if zero entity_record entries are produced
• Zero entities MUST NOT be inferred from empty files

entity_record
Required fields:
• record_type = "entity_record"
• media_id
• entity_id
• scope
• scope_id
• start_char
• end_char
• entity_text
• entity_type
• tags
• flags
• reasons
• evidence

Rules:
• entity_id MUST be deterministically derived from stable inputs
• entity_record MUST NOT exist without a corresponding entities_outcome

entity_link_outcome
Required fields:
• record_type = "entity_link_outcome"
• media_id
• ok
• skipped
• partial
• reasons
• input_identities (source artifact hashes or ids)
• node_count
• mention_count
• artifact_fingerprint (required for newly produced artifacts, missing implies deprecated)

Rules:
• entity_link_outcome MUST exist even if zero entity_node entries are produced
• Filtering summaries MAY be included but MUST NOT replace reasons

entity_node
Required fields:
• record_type = "entity_node"
• media_id
• entity_node_id
• entity_type
• display_name
• mention_ids
• support (at minimum mention_count and sources)
• provenance

Rules:
• entity_node_id MUST be deterministically derived from stable inputs
• entity_node MUST NOT exist without a corresponding entity_link_outcome

entity_mention
Required fields:
• record_type = "entity_mention"
• media_id
• mention_id
• entity_node_id
• scope
• scope_id
• start_char
• end_char
• entity_text
• entity_type_observed
• extractor
• extractor_version
• confidence
• tags
• evidence

Rules:
• mention_id MUST be deterministically derived from stable inputs
• entity_mention MUST reference an existing entity_node_id within the same MEDIA_ID

stage_status
Required fields:
• stage
• media_id (or "catalog")
• ok
• skipped
• blocked
• reasons
• warnings (optional)

tool_run_manifest (operational, written under logs.all)
Required fields:
• record_type = "tool_run_manifest"
• tool
• ts_utc
• run_id
• execution_environment
• command
• exit_code
• ok
• reasons (if ok=false)
• outputs_written

tool_contracts_outcome (operational, written under logs.all)
Required fields:
• record_type = "tool_contracts_outcome"
• tool = "run_contracts"
• run_id
• created_utc
• media_id
• ok
• results (per-checker statuses and details)

tool_media_watch_outcome (operational, contract-governed, written under logs.all)
Required fields:
• record_type = "tool_media_watch_outcome"
• schema_version
• run_id
• created_utc
• media_id
• ok
• decision
• decision_reason (enumerated watcher/orchestration reason)
• start_stage
• logroot (MUST equal /workspace/pipeline/logs.all)
• policy_path
• policy_sha256
• python_executable
• input (includes input_path, input_bytes_sha256, input_size_bytes, input_mtime_epoch, settle_seconds, scan_interval_s, max_per_scan, max_depth)
• stages (list of invoked stage entries, each including: stage, cmd, exit_code, duration_ms, log_path, log_sha256, log_bytes)
• artifacts (hash snapshot for relevant canonical artifacts)
• contracts_audit_path (required when contracts stage was invoked)
• contracts_audit_sha256 (required when contracts_audit_path exists)
• contracts_summary (best-effort summary of tool_contracts_outcome)

Optional fields:
• identity_mismatches (structured mismatch evidence, e.g. chunks sha mismatch)
• warnings (enumerated list)
• reasons (enumerated map, additive detail, must not conflict with decision_reason)

Rules:
• tool_media_watch_outcome MUST NOT be treated as semantic truth. It is operational evidence of orchestration and references canonical artifacts.
• Canonical truth remains the per-stage artifacts and /workspace/catalog/audit entries.
• log_path entries MUST resolve under logroot.

service_runstate (operational, written under logs.all)
Required fields:
• record_type = "service_runstate"
• service
• ts_utc
• run_id
• pid
• policy_flags
• code identity (app_file, app_sha256)
• key path stats

Rules:
• MUST be written atomically
• MUST NOT be zero-byte
• MUST be stored under logs.all

service_error (operational, written under logs.all)
Required fields:
• record_type = "service_error"
• service
• ts_utc
• run_id
• ok = false
• blocked or invalid = true
• reasons (enumerated)

Rules:
• MUST be written atomically
• MUST NOT be zero-byte
• MUST be stored under logs.all

====================================================================
WARNINGS
Warnings indicate degraded or noteworthy conditions that do not invalidate the result.

Examples:
• reduced_sample_size
• short_clip
• partial_validation

Rules:
• Warnings MUST NOT replace reasons
• Warnings MUST NOT encode failure

====================================================================
AUDIT VS STATUS
Audit records:
• Immutable
• Describe what happened
• Capture hashes, counts, timings

Status records:
• Summarise outcome
• Intended for orchestration and monitoring
• MAY be superseded by later runs

====================================================================
PROHIBITIONS
The following are forbidden:
• Inferring success from file existence
• Inferring failure from file absence
• Using empty arrays to imply “nothing happened”
• Using prose-only explanations without enumerated reasons
• Encoding state in filenames alone
• Using free-text fields to override enumerated reason codes
• Using empty strings for enumerated fields (reason, decision_reason, reasons entries, etc)
• Writing operational logs or runstate outside /workspace/pipeline/logs.all
• Treating launcher/preflight invariant failures as warnings-only conditions

====================================================================
CHANGE CONTROL
This contract MUST be updated when:
• A new reason code is introduced
• A new record_type is added
• Semantics of an existing flag change

Version increments are mandatory.

====================================================================
APPENDIX A — NON-CANONICAL COMPUTE ENVIRONMENTS (INFORMATIONAL)
This appendix is informational and does not introduce new record_type values, flags, or mandatory behaviours.
Its purpose is to clarify how optional, non-canonical execution environments (e.g. GPU-based vision tooling) are represented without violating pipeline invariants.

VISION_GPU / HEAVY VISION PROCESSING
Certain tooling (e.g. segmentation, matting, object masks, dense vision models) may execute in non-canonical environments such as:
• GPU-enabled containers
• CUDA-dependent virtual environments
• Experimental or exploratory compute contexts

These environments are referred to collectively as vision_gpu.

Rules:
• vision_gpu is NOT a pipeline stage.
• vision_gpu MUST NOT introduce new pipeline invariants.
• No canonical stage may require vision_gpu in order to be correct.
• Outputs produced by vision_gpu tooling MUST conform to existing artifact schemas and invariants if they are written into the pipeline.

REPRESENTATION IN METADATA
vision_gpu involvement MUST be represented using existing mechanisms only:

Tags:
• system.non_canonical_compute
• system.gpu_accelerated
• stage.experimental

Flags:
• partial
• degraded
• ok (as applicable)

Reasons:
• MUST use existing enumerated reasons.
• No vision_gpu-specific reason codes are introduced by this appendix.

PROHIBITIONS
The following are explicitly forbidden:
• Introducing vision_gpu-specific record_type values
• Encoding GPU usage as a success or failure signal
• Inferring higher authority or correctness from GPU execution
• Allowing vision_gpu artifacts to bypass validation or audit rules
• Making any canonical stage depend on GPU availability

RATIONALE
vision_gpu exists to enable optional acceleration and experimentation without weakening determinism, auditability, or offline correctness.
This appendix ensures such tooling can coexist with the pipeline without expanding the contract surface or introducing ambiguity.

====================================================================
END OF CONTRACT