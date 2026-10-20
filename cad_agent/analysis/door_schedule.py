"""Task-owned door → source model annotation → selected schedule row candidates.

Exact source identities are verified; model equality is only a lookup key.
Neither explicit user labels nor these chains confirm engineering identity.
"""
from copy import deepcopy
import json
import re
from typing import Literal

from pydantic import Field

from cad_agent.contracts import Record, stable_id
from cad_agent.semantics.contracts import SemanticTable

VERSION = "door-schedule-v1"
TABLE_TOOLS = {"read_table_semantics", "read_door_schedule_table"}
CONTEXT_PATTERNS = {
    "project": r"(?:工程名称|项目名称|项目)\s*[:：]\s*([^\n；;]+)",
    "floor": r"(?:适用楼层|楼层)\s*[:：]\s*([^\n；;]+)",
    "revision": r"(?:版本号|版本|修订号|版次)\s*[:：]\s*([^\n；;]+)",
}


class ReadDoorScheduleTable(SemanticTable):
    snapshot_id: str = Field(min_length=1)


class ContextEvidencePair(Record):
    dimension: Literal["project", "floor", "revision"]
    source_evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    target_evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class TraceDoorSchedule(Record):
    object_id: str = Field(min_length=1)
    annotation_evidence_ids: list[str] = Field(min_length=1, max_length=8)
    target_snapshot_id: str = Field(min_length=1)
    table_id: str = ""
    context_evidence: list[ContextEvidencePair] = Field(default_factory=list, max_length=3)


class DoorScheduleRead(Record):
    relation_id: str = ""
    object_id: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _allowed(session, sid):
    from cad_agent.analysis.project_sources_workflow import selected_ids
    if sid not in selected_ids(session):
        raise ValueError("DOOR_SCHEDULE_SELECTION: 目标图纸未被用户加入本轮关联资料。")


def read_door_schedule_table(session, args):
    """Read the selected immutable source, using the ordinary semantic parser."""
    from cad_agent.semantics.service import SemanticService
    _allowed(session, args.snapshot_id)
    result = SemanticService(session.w, args.snapshot_id).table(args)
    result["source_sha256"] = session.w.store.get("drawing", args.snapshot_id)["sha256"]
    result["next_actions"] = [
        dict(tool="read_door_schedule_table",
             arguments=call["arguments"] | {"snapshot_id": args.snapshot_id})
        for call in result["next_actions"]
    ]
    result["selection_basis"] = "user_selected_snapshot_not_identity_proof"
    return result


def _evidence(session, ids, sid):
    result = []
    for eid in sorted(set(ids)):
        item = session.evidence.get(eid)
        if not item or item.get("snapshot_id") != sid or not item.get("entity_ids"):
            raise ValueError("DOOR_SCHEDULE_EVIDENCE: 须提供指定快照中已读的原文实体证据。")
        result.append(deepcopy(item))
    return result


def _model(text):
    # A complete standalone model or explicitly labelled model field only.
    # Never pick an arbitrary substring from a note, or normalize case/hyphens.
    value = text.strip()
    match = re.fullmatch(r"(?:门型号|门窗型号|型号|门编号)\s*[:：]\s*(.+)", value)
    if match:
        value = match[1].strip()
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,30}", value) and re.search(r"\d", value) else None


def _contexts(session, args, obj):
    pairs = {p.dimension: p for p in args.context_evidence}
    if len(pairs) != len(args.context_evidence):
        raise ValueError("DOOR_SCHEDULE_CONTEXT: 同一上下文维度只能提交一次。")
    metadata = next((d.model_dump() for d in session.request.related_drawings
                     if d.snapshot_id == args.target_snapshot_id), {})
    result = {}
    for dimension, pattern in CONTEXT_PATTERNS.items():
        pair = pairs.get(dimension)
        source = _evidence(session, pair.source_evidence_ids, obj["snapshot_id"]) if pair else []
        target = _evidence(session, pair.target_evidence_ids, args.target_snapshot_id) if pair else []
        def values(evidence):
            return sorted({m.strip() for e in evidence for m in re.findall(pattern, e["quote"])})
        sv, tv = values(source), values(target)
        state = ("conflict" if len(sv) > 1 or len(tv) > 1 or sv and tv and sv != tv else
                 "matching_source_text" if sv and tv else "unconfirmed")
        result[dimension] = dict(state=state, source_values=sv, target_values=tv,
            source_evidence=source, target_evidence=target,
            selected_metadata=metadata.get(dimension, ""),
            metadata_basis="user_labels_not_source_identity",
            applicability_to_instance_and_row="unconfirmed")
    return result


