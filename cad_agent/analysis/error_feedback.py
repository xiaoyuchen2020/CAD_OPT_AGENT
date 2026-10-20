"""Incremental repair packets with lossless, capability-checked diagnostic readback.

Error metadata is not evidence and never grants coverage or publication credit.
"""
import hashlib
import re
from copy import deepcopy

from pydantic import ValidationError
from cad_agent.analysis.efficiency import dumps


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def field_issues(error, session, name):
    if isinstance(error, ValidationError):
        return [dict(path=".".join(map(str, e["loc"])), code=e["type"], message=e["msg"])
                for e in error.errors(include_url=False, include_context=False, include_input=False)]
    issues = getattr(error, "issues", None)
    if issues is None and name == "finish_analysis":
        issues = getattr(session, "answer_field_issues", [])
    return list(issues or [])


def affected_tasks(session, name, args, issues, task_ids):
    nodes = {s["id"]: s for s in session.task.data["subtasks"]}
    def references(value):
        return {i for i in value if isinstance(i, str)} if isinstance(value, list) else set()
    ids = references(task_ids)
    for issue in issues:
        ids.update(references(issue.get("question_part_ids")))
        if isinstance(issue.get("requirement_id"), str):
            ids.add(issue["requirement_id"])
        match = re.match(r"(steps|claims)\.(\d+)(?:\.|$)", str(issue.get("path", "")))
        if match and isinstance(args, dict):
            rows = args.get(match[1], [])
            if isinstance(rows, list) and int(match[2]) < len(rows):
                row = rows[int(match[2])]
                if isinstance(row, dict):
                    ids.update(references(row.get("question_part_ids")))
                    if isinstance(row.get("requirement_id"), str):
                        ids.add(row["requirement_id"])
    ids &= nodes.keys()
    if not ids:
        ids = {s["id"] for s in nodes.values()
               if not (s["status"] == "completed" and s.get("delivered"))}
    return sorted(ids)


def task_state(session, ids):
    from cad_agent.analysis.scheduling import execution_state
    data = session.task.data
    return [{k: deepcopy(s[k]) for k in
             ("id", "status", "reason", "gap_kind", "depends_on", "delivered") if k in s}
            | execution_state(data, s["id"])
            for s in data["subtasks"] if s["id"] in ids]


def repair_actions(session, name, args, ids, ref):
    """Expose only registered runtime routes and known continuation arguments."""
    from cad_agent.analysis.scheduling import schedule, execution_state
    from cad_agent.analysis.tool_registry import TOOL_SPECS, runtime_supports
    from cad_agent.analysis.focus import CONTROL
    supported = lambda tool: tool in TOOL_SPECS and runtime_supports(
        tool, session.request.runtime, session.w.settings.provider)
    actions = []
    exposed = session.focus.names() if getattr(session, "focused_enabled", False) else set(TOOL_SPECS)
    executable = [i for i in ids if execution_state(session.task.data, i)["executable"]]
    if supported(name) and name in exposed and (name in CONTROL or executable):
        actions.append(dict(tool=name, task_ids=ids if name in CONTROL else executable, action="correct_reported_fields",
                            arguments_required=True))
    for item in schedule(session):
        if item["executable"] and set(item.get("task_ids", [])) & set(ids):
            action = deepcopy(item["next_action"])
            action["action"] = "resolve_pending_requirement"
            if action not in actions:
                actions.append(action)
    actions.append(dict(tool="read_evidence_record",
                        arguments=dict(record_id=ref["record_id"], start=0, length=6000),
                        task_ids=ids, action="read_complete_error_and_state"))
    return actions


def build_feedback(session, name, args, result, error, task_ids):
    """Persist full diagnostics first, then send bounded changed state only."""
    from cad_agent.analysis.evidence_store import save_session
    issues = field_issues(error, session, name)
    ids = affected_tasks(session, name, args, issues, task_ids)
    # Save all evidence, original exception and rejected input for readback.
    # Old archive references are already durable; omitting their growing index
    # prevents the diagnostic record itself from recursively embedding history.
    task = {k: v for k, v in session.task.data.items()
            if k != "evidence_records" and not k.startswith("execution_")}
    full = dict(tool=name, arguments=args, affected_task_ids=ids,
                exception_type=type(error).__name__, exception_message=str(error),
                field_issues=issues, feedback=result, task=task,
                evidence=session.evidence, observations=session.observations)
    ref = save_session(session, "tool_error", full)
    state = task_state(session, ids)
    previous = getattr(session, "_error_feedback_state", {})
    changed = [s for s in state if previous.get(s["id"]) != digest(s)]
    session._error_feedback_state = previous | {s["id"]: digest(s) for s in state}
    related = [s for s in session.task.data["subtasks"] if s["id"] in ids]
    evidence = sorted({eid for s in related for eid in s.get("evidence_ids", [])})
    observations = sorted({oid for s in related for oid in s.get("observation_ids", [])})
    packet = dict(error={k: deepcopy(v) for k, v in result["error"].items()
                         if k not in ("issues", "message")},
                  affected_task_ids=ids, error_record=ref,
                  repair_context=dict(
                      field_issues=[{k: str(v)[:300] for k, v in item.items()
                                     if k in ("path", "code", "message", "requirement_id")}
                                    for item in issues[:12]],
                      field_issue_count=len(issues),
                      state_delta=changed, state_unchanged=not changed,
                      evidence_ids=evidence[:24], evidence_id_count=len(evidence),
                      observation_ids=observations[:12], observation_id_count=len(observations),
                      complete_context_in=ref["record_id"]),
                  next_actions=repair_actions(session, name, args, ids, ref))
    packet["error"]["message"] = str(result["error"].get("message", str(error)))[:600]
    if "issues" in result["error"]:
        # Keep the existing plan validator contract without duplicating its page.
        packet["error"]["issues"] = packet["repair_context"].pop("field_issues")
    if not issues:
        packet["error"]["detail"] = str(error)[:600]
    object_ids = ([args["object_id"]] if isinstance(args, dict) and args.get("object_id") else [])
    objects = session.task.data.get("spatial_objects", {})
    if object_ids:
        packet["repair_context"]["spatial_objects"] = [
            {k: deepcopy(objects[oid].get(k)) for k in ("object_id", "identity", "scope_id", "unit", "coverage")}
            | {"next_read": {"tool": "get_spatial_object", "arguments": {"object_id": oid}}}
            for oid in object_ids if isinstance(oid, str) and oid in objects]
    if "retry" in result:
        packet["retry"] = result["retry"]
    # Supplemental routing can be huge for large plans. The archive is complete.
    if len(dumps(packet)) > 12000:
        packet["next_actions"] = packet["next_actions"][:2] + [packet["next_actions"][-1]]
        packet["repair_context"]["state_delta"] = changed[:8]
        packet["repair_context"]["state_delta_count"] = len(changed)
    return packet
