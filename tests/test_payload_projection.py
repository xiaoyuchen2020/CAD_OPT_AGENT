"""Real dispatch, archive capability boundaries, and field-level read accounting."""
from copy import deepcopy
import json
import pytest

from cad_agent.analysis.evidence_store import EvidenceRecordRead, read
from cad_agent.analysis.payload_projection import project_result, selected_view
from cad_agent.analysis.session import CadAgentSession
from cad_agent.components.service import ComponentService
from cad_agent.contracts import ChatRequest


@pytest.fixture
def source(workspace, demo_bytes):
    drawing = workspace.ingest("projection.dxf", demo_bytes)
    record = dict(
        record_id="component_projection", snapshot_id=drawing["id"],
        source_sha256=drawing["sha256"], kind="relations",
        layout_id=drawing["layouts"][0]["layout_id"], unit="unknown",
        unit_status="unresolved", status="needs_review",
        scope_bbox=[0, 0, 1000, 1000], coordinate_space="dxf_wcs",
        coverage={"complete": False, "missing": ["unread_next_page"]},
        diagnostics=["unknown_units"], rooms=[], walls=[], relations=[],
        openings=[
            dict(
                object_id=f"opening_{i}", opening_type="unknown",
                identity_status="candidate", engineering_confirmed=False,
                entity_ids=[f"entity_{i}"], source_region_ids=[f"region_{i}"],
                bbox=[0, 0, 1000, 1000], missing=["identity"],
                boundary=[[j + 0.125, j / 7] for j in range(100)],
                source_evidence={"basis": "native_geometry_only"},
            )
            for i in range(4)
        ],
    )
    workspace.store.put("component", record["record_id"], record)
    return drawing, record


def session(workspace, drawing):
    return CadAgentSession(workspace, ChatRequest(
        snapshot_id=drawing["id"], question="读取门洞候选", mode="agent",
    ))


def arguments(record, **extra):
    return dict(record_id=record["record_id"], view="openings", offset=0, limit=2, **extra)


def restore(s, ref):
    chunks, start = [], 0
    while True:
        page = read(s, EvidenceRecordRead(record_id=ref["record_id"], start=start, length=511))
        chunks.append(page["content"])
        if not page["has_more"]:
            break
        start = page["next_start"]
    return json.loads("".join(chunks))


def test_actual_dispatch_projects_and_archives_exact_tool_page(workspace, source):
    drawing, record = source
    s = session(workspace, drawing)
    raw = ComponentService.page(record, "openings", 0, 2, page_call_budget=11)
    out = json.loads(s.invoke_tool("get_component_evidence", arguments(record)))
    assert "error" not in out
    assert out["payload_projection"]["effective_view"] == "summary"
    assert out["payload_projection"]["omitted_fields_read"] is False
    assert out["payload_projection"]["candidate_rows_omitted"] == 0
    for key in (
        "snapshot_id", "source_sha256", "unit", "unit_status", "status",
        "scope_bbox", "coverage", "diagnostics", "offset", "total",
        "has_more", "next_offset", "requested_limit", "effective_limit", "pagination",
    ):
        assert out[key] == raw[key]
    for actual, original in zip(out["items"], raw["items"]):
        assert actual == {k: v for k, v in original.items() if k != "boundary"}
    archived = restore(s, out["evidence_record"])
    # Page budget depends on the session configuration, but all original data
    # and pagination survive the archive exactly.
    assert archived["items"] == raw["items"]
    assert archived["pagination"] == raw["pagination"]
    observation = s.observations[out["observation_id"]]["data"]
    assert all("boundary" not in item for item in observation["items"])
    assert len(observation["items"]) == 2 and observation["has_more"]
    assert not s.evidence
    # Public readback retrieves data without granting observation or source
    # credit for coordinates hidden by the preceding summary.
    observations = deepcopy(s.observations)
    page = json.loads(s.invoke_tool("read_evidence_record", {
        "record_id": out["evidence_record"]["record_id"], "length": 511,
    }))
    assert page["content"] == workspace.store.get("evidence_archive", page["record_id"])["text"][:511]
    assert s.observations == observations and not s.evidence


