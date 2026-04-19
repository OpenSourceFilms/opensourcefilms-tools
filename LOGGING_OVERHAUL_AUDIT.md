LOGGING_OVERHAUL_AUDIT.md

Version: 0.2
Updated: 2026-02-11 (Australia/Melbourne)
Scope: /workspace/pipeline/bin (canonical stage entrypoints and orchestration)

====================================================================
GOAL

Bring every pipeline entrypoint that writes logs (directly or indirectly) into full compliance with the canonical operational logging protocol:

Single operational write root: /workspace/pipeline/logs.all

No operational logs or runstate anywhere else (including /workspace/pipeline/logs, /logs, /workspace/logs, ad hoc *.log files)

Explicit, audit-friendly, deterministic run records:

per-run stdout/stderr capture

per-run meta (cmd, exit_code, duration, bytes, sha256, run_id, media_id)

explicit outcomes and reason codes (never infer from absence/emptiness)

Atomic writes for JSON, append-only JSONL where appropriate

No zero-byte log artifacts unless explicitly defined as valid

This is a compliance remediation and governance-hardening stream, not a feature stream.

====================================================================
HOW THIS AUDIT IS BUILT

Inputs:

ripgrep scans over bin/ for:

python logging usage (logging.*, FileHandler, print(), open(*log))

shell redirection / tee (>>, 2>&1, &>>, tee)

old log paths (/logs/, /workspace/pipeline/logs, media_watch.log, watch_state, *.log)

direct file inspection of canonical entrypoints

contract cross-check against:

MASTER.md

METADATA_TAGS_AND_FLAGS_CONTRACT.md

PIPELINE_WRITE_AND_INTERACTION_MAP.md

This document tracks:

Every file that needs changes

Why it needs changes (concrete failure modes)

What was changed

What remains

Second-pass items that can be missed on first conversion (common traps)

====================================================================
STATUS LEGEND

[TODO] Not started
[IN-PROGRESS] Actively being rewritten
[DONE] Updated to logs.all protocol and reviewed for compliance
[DEFER] Known issue but intentionally delayed (must include justification)

====================================================================
SESSION DELTA (2026-02-11)

Added overnight contract-audit capability in tooling stream (new scripted audit path introduced in ops flow).

Confirmed logs.all remains canonical single operational root in governance direction.

Confirmed need to harden launcher/preflight behavior so invariant breaches are hard-fail, not warning-only.

Confirmed need for explicit governance of operational control surfaces (media_watch launcher, preflight guard family), with artifacts under logs.all.

No stage semantic contract expansion in this update. This remains a logging/compliance remediation layer.

====================================================================
COMPLETED

[DONE] Logging overhaul audit baseline established and normalized to current governance date.
[DONE] Priority backlog revalidated against canonical path policy (logs.all-only).
[DONE] Non-canonical backup entrypoints (.ipynb_checkpoints, -CopyN) explicitly excluded from canonical remediation scope.

Note:
“Completed” here means audit governance and prioritization artifacts are now current and actionable. It does not imply all listed scripts are already remediated.

====================================================================
FILES REQUIRING OVERHAUL (PRIORITISED)

bin/stageC_milvus_materialize.py

A) Shell orchestrators / wrappers (highest priority: they control stdout/stderr routing)
These files show heavy use of redirects/tee and are likely writing logs to non-canonical locations.

[IN-PROGRESS] bin/stageA_one.sh

Evidence:

high redirect/tee density in scan

Likely issues:

Redirecting stdout/stderr into local *.log, /workspace/pipeline/logs, or similar

Missing per-run meta record (exit_code, bytes, sha256, cmd)

Non-atomic write patterns for runstate

Required changes:

Route all stdout/stderr to logs.all run directory

Write meta JSON for the run (or call a shared logger helper)

Ensure explicit success/skip/fail outcome is recorded

[TODO] bin/stageB_one.sh

Evidence:

heavy redirect/tee patterns

Same required changes as stageA_one.sh

[TODO] bin/pipeline_run.sh

Evidence:

multiple redirect/tee patterns

Likely issues:

Aggregates multiple stages, may currently smear logs across paths or reuse files

Required changes:

Run-scoped directory under logs.all, stage-scoped subdirs

Ensure every stage invocation has its own meta.json and captured stdout/stderr

[TODO] bin/stageA_launch_detached.sh

Evidence:

appears in old-path scans

Likely issues:

background process runstate written outside logs.all

PID files, watch_state, etc

Required changes:

canonical runstate only under logs.all

explicit heartbeat/integrity signals if applicable

[TODO] bin/run_media_webroot.sh

Evidence:

appears in old-path scans plus redirect/tee usage

Likely issues:

service logs going to /workspace/pipeline/logs or ad hoc files

Required changes:

service operational logs + runstate only under logs.all

readiness checks must not be inferred from process existence

[TODO] bin/preflight_stageA.sh

Evidence:

appears in old-path and redirect scans

Likely issues:

diagnostic output not captured deterministically

Required changes:

write explicit preflight outcome record and log capture

ensure invariant violations are hard-fail outcomes

[TODO] bin/preflight_guard.sh

Evidence:

