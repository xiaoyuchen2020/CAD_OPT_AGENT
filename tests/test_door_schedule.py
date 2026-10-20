"""Two real DXF sources; task-owned, ambiguity-preserving schedule lineage."""
from copy import deepcopy
import json

import pytest

from cad_agent.analysis.door_schedule import (
    ContextEvidencePair, DoorScheduleRead, ReadDoorScheduleTable, TraceDoorSchedule,
    read_door_schedule_table, read_task_door_schedules, trace_door_schedule,
)
from cad_agent.analysis.evidence_ledger import register_observation
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.spatial_evidence import BindSpatialObject, bind_spatial_object
from cad_agent.contracts import ChatRequest
from cad_agent.semantics.service import SemanticService
from conftest import dxf_bytes


@pytest.fixture
def chain(workspace, tmp_path):
    def plan(doc):
        m = doc.modelspace()
        m.add_lwpolyline([(0, 0), (30, 0), (30, 30), (0, 30)], close=True)
        for text, x, y in [("M101", 5, 15), ("M102", 5, 10), ("M101", 80, 15),
                           ("项目：甲楼", 0, 70), ("楼层：一层", 0, 60), ("版本：A", 0, 50)]:
            m.add_text(text, dxfattribs={"insert": (x, y), "height": 2})
    def schedule(doc):
        m = doc.modelspace()
        for y in (0, 15, 30, 45):
            m.add_line((0, y), (120, y))
        for x in (0, 40, 80, 120):
            m.add_line((x, 0), (x, 45))
        for text, x, y in [("型号", 5, 37), ("宽度(mm)", 45, 37), ("楼层", 85, 37),
                           ("M101", 5, 22), ("900", 45, 22), ("一层", 85, 22),
                           ("M101", 5, 7), ("1000", 45, 7), ("二层", 85, 7),
                           ("项目：甲楼", 0, 90), ("楼层：二层", 0, 75), ("版本：B", 0, 60)]:
            m.add_text(text, dxfattribs={"insert": (x, y), "height": 2})
    a = workspace.ingest("plan.dxf", dxf_bytes(tmp_path, plan))
    b = workspace.ingest("schedule.dxf", dxf_bytes(tmp_path, schedule))
    s = CadAgentSession(workspace, ChatRequest(snapshot_id=a["id"],
        question="查门实例的门窗表型号。", related_drawings=[
            dict(snapshot_id=b["id"], project="同项目", floor="一层", revision="A")]))
    # Source frame is already parser-owned; this fixture selects it explicitly.
    frame = a["frames"][0]
    scope = dict(scope_id="scope-test", snapshot_id=a["id"], source_sha256=a["sha256"],
        frame_id=frame["frame_id"], layout_id=frame["layout_id"], bbox=frame["bbox"],
        name=frame["name"], object_workflow={})
    s.task.data["drawing_scopes"] = {scope["scope_id"]: scope}
    s.task.data["active_scope_by_task"] = {"q1": scope["scope_id"]}
    s.trace.append(dict(task_ids=["q1"]))
    shape = next(e for e in s.index.scene["lines"] if e["kind"] == "LWPOLYLINE")
    labels = [r for r in s.index.regions if r.text in ("M101", "M102") and r.bbox[0] < 30]
    obj = bind_spatial_object(s, BindSpatialObject(scope_id=scope["scope_id"],
        entity_ids=[shape["entity_id"]], label_region_ids=[r.region_id for r in labels if r.text == "M101"], role="door"))
    for label in labels:
        for eid in label.entity_ids:
            s._register_tree(s.index.entity_evidence(eid))
    ann = [e["evidence_id"] for e in s.evidence.values() if e["quote"] == "M101"]
    table = SemanticService(workspace, b["id"]).index.tables[0]
    args = TraceDoorSchedule(object_id=obj["object_id"], annotation_evidence_ids=ann,
        target_snapshot_id=b["id"], table_id=table["table_id"])
    return s, args, a, b


def read_page(s, args, offset=0, limit=2):
    data = read_door_schedule_table(s, ReadDoorScheduleTable(
        snapshot_id=args.target_snapshot_id, table_id=args.table_id,
        row_offset=offset, row_limit=limit, header_rows=[0]))
    data = s._register_tree(data)
    register_observation(s, "read_door_schedule_table", data)
    return data


