#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple


def die(msg: str, rc: int = 2) -> None:
    print("FATAL:", msg, file=sys.stderr)
    sys.exit(rc)


def warn(msg: str) -> None:
    print("WARN:", msg, file=sys.stderr)


def ok(msg: str) -> None:
    print("OK:", msg)


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        die(f"missing events jsonl: {path}")

    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                die(f"invalid json at {path}:{ln}: {type(e).__name__}: {e}")
            if not isinstance(obj, dict):
                die(f"non-object json at {path}:{ln}")
            rows.append(obj)

    if not rows:
        die(f"events jsonl has no records (blank/empty forbidden): {path}")
    return rows


def require_keys(obj: Dict[str, Any], keys: List[str], ctx: str) -> None:
    for k in keys:
        if k not in obj:
            die(f"missing required field {k} in {ctx}")


def is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def check_reasons(v: Any, ctx: str) -> None:
    # In this pipeline, reasons are required and must not be prose-only.
    # We accept either:
    # - dict: { "REASON_CODE": <count/int/bool/any> } (common pattern)
    # - list: ["REASON_CODE", ...] (some artifacts may prefer list)
    if isinstance(v, dict):
        if not v:
            warn(f"{ctx}: reasons dict is empty")
        for k in v.keys():
            if not isinstance(k, str) or not k:
                die(f"{ctx}: invalid reasons key: {repr(k)}")
        return
    if isinstance(v, list):
        if not v:
            warn(f"{ctx}: reasons list is empty")
        for x in v:
            if not isinstance(x, str) or not x:
                die(f"{ctx}: invalid reason entry: {repr(x)}")
        return
    die(f"{ctx}: reasons must be dict or list, got {type(v).__name__}")


def check_events_outcome(o: Dict[str, Any], mid: str) -> None:
    ctx = "events_outcome"
    require_keys(
        o,
        ["record_type", "media_id", "ok", "skipped", "partial", "reasons", "input_identities", "event_count"],
        ctx,
    )

    if o["record_type"] != "events_outcome":
        die(f"{ctx}: record_type mismatch: {o.get('record_type')}")
    if o["media_id"] != mid:
        die(f"{ctx}: media_id mismatch: {o.get('media_id')} != {mid}")

    if not is_bool(o["ok"]) or not is_bool(o["skipped"]) or not is_bool(o["partial"]):
        die(f"{ctx}: ok/skipped/partial must be boolean")

    check_reasons(o["reasons"], ctx)

    # input_identities must exist. Allow dict or list, but not empty string.
    ii = o["input_identities"]
    if not isinstance(ii, (dict, list)):
        die(f"{ctx}: input_identities must be dict or list")

    ec = o["event_count"]
    if not isinstance(ec, int) or ec < 0:
        die(f"{ctx}: event_count must be int >= 0")

    # Basic logical consistency checks (do not overconstrain)
    if o["skipped"] is True and o["ok"] is True:
        die(f"{ctx}: skipped=true is inconsistent with ok=true")
    if o["ok"] is False and o["skipped"] is False and o["partial"] is False:
        warn(f"{ctx}: ok=false but skipped/partial both false, confirm intent")


def check_event_record(r: Dict[str, Any], mid: str) -> None:
    ctx = "event_record"
    require_keys(
        r,
        ["record_type", "media_id", "event_id", "t0_ms", "t1_ms", "event_type", "tags", "flags", "reasons", "evidence", "provenance"],
        ctx,
    )

    if r["record_type"] != "event_record":
        die(f"{ctx}: record_type mismatch: {r.get('record_type')}")
    if r["media_id"] != mid:
        die(f"{ctx}: media_id mismatch: {r.get('media_id')} != {mid}")

    eid = r["event_id"]
    if not isinstance(eid, str) or not eid:
        die(f"{ctx}: event_id must be non-empty string")

    for k in ["t0_ms", "t1_ms"]:
        if not isinstance(r[k], int) or r[k] < 0:
            die(f"{ctx}: {k} must be int >= 0")
    if r["t1_ms"] < r["t0_ms"]:
        die(f"{ctx}: t1_ms < t0_ms for event_id={eid}")

    if not isinstance(r["event_type"], str) or not r["event_type"]:
        die(f"{ctx}: event_type must be non-empty string")

    tags = r["tags"]
    if not isinstance(tags, list):
        die(f"{ctx}: tags must be list")
    for t in tags:
        if not isinstance(t, str) or not t:
            die(f"{ctx}: invalid tag entry: {repr(t)}")

    flags = r["flags"]
    if not isinstance(flags, dict):
        die(f"{ctx}: flags must be dict")
    for fk, fv in flags.items():
        if not isinstance(fk, str) or not fk:
            die(f"{ctx}: invalid flags key: {repr(fk)}")
        if not is_bool(fv):
            die(f"{ctx}: flag {fk} must be boolean")

    check_reasons(r["reasons"], ctx)

    if not isinstance(r["evidence"], (dict, list)):
        die(f"{ctx}: evidence must be dict or list")
    if not isinstance(r["provenance"], dict):
        die(f"{ctx}: provenance must be dict")

    # Encourage evidence richness without making it a hard failure yet.
    if isinstance(r["evidence"], dict) and not r["evidence"]:
        warn(f"{ctx}: evidence dict empty for event_id={eid}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage E0 contract check for /workspace/catalog/events/<media_id>.jsonl")
    ap.add_argument("--mid", required=True, help="media_id to check")
    ap.add_argument("--path", default=None, help="override events jsonl path (absolute)")
    args = ap.parse_args()

    mid = args.mid
    if args.path is not None:
        p = args.path
        if not os.path.isabs(p):
            die("--path must be absolute if provided")
    else:
        p = f"/workspace/catalog/events/{mid}.jsonl"

    rows = load_jsonl(p)

    outcomes = [x for x in rows if x.get("record_type") == "events_outcome"]
    events = [x for x in rows if x.get("record_type") == "event_record"]
    unknown = [x.get("record_type") for x in rows if x.get("record_type") not in ("events_outcome", "event_record")]

    if unknown:
        die(f"unknown record_type values present in {p}: {sorted(set([str(u) for u in unknown]))}")

    if len(outcomes) != 1:
        die(f"expected exactly 1 events_outcome, found {len(outcomes)} in {p}")

    check_events_outcome(outcomes[0], mid)

    for r in events:
        check_event_record(r, mid)

    # Count agreement
    declared = outcomes[0]["event_count"]
    actual = len(events)
    if declared != actual:
        die(f"event_count mismatch in {p}: declared {declared} vs actual {actual}")

    ok(f"stageE0_contract_check passed for {mid} ({actual} event_record, 1 events_outcome)")


if __name__ == "__main__":
    main()