control-plane guard surface requiring explicit governance

Likely issues:

path/root assumptions can drift without explicit run outcome

Required changes:

dynamic repo-root resolution (git top-level)

explicit TOOL_PRECONDITION_FAILED on guard breach

operational outcome artifact under logs.all/tools/preflight

[TODO] bin/stageA_regression_smoke.sh

Evidence:

appears in old-path scan

Required changes:

same as preflight family, plus ensure “smoke pass” has explicit result artifact, not inferred

[TODO] bin/example_asr_run.sh

Evidence:

appears in old-path scan

Required changes:

ensure it does not write to /workspace/pipeline/logs or ad hoc *.log

[TODO] bin/run_all_from_scratch.sh
[TODO] bin/run_all_from_scratch.py

Evidence:

both appear in old-path scan

Required changes:

ensure they don’t bypass canonical wrapper logging and don’t write ad hoc logs

[TODO] bin/media_watch_launch_detached.sh

Evidence:

top offender in old-path scans

Likely issues:

writes media_watch.log / watch_state outside logs.all

background daemon lifecycle not producing explicit runstate and heartbeat in logs.all

Required changes:

move ALL watcher runstate into logs.all

ensure heartbeat.json + manifest/latest.json responsibilities are explicit

ensure launcher invariant failures are hard-fail, not warning-only

B) Python stage scripts with logging calls (second priority: convert to structured + wrapper-captured stdout/stderr)

NOTE: Many entries have .ipynb_checkpoints and -CopyN variants. Those are non-canonical and should NOT be treated as entrypoints. This list targets canonical names only.

[TODO] bin/stageB1_search_local.py

Likely issues:

print() / ad hoc logging

may write logs directly or assume wrapper redirection

Required changes:

adopt structured logging event envelope (or stdout JSONL) compatible with logs.all capture

ensure explicit outcomes/reason codes

[TODO] bin/stageB1_embed_e5_onnx.py
[TODO] bin/stageB1_embed_e5.py

Likely issues:

embedding eligibility and outcome reporting can’t be inferred from vectors=[] or missing files

must record explicit NO_ELIGIBLE_CHUNKS etc

Required changes:

ensure artifact + operational logging both satisfy explicit outcomes

[TODO] bin/media_watch_daemon.py

Likely issues:

direct writes to media_watch.log, watch_state, /logs, etc

Required changes:

move all operational outputs to logs.all

clarify heartbeat + manifest writer responsibility

[TODO] bin/audit_index.py
[TODO] bin/audit_one.sh

Likely issues:

audit tooling must be immutable and provenance-safe, and must not leak operational logs elsewhere

Required changes:

ensure audit outputs are separate from operational logs, operational logs go under logs.all

[TODO] bin/check_config.py

Likely issues:

prints without explicit structured outcome

Required changes:

explicit outcome record

[TODO] bin/run_batch.py

Likely issues:

orchestrator in python, may be writing logs to old paths

Required changes:

align with logs.all run directories, per-stage meta, explicit outcomes

[TODO] bin/stageF0_contract_check.py
[TODO] bin/stageF1_contract_check.py
[TODO] bin/stageE0_contract_check.py
[TODO] bin/contract_check_global.py
[TODO] bin/run_contracts.py

Likely issues:

contract checks must emit explicit pass/fail details and reason codes, not just stdout text

Required changes:

structured outcome artifact + logs captured under logs.all

[TODO] bin/stageF0_entities.py
[TODO] bin/stageE0_events.py
[TODO] bin/stageB2_motion_flow.py

Likely issues:

may emit empty outputs without explicit reason code records

Required changes:

ensure “empty but valid” vs “skipped/ineligible” is explicit

[TODO] bin/query_audit.py
[TODO] bin/query_moments_asr.py

Likely issues:

tooling scripts often write ad hoc logs or assume interactive use

Required changes:

if they write operational logs at all, it must be under logs.all, otherwise keep purely stdout and let wrapper capture

====================================================================
SECOND-PASS CHECKLIST (COMMONLY MISSED)

Remove all writes to:

/workspace/pipeline/logs

/logs

/workspace/logs

media_watch.log / watch_state

any hard-coded *.log filenames

Ensure every wrapper captures:

stdout and stderr separately (or explicitly combined), with deterministic filenames

meta.json including exit_code, duration, bytes, sha256, cmd, run_id, media_id

Ensure Python scripts do NOT:

create FileHandlers pointing outside logs.all

silently pass on exceptions without emitting explicit failure outcomes

Ensure empty outputs are explicit:

no work to do vs ineligible vs failed are distinct outcomes with enumerated reasons

Confirm Copyparty visibility:

logs.all integrity signals (heartbeat.json, manifest/latest.json) are updated and not served as stubs

Control-plane hardening:

launcher/preflight invariant failures must be hard-fail with explicit reason codes

dynamic repo-root resolution must be used for path guards

no warning-only downgrade on invariant breaches

====================================================================
CURRENT PRIORITY TARGET

Immediate next file to convert: bin/stageA_one.sh
Reason: highest redirect/tee density and broadest blast radius for non-canonical log writes.

====================================================================
END OF AUDIT