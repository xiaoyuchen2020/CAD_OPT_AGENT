"""Bounded diagnostic feedback and progress-aware repeated failure circuit."""
import hashlib
import json
from cad_agent.analysis.efficiency import dumps


def _stable(value):
    """Ignore bookkeeping, never actual geometry, source spans or receipts."""
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, dict):
        return {k: (sorted({_stable_json(i) for i in v}) if k == "operation_receipts" and isinstance(v, list)
                    else _stable(v)) for k, v in sorted(value.items()) if k not in (
            "observation_id", "created_at", "updated_at", "elapsed_seconds",
            "calls", "stalled", "evidence_records", "retrieval", "cached")}
    return value


def _stable_json(value):
    return dumps(_stable(value))


def _call_key(name, args, owners):
    return name + ":" + hashlib.sha256(json.dumps(
        [args, sorted(owners)], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _owners(value):
    return sorted({i for i in value if isinstance(i, str)}) if isinstance(value, list) else []


def _effective_owners(session, name, value):
    from cad_agent.analysis.focus import CONTROL
    owners = _owners(value)
    if not owners and not session.task.complex and name not in CONTROL:
        owners = [session.task.parts[0]["id"]]
    return owners


def progress_signature(session):
    from cad_agent.analysis.focus import CONTROL
    data = session.task.data
    # Control calls and freshly assigned observation IDs are not fact growth.
    # Identical re-reads have one content digest. Genuine new candidates, cursors,
    # unit decisions, scope decisions and operation receipts remain progress.
    observations = sorted({hashlib.sha256(dumps(_stable(o)).encode()).hexdigest()
                           for o in session.observations.values() if o.get("tool") not in CONTROL})
    steps = [{k: s.get(k) for k in ("id", "depends_on", "operations", "delivered",
                                   "operation_receipts", "coverage", "gap_kind", "reason")}
             | {"blocked_status": s.get("status") if s.get("status") not in ("running", "pending") else None}
             for s in data["subtasks"]]
    facts = [session.evidence, observations, steps,
             sorted(getattr(session.policy, "seen_results", set())),
             sorted(getattr(session, "sent_image_ids", set())),
             {k: data.get(k) for k in ("requirement_coverage", "unit_policy",
                                      "object_registry", "object_workflows", "spatial_objects")}]
    return hashlib.sha256(dumps(_stable(facts)).encode()).hexdigest()


def error_context(session):
    return dict(requirement_ids=[p["id"] for p in session.task.parts],
                field_issues=[{k: str(v)[:300] for k, v in issue.items()
                               if k in ("path", "code", "message", "requirement_id")}
                              for issue in session.answer_field_issues[:12]],
                available_tools=sorted(session.focus.names()))


def register_failure(session, name, args, result):
    state = getattr(session, "_tool_failures", None)
    if state is None:
        state = session._tool_failures = {}
    # Contract recurrence does not claim that changed arguments are progress.
    # Target/source identities distinguish independent objects with one error.
    message = str(result["error"].get("message", ""))[:1500]
    targets = {k: v for k, v in (args.items() if isinstance(args, dict) else [])
               if k.endswith(("_id", "_ids")) or k in ("layout_id", "bbox", "seed")}
    issues = result["error"].get("issues", []) or result.get("repair_context", {}).get("field_issues", [])
    contract = [(i.get("path"), i.get("code"), i.get("requirement_id")) for i in issues]
    owners = _effective_owners(session, name, session.trace[-1].get("task_ids", []) if session.trace else [])
    key = name + ":" + hashlib.sha256(dumps([targets, sorted(owners), contract or result["error"].get("detail", message)]).encode()).hexdigest()
    signature = progress_signature(session)
    previous = state.get(key, {})
    count = previous.get("count", 0) + 1 if previous.get("progress") == signature else 1
    state[key] = dict(count=count, progress=signature)
    result["error"]["message"] = message
    result["retry"] = dict(identical_failure_count=count, unchanged_retry_allowed=count < 2,
                           action="correct_arguments_or_read_missing_evidence" if count < 2 else "change_path_or_deliver_known_gaps")
    # Two identical failures get specific repair guidance. A third attempt is
    # blocked *before* execution (see blocked()), not allowed to spin forever.
    session._last_failure_key = key
    raw_key = _call_key(name, args, owners)
    session._failed_calls = getattr(session, "_failed_calls", {})
    old_call = session._failed_calls.get(raw_key, {})
    exact_count = (old_call.get("count", 0) + 1
                   if old_call.get("progress") == signature and old_call.get("contract") == key else 1)
    session._failed_calls[raw_key] = dict(count=exact_count, progress=signature,
                                         task_ids=owners, contract=key)
    session._last_failure_call_key = raw_key


def blocked(session, name, raw_args):
    args = raw_args
    owners = []
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        if not isinstance(args, dict):
            return False
        owners = _effective_owners(session, name, args.pop("task_ids", None))
        # Execution canonicalizes default fields; mirror that for valid schemas.
        from cad_agent.analysis.tool_registry import TOOL_SPECS
        if session.policy.enabled:
            if "limit" in TOOL_SPECS[name][0].model_fields:
                args.setdefault("limit", 12 if name == "analyze_room_batch" else 5)
            args = TOOL_SPECS[name][0].model_validate(args, strict=True).model_dump()
    except (ValueError, TypeError, KeyError):
        pass
    key = _call_key(name, args, owners)
    old = getattr(session, "_failed_calls", {}).get(key)
    stopped = bool(old and old["count"] >= 2 and old["progress"] == progress_signature(session))
    if stopped:
        session._blocked_failure_feedback = old.get("feedback", {})
    return stopped