def test_real_two_drawing_chain_preserves_entities_headers_and_snapshots(chain):
    s, args, a, b = chain
    read_page(s, args)
    result = trace_door_schedule(s, args)
    assert result["state"] == "candidate"
    assert result["candidate_count"] == 1
    assert not result["identity_confirmed"]
    assert result["source"]["snapshot_id"] == a["id"]
    assert result["source"]["entity_ids"] and result["source"]["entity_paths"]
    assert result["annotations"][0]["evidence"]["entity_ids"]
    candidate = result["candidates"][0]
    assert candidate["snapshot_id"] == b["id"]
    assert candidate["row_index"] == 1 and candidate["record_id"] and candidate["edge_id"]
    field = next(f for f in candidate["record"]["fields"] if "code" in f["field_roles"])
    assert field["header_cell_ids"] and field["header_evidence"]
    assert all(e["snapshot_id"] == b["id"] for e in field["header_evidence"])
    assert set(result["context_gaps"]) == {"project", "floor", "revision"}
    assert all(v["state"] == "unconfirmed" for v in result["context"].values())
    assert result["context"]["revision"]["selected_metadata"] == "A"
    assert result["coverage"]["whole_schedule_complete"] is False
    assert any(c["tool"] == "read_door_schedule_table" for c in result["next_calls"])


def test_same_model_multiple_rows_kept_even_when_floor_labels_differ(chain):
    s, args, _, _ = chain
    read_page(s, args, limit=3)
    result = trace_door_schedule(s, args)
    assert result["state"] == "conflict"
    assert result["candidate_count"] == 2
    assert "MULTIPLE_SCHEDULE_CANDIDATES" in result["conflicts"]
    assert len({c["edge_id"] for c in result["candidates"]}) == 2
    assert {c["row_index"] for c in result["candidates"]} == {1, 2}
    assert all(not c["identity_confirmed"] for c in result["candidates"])


def test_unselected_target_unread_table_and_missing_object(chain):
    s, args, _, _ = chain
    with pytest.raises(ValueError, match="SELECTION"):
        read_door_schedule_table(s, ReadDoorScheduleTable(snapshot_id="foreign", table_id=args.table_id))
    with pytest.raises(ValueError, match="SELECTION"):
        trace_door_schedule(s, args.model_copy(update={"target_snapshot_id": "foreign"}))
    result = trace_door_schedule(s, args)
    assert result["state"] == "table_not_read" and not result["candidates"]
    assert result["next_calls"][0]["arguments"]["snapshot_id"] == args.target_snapshot_id
    with pytest.raises(ValueError, match="SPATIAL_OBJECT"):
        trace_door_schedule(s, args.model_copy(update={"object_id": "not-bound"}))


def test_same_model_from_another_instance_is_rejected(chain):
    s, args, _, _ = chain
    far = next(r for r in s.index.regions if r.text == "M101" and r.bbox[0] > 30)
    evidence = s._register_tree(s.index.entity_evidence(far.entity_ids[0]))[0]
    with pytest.raises(ValueError, match="ANNOTATION"):
        trace_door_schedule(s, args.model_copy(update={"annotation_evidence_ids": [evidence["evidence_id"]]}))


def test_explicit_source_floor_and_revision_conflicts_are_not_overridden_by_metadata(chain):
    s, args, a, b = chain
    read_page(s, args)
    pairs = []
    for dimension, prefix in (("project", "项目"), ("floor", "楼层"), ("revision", "版本")):
        ids = []
        for sid in (a["id"], b["id"]):
            index = SemanticService(s.w, sid).index
            region = next(r for r in index.regions if r.text.startswith(prefix + "："))
            evidence = s._register_tree(index.entity_evidence(region.entity_ids[0]))[0]
            ids.append(evidence["evidence_id"])
        pairs.append(ContextEvidencePair(dimension=dimension,
            source_evidence_ids=[ids[0]], target_evidence_ids=[ids[1]]))
    result = trace_door_schedule(s, args.model_copy(update={"context_evidence": pairs}))
    assert result["state"] == "conflict"
    assert result["context"]["project"]["state"] == "matching_source_text"
    assert result["context"]["floor"]["state"] == "conflict"
    assert result["context"]["revision"]["state"] == "conflict"
    assert result["candidate_count"] == 1
    assert not result["candidates"][0]["applicability_confirmed"]


