LOGGING_OVERHAUL_AUDIT.md
Version: 0.1
Updated: 2026-02-04 (Australia/Melbourne)
Scope: /workspace/pipeline/bin (canonical stage entrypoints and orchestration)

====================================================================
GOAL

Bring every pipeline entrypoint that writes logs (directly or indirectly) into full compliance with the canonical operational logging protocol:

- Single operational write root: /workspace/pipeline/logs.all
- No operational logs or runstate anywhere else (including /workspace/pipeline/logs, /logs, /workspace/logs, ad hoc *.log files)
- Explicit, audit-friendly, deterministic run records:
  - per-run stdout/stderr capture
  - per-run meta (cmd, exit_code, duration, bytes, sha256, run_id, media_id)
  - explicit outcomes and reason codes (never infer from absence/emptiness)
- Atomic writes for JSON, append-only JSONL where appropriate
- No zero-byte log artifacts unless explicitly defined as valid

====================================================================
HOW THIS AUDIT IS BUILT

Inputs:
- ripgrep scans over bin/ for:
  - python logging usage (logging.*, FileHandler, print(), open(*log))
  - shell redirection / tee (>>, 2>&1, &>>, tee)
  - old log paths (/logs/, /workspace/pipeline/logs, media_watch.log, watch_state, *.log)

This document tracks:
- Every file that needs changes
- Why it needs changes (concrete failure modes)
- What was changed
- What remains
- Second-pass items that can be missed on first conversion (common traps)

====================================================================
STATUS LEGEND

[TODO] Not started
[IN-PROGRESS] Actively being rewritten
[DONE] Updated to logs.all protocol and reviewed for compliance
[DEFER] Known issue but intentionally delayed (must include justification)

====================================================================
COMPLETED

Currently none.

====================================================================
FILES REQUIRING OVERHAUL (PRIORITISED)

 bin/stageC_milvus_materialize.py 
 

A) Shell orchestrators / wrappers (highest priority: they control stdout/stderr routing)
These files show heavy use of redirects/tee and are likely writing logs to non-canonical locations.

[TODO] bin/stageA_one.sh
- Evidence:
  - 34 matches for redirects/tee patterns in bin scan
- Likely issues:
  - Redirecting stdout/stderr into local *.log, /workspace/pipeline/logs, or similar
  - Missing per-run meta record (exit_code, bytes, sha256, cmd)
  - Non-atomic write patterns for runstate
- Required changes:
  - Route all stdout/stderr to logs.all run directory
  - Write meta JSON for the run (or call a shared logger helper)
  - Ensure explicit success/skip/fail outcome is recorded

[TODO] bin/stageB_one.sh
- Evidence:
  - 18 matches for redirects/tee patterns
- Same required changes as stageA_one.sh

[TODO] bin/pipeline_run.sh
- Evidence:
  - 11 matches for redirects/tee patterns
- Likely issues:
  - Aggregates multiple stages, may currently smear logs across paths or reuse files
- Required changes:
  - Run-scoped directory under logs.all, stage-scoped subdirs
  - Ensure every stage invocation has its own meta.json and captured stdout/stderr

[TODO] bin/stageA_launch_detached.sh
- Evidence:
  - shows in “old logs paths” scan
- Likely issues:
  - background process runstate written outside logs.all
  - PID files, watch_state, etc
- Required changes:
  - canonical runstate only under logs.all
  - explicit heartbeat/integrity signals if applicable

[TODO] bin/run_media_webroot.sh
- Evidence:
  - shows in “old logs paths” scan; 5 matches of redirects/tee
- Likely issues:
  - service logs going to /workspace/pipeline/logs or ad hoc files
- Required changes:
  - service operational logs + runstate only under logs.all
  - readiness checks must not be inferred from process existence

[TODO] bin/preflight_stageA.sh
- Evidence:
  - 5 matches; also appears in old-path scan
- Likely issues:
  - diagnostic output not captured deterministically
- Required changes:
  - write explicit preflight outcome record and log capture

[TODO] bin/stageA_regression_smoke.sh
- Evidence:
  - 9 matches; appears in old-path scan
- Required changes:
  - same as above, plus ensure “smoke pass” has explicit result artifact, not inferred

[TODO] bin/example_asr_run.sh
- Evidence:
  - appears in old-path scan
- Required changes:
  - ensure it does not write to /workspace/pipeline/logs or ad hoc *.log

[TODO] bin/run_all_from_scratch.sh
[TODO] bin/run_all_from_scratch.py
- Evidence:
  - both appear in old-path scan
- Required changes:
  - ensure they don’t bypass canonical wrapper logging, and don’t write ad hoc logs