def _annotations(session, obj, ids):
    labels = obj.get("label", [])
    geometry_ids = set(obj["geometry"]["entity_ids"])
    attached_ids = geometry_ids | {eid for label in labels for eid in label["entity_ids"]}
    # A caller cannot hide a second, already-read model on the bound object by
    # supplying only its preferred evidence ID.
    all_ids = set(ids) | {eid for eid, item in session.evidence.items()
        if item.get("snapshot_id") == obj["snapshot_id"]
        and item.get("entity_ids") and set(item["entity_ids"]) <= attached_ids
        and _model(item.get("quote", ""))}
    evidence = _evidence(session, all_ids, obj["snapshot_id"])
    native_texts = {t["entity_id"]: t["text"] for t in session.index.scene["texts"]}
    annotations = []
    for item in evidence:
        if item.get("layout_id") != obj["layout_id"] or item.get("frame_id") != obj["frame_id"]:
            raise ValueError("DOOR_SCHEDULE_ANNOTATION: 型号标注不属于实例的布局和图框。")
        if not set(item["entity_ids"]) <= attached_ids:
            raise ValueError("DOOR_SCHEDULE_ANNOTATION: 型号标注未绑定该实例；不能凭邻近或同名借用。")
        code = _model(item["quote"])
        source_models = {_model(native_texts[eid]) for eid in item["entity_ids"] if eid in native_texts}
        source_models |= {_model(label["text"]) for label in labels
                          if set(item["entity_ids"]) == set(label["entity_ids"])}
        if not code or code not in source_models:
            raise ValueError("DOOR_SCHEDULE_MODEL: 需要完整独立型号或显式型号字段，不接受从正文猜型号。")
        annotations.append(dict(model=code, evidence=item,
            binding_basis="explicit_object_source_selection",
            instance_annotation_state="candidate", identity_confirmed=False))
    return annotations


def _read_rows(session, args):
    observations = dict(session.task.data.get("observations", {})) | session.observations
    rows, pages = {}, []
    for oid, observation in observations.items():
        data = observation.get("data", {})
        if (observation.get("tool") not in TABLE_TOOLS or
                data.get("snapshot_id") != args.target_snapshot_id or
                args.table_id and data.get("table_id") != args.table_id):
            continue
        current_sha = session.w.store.get("drawing", args.target_snapshot_id)["sha256"]
        if data.get("source_sha256", current_sha) != current_sha:
            raise ValueError("DOOR_SCHEDULE_STALE: 已读表格版本已改变，请重新读取。")
        pages.append(dict(observation_id=oid, table_id=data["table_id"],
            row_offset=data.get("row_offset", 0), row_count=data.get("row_count"),
            row_indices=[r["row_index"] for r in data.get("items", [])],
            next_actions=deepcopy(data.get("next_actions", []))))
        for record in data.get("records", []):
            # Preserve distinct readings/header interpretations of the same row.
            signature = _key([data["table_id"], data.get("version"), record])
            if signature in rows:
                rows[signature]["observation_ids"].append(oid)
                continue
            rows[signature] = dict(snapshot_id=data["snapshot_id"],
                source_sha256=current_sha, table_id=data["table_id"], layout_id=data.get("layout_id"),
                table_version=data.get("version"), row_index=record["row_index"],
                record=deepcopy(record), observation_ids=[oid])
    return list(rows.values()), pages