def test_multiple_instance_annotations_not_silently_reduced(chain):
    s, args, _, _ = chain
    read_page(s, args)
    old = s.task.data["spatial_objects"][args.object_id]
    labels = [r.region_id for r in s.index.regions if r.text in ("M101", "M102") and r.bbox[0] < 30]
    bound = bind_spatial_object(s, BindSpatialObject(scope_id=old["scope_id"],
        entity_ids=old["geometry"]["entity_ids"], label_region_ids=labels, role="door"))
    other = next(e["evidence_id"] for e in s.evidence.values() if e["quote"] == "M102")
    result = trace_door_schedule(s, args.model_copy(update={
        "object_id": bound["object_id"]}))
    assert result["models"] == ["M101", "M102"]
    assert "MULTIPLE_INSTANCE_MODELS" in result["conflicts"]
    # An unread bound alternative also remains visible and produces a read call.
    del s.evidence[other]
    pending = trace_door_schedule(s, args.model_copy(update={"object_id": bound["object_id"]}))
    assert "MULTIPLE_INSTANCE_MODELS" in pending["conflicts"]
    assert pending["unread_annotation_candidates"][0]["model"] == "M102"


def test_persistence_readback_and_stable_identifiers(chain):
    s, args, _, _ = chain
    read_page(s, args)
    first = trace_door_schedule(s, args)
    # Saving/reloading JSON does not need a new session and cannot grant credit.
    restored = json.loads(json.dumps(s.task.data))
    read = read_task_door_schedules(restored, DoorScheduleRead(relation_id=first["relation_id"]))
    assert read["items"] == [first]
    read["items"][0]["state"] = "tampered"
    assert restored["door_schedule_relations"][first["relation_id"]]["state"] == "candidate"
    second = trace_door_schedule(s, args)
    assert second["relation_id"] == first["relation_id"]
    assert second["candidates"][0]["edge_id"] == first["candidates"][0]["edge_id"]
    # Subsequent pages update the same requested chain while retaining both rows.
    read_page(s, args, offset=2, limit=1)
    third = trace_door_schedule(s, args)
    assert third["relation_id"] == first["relation_id"] and third["candidate_count"] == 2
    assert first["candidates"][0]["edge_id"] in {c["edge_id"] for c in third["candidates"]}
    assert len(s.task.data["door_schedule_relations"]) == 1


def test_header_role_is_required_and_stale_table_hash_rejected(chain):
    s, args, _, _ = chain
    read_page(s, args)
    observation = next(o for o in s.observations.values() if o["tool"] == "read_door_schedule_table")
    original = deepcopy(observation)
    for record in observation["data"]["records"]:
        for field in record["fields"]:
            field["field_roles"] = ["notes"]
    assert trace_door_schedule(s, args)["state"] == "model_not_found_in_read_rows"
    observation.update(original)
    observation["data"]["source_sha256"] = "wrong"
    with pytest.raises(ValueError, match="STALE"):
        trace_door_schedule(s, args)


def test_registered_tools_cross_snapshot_observation_and_persistent_trace(chain):
    s, args, a, b = chain
    # Use the actual tool wrapper, including budget, ledger and persistence.
    read = json.loads(s.invoke_tool("read_door_schedule_table", dict(
        snapshot_id=b["id"], table_id=args.table_id, row_offset=0, row_limit=2, header_rows=[0])))
    assert "error" not in read, read
    observation = s.observations[read["observation_id"]]
    assert observation["snapshot_id"] == observation["data"]["snapshot_id"] == b["id"]
    trace = json.loads(s.invoke_tool("trace_door_schedule", args.model_dump()))
    assert "error" not in trace, trace
    assert trace["candidate_count"] == 1
    assert trace["source"]["snapshot_id"] == a["id"]
    assert trace["candidates"][0]["snapshot_id"] == b["id"]
    fields = json.loads(s.invoke_tool("query_property_evidence", dict(property_name="width")))
    assert "error" not in fields, fields
    assert fields["items"]
    assert all(f["snapshot_id"] == b["id"] and f["subject_id"] != args.object_id
               for f in fields["items"])
    assert all(f["basis"] == "source_nominal" and f["unit"] == "mm" for f in fields["items"])
    from cad_agent.analysis.tasks import TaskLedger
    task = TaskLedger.read(s.w, s.task.data["task_id"])
    assert read_task_door_schedules(task)["items"][0]["relation_id"] == trace["relation_id"]
    second_page = json.loads(s.invoke_tool("read_door_schedule_table", dict(
        snapshot_id=b["id"], table_id=args.table_id, row_offset=2, row_limit=1, header_rows=[0])))
    assert "error" not in second_page, second_page
    updated = json.loads(s.invoke_tool("trace_door_schedule", args.model_dump()))
    assert "error" not in updated, updated
    assert updated["relation_id"] == trace["relation_id"]
    assert updated["candidate_count"] == 2 and updated["state"] == "conflict"