def test_full_view_is_independent_of_summary_cache(workspace, source):
    drawing, record = source
    s = session(workspace, drawing)
    small = json.loads(s.invoke_tool("get_component_evidence", arguments(record, result_view="summary")))
    full = json.loads(s.invoke_tool("get_component_evidence", arguments(record, result_view="full")))
    assert "payload_projection" in small
    assert not full.get("already_returned") and "payload_projection" not in full
    assert full["items"] == record["openings"][:2]
    assert len(json.dumps(small)) < len(json.dumps(full))


def test_archive_rejects_other_task_snapshot_and_changed_source(workspace, source):
    drawing, record = source
    s = session(workspace, drawing)
    out = json.loads(s.invoke_tool("get_component_evidence", arguments(record)))
    args = EvidenceRecordRead(record_id=out["evidence_record"]["record_id"])
    foreign = session(workspace, drawing)
    rejected = json.loads(foreign.invoke_tool("read_evidence_record", {"record_id": args.record_id}))
    assert rejected["error"]["code"] == "INVALID_TOOL_INPUT"
    assert not foreign.observations and not foreign.evidence
    s.allowed_snapshots = set()
    with pytest.raises(ValueError, match="选定图纸"):
        read(s, args)
    s.allowed_snapshots = {drawing["id"]}
    workspace.store.put("drawing", drawing["id"], drawing | {"sha256": "changed"})
    with pytest.raises(ValueError, match="版本"):
        read(s, args)


def test_task_operations_and_direct_geometry_reads_keep_detail(workspace, source):
    drawing, _ = source
    s = session(workspace, drawing)
    s.task.auto_plan()
    owner = s.task.data["subtasks"][0]
    owner["operations"] = ["contours"]
    assert selected_view(s, "get_component_evidence", [owner["id"]]) == "full"
    owner["operations"] = ["relations"]
    assert selected_view(s, "get_component_evidence", [owner["id"]]) == "summary"
    assert selected_view(s, "read_geometry", [owner["id"]]) == "full"
    assert selected_view(s, "get_component_evidence", ["not_an_owner"]) == "full"


def test_critical_nested_fields_and_executable_calls_are_never_projected(workspace, source):
    drawing, _ = source
    s = session(workspace, drawing)
    coords = [[j, j / 3] for j in range(500)]
    original = dict(
        items=[dict(object_id="one", boundary=coords, holes=[], status="candidate",
                    geometry={"status": "unknown", "missing": ["closed_boundary"]})],
        evidence={"evidence_id": "e", "quote": "保留原文与来源", "boundary": coords},
        next_calls=[{"tool": "some_tool", "arguments": {"boundary": coords}}],
        geometry_complete=False, scope={"bbox": [0, 0, 100, 100]},
    )
    before = deepcopy(original)
    out = project_result(s, "get_component_evidence", {}, original, view="summary")
    assert original == before
    assert out["evidence"] == original["evidence"]
    assert out["next_calls"] == original["next_calls"]
    assert out["items"][0]["geometry"] == original["items"][0]["geometry"]
    assert out["items"][0]["holes"] == []
    assert out["payload_projection"]["omitted_fields"] == [
        dict(path="/items/*/boundary", occurrences=1, coordinate_points=500,
             chars=len(json.dumps(coords, separators=(",", ":"))))
    ]
    assert restore(s, out["evidence_record"]) == original
    assert not s.observations and not s.evidence


def test_small_unknown_and_invalid_views_stay_conservative(workspace, source):
    drawing, record = source
    s = session(workspace, drawing)
    small = {"boundary": [[i, i] for i in range(8)], "unit": "unknown"}
    assert project_result(s, "get_component_evidence", {}, small, view="summary") is small
    assert project_result(s, "future_tool", {}, {"boundary": [[i, i] for i in range(999)]}, view="summary").keys() == {"boundary"}
    out = json.loads(s.invoke_tool("get_component_evidence", arguments(record, result_view="bad")))
    assert out["error"]["code"] == "INVALID_TOOL_INPUT"
    assert not s.observations and not s.evidence