[TODO] bin/media_watch_launch_detached.sh
- Evidence:
  - top offender in old-path scan (5)
- Likely issues:
  - writes media_watch.log / watch_state outside logs.all
  - background daemon lifecycle not producing explicit runstate and heartbeat in logs.all
- Required changes:
  - move ALL watcher runstate into logs.all
  - ensure heartbeat.json + manifest/latest.json requirements are respected (writer responsibility must be clear)

B) Python stage scripts with logging calls (second priority: convert to structured + wrapper-captured stdout/stderr)

NOTE: Many entries have .ipynb_checkpoints and -CopyN variants. Those are non-canonical and should NOT be treated as entrypoints. This list targets canonical names only.

[TODO] bin/stageB1_search_local.py (20 hits)
- Likely issues:
  - print() / ad hoc logging
  - may write logs directly or assume wrapper redirection
- Required changes:
  - adopt structured logging event envelope (or stdout JSONL) compatible with logs.all capture
  - ensure explicit outcomes/reason codes

[TODO] bin/stageB1_embed_e5_onnx.py (10 hits)
[TODO] bin/stageB1_embed_e5.py (6 hits)
- Likely issues:
  - embedding eligibility and outcome reporting can’t be inferred from vectors=[] or missing files
  - must record explicit “NO_ELIGIBLE_CHUNKS” etc
- Required changes:
  - ensure artifact + operational logging both satisfy “explicit outcomes”

[TODO] bin/media_watch_daemon.py (10 hits; also appears in old-path scan)
- Likely issues:
  - direct writes to media_watch.log, watch_state, /logs, etc
- Required changes:
  - move all operational outputs to logs.all
  - write heartbeat + manifest responsibilities clarified

[TODO] bin/audit_index.py (10 hits)
[TODO] bin/audit_one.sh (2 hits but important by role)
- Likely issues:
  - audit tooling must be immutable and provenance-safe, and must not leak operational logs elsewhere
- Required changes:
  - ensure audit outputs are separate from operational logs; operational logs go under logs.all

[TODO] bin/check_config.py (9 hits)
- Likely issues:
  - prints without explicit structured outcome
- Required changes:
  - explicit outcome record

[TODO] bin/run_batch.py (5 hits; also appears in old-path scan)
- Likely issues:
  - orchestrator in python, may be writing logs to old paths
- Required changes:
  - align with logs.all run directories, per-stage meta, explicit outcomes

[TODO] bin/stageF0_contract_check.py (6 hits)
[TODO] bin/stageF1_contract_check.py (5 hits)
[TODO] bin/stageE0_contract_check.py (3 hits)
[TODO] bin/contract_check_global.py (4 hits)
[TODO] bin/run_contracts.py (5 hits)
- Likely issues:
  - contract checks must emit explicit pass/fail details and reason codes, not just stdout text
- Required changes:
  - structured outcome artifact + logs captured under logs.all

[TODO] bin/stageF0_entities.py (5 hits)
[TODO] bin/stageE0_events.py (3 hits)
[TODO] bin/stageB2_motion_flow.py (5 hits)
- Likely issues:
  - may emit empty outputs without explicit reason code records
- Required changes:
  - ensure “empty but valid” vs “skipped/ineligible” is explicit

[TODO] bin/query_audit.py (6 hits)
[TODO] bin/query_moments_asr.py (5 hits)
- Likely issues:
  - tooling scripts often write ad hoc logs or assume interactive use
- Required changes:
  - if they write operational logs at all, it must be under logs.all; otherwise keep them purely stdout and let wrapper capture

====================================================================
SECOND-PASS CHECKLIST (COMMONLY MISSED)

1) Remove all writes to:
- /workspace/pipeline/logs
- /logs
- /workspace/logs
- media_watch.log / watch_state
- any hard-coded *.log filenames

2) Ensure every wrapper captures:
- stdout and stderr separately (or explicitly combined), with deterministic filenames
- meta.json including exit_code, duration, bytes, sha256, cmd, run_id, media_id

3) Ensure Python scripts do NOT:
- create FileHandlers pointing outside logs.all
- silently “pass” on exceptions without emitting explicit failure outcomes

4) Ensure empty outputs are explicit:
- “no work to do” vs “ineligible” vs “failed” are distinct outcomes with enumerated reasons

5) Confirm Copyparty visibility:
- logs.all integrity signals (heartbeat.json, manifest/latest.json) are updated and not served as stubs

====================================================================
NEXT TARGET

Immediate next file to convert: bin/stageA_one.sh
Reason: highest redirect/tee density, most likely to be the root cause of widespread non-canonical log writes.