def trace_door_schedule(session, args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input
    _allowed(session, args.target_snapshot_id)
    obj = resolve_object_input(session, args.object_id)
    _allowed(session, obj["snapshot_id"])
    if obj.get("role", {}).get("reference_only"):
        raise ValueError("DOOR_SCHEDULE_REFERENCE: 图例对象不能作为实际门实例。")
    if obj.get("role", {}).get("value") not in ("door", "door_instance", "door_opening", "门"):
        raise ValueError("DOOR_SCHEDULE_ROLE: 需要显式绑定为门候选的原生对象。")
    annotations = _annotations(session, obj, args.annotation_evidence_ids)
    contexts = _contexts(session, args, obj)
    rows, pages = _read_rows(session, args)
    models = sorted({a["model"] for a in annotations})
    unread_annotations = [deepcopy(label) | {"model": _model(label["text"]), "state": "source_read_pending"}
        for label in obj.get("label", []) if _model(label["text"])
        and not any(set(a["evidence"]["entity_ids"]) <= set(label["entity_ids"]) for a in annotations)]
    candidates = []
    for row in rows:
        fields = row["record"].get("fields", [])
        matches = [f for f in fields if "code" in f.get("field_roles", [])
                   and _model(f.get("text", "")) in models]
        if not matches:
            continue
        # Keep header and row evidence together; a model in an unrelated column
        # is not a schedule-key match. Raw fields remain available for review.
        for field in matches:
            if not field.get("header_evidence") or not field.get("evidence"):
                continue
            for evidence in field["header_evidence"] + field["evidence"]:
                if evidence.get("snapshot_id") != args.target_snapshot_id:
                    raise ValueError("DOOR_SCHEDULE_ROW: 表格字段来源快照不一致。")
            candidate = deepcopy(row)
            candidate.update(model=_model(field["text"]), model_cell_id=field["cell_id"],
                state="candidate", relation_basis="explicit_model_header_exact_text",
                identity_confirmed=False, applicability_confirmed=False)
            candidate["record_id"] = stable_id("schedule_row", VERSION,
                args.target_snapshot_id, row["table_id"], str(row["row_index"]),
                _key(row["record"]))
            candidate["edge_id"] = stable_id("door_schedule_edge", VERSION, obj["object_id"],
                candidate["record_id"], field["cell_id"], _key(annotations))
            candidates.append(candidate)
    conflicts = []
    if len(set(models) | {label["model"] for label in unread_annotations}) > 1:
        conflicts.append("MULTIPLE_INSTANCE_MODELS")
    if len(candidates) > 1:
        conflicts.append("MULTIPLE_SCHEDULE_CANDIDATES")
    conflicts.extend(f"{k.upper()}_SOURCE_CONFLICT" for k, v in contexts.items() if v["state"] == "conflict")
    state = ("conflict" if conflicts else "candidate" if candidates else
             "table_not_read" if not pages else "model_not_found_in_read_rows")
    next_calls = [call for page in pages for call in page["next_actions"]]
    next_calls.extend(dict(tool="read_project_sources", arguments=dict(action="read",
        snapshot_id=obj["snapshot_id"], region_id=label["region_id"])) for label in unread_annotations)
    if not pages:
        next_calls.append(dict(tool="read_door_schedule_table",
            arguments=dict(snapshot_id=args.target_snapshot_id, table_id=args.table_id))
            if args.table_id else dict(tool="read_project_sources",
            arguments=dict(action="search", snapshot_id=args.target_snapshot_id, query="门窗表")))
    context_gaps = [k for k, v in contexts.items() if v["state"] != "matching_source_text"]
    for dimension in context_gaps:
        for sid in (obj["snapshot_id"], args.target_snapshot_id):
            next_calls.append(dict(tool="read_project_sources", arguments=dict(action="search",
                snapshot_id=sid, query={"project": "项目", "floor": "楼层", "revision": "版本"}[dimension])))
    source_identity = {k: obj[k] for k in ("object_id", "snapshot_id", "source_sha256", "layout_id", "frame_id", "scope_id")}
    source_identity["entity_ids"] = deepcopy(obj["geometry"]["entity_ids"])
    source_identity["role"] = deepcopy(obj["role"])
    source_identity["entity_paths"] = [
        dict(entity_id=r["entity_id"], entity_path=r.get("entity_path", []))
        for r in obj["geometry"].get("records", [])]
    relation_id = stable_id("door_schedule", VERSION, _key(source_identity), _key(annotations),
        args.target_snapshot_id, args.table_id, _key(contexts))
    result = dict(version=VERSION, relation_id=relation_id, state=state,
        snapshot_id=obj["snapshot_id"], source=source_identity, annotations=annotations,
        target=dict(snapshot_id=args.target_snapshot_id,
            source_sha256=session.w.store.get("drawing", args.target_snapshot_id)["sha256"],
            table_id=args.table_id, selection_state="selected"),
        models=models, candidates=sorted(candidates, key=lambda c: c["edge_id"]),
        unread_annotation_candidates=unread_annotations,
        candidate_count=len(candidates), context=contexts, conflicts=conflicts,
        identity_confirmed=False, context_gaps=context_gaps,
        coverage=dict(read_pages=pages, whole_schedule_complete=False,
            whole_drawing_complete=False, instance_census_complete=False),
        next_calls=list({_key(c): c for c in next_calls}.values()),
        operation_receipts=[dict(operation="door_schedule", fulfilled=False, basis="candidate_source_chain")],
        note="来源链可定位实例、标注及表行；同型号仅形成候选。项目、楼层、版本及实际适用关系未确认，不能静默选行或借值。")
    session.task.data.setdefault("door_schedule_relations", {})[relation_id] = deepcopy(result)
    return result


def read_task_door_schedules(task_data, args=None):
    """Pure read for HTTP/task reload; no session, writes, or evidence credit."""
    args = args or DoorScheduleRead()
    rows = list(task_data.get("door_schedule_relations", {}).values())
    if args.relation_id:
        rows = [r for r in rows if r["relation_id"] == args.relation_id]
        if not rows:
            raise ValueError("DOOR_SCHEDULE_NOT_FOUND: 本任务没有该关系记录。")
    if args.object_id:
        rows = [r for r in rows if r["source"]["object_id"] == args.object_id]
    rows.sort(key=lambda r: r["relation_id"])
    end = args.offset + args.limit
    return dict(items=deepcopy(rows[args.offset:end]), total=len(rows), offset=args.offset,
        has_more=end < len(rows), next_offset=end if end < len(rows) else None,
        note="持久候选关系回读；不新增原文读取信用或确认实例身份。")
