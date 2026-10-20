"""Public readback and model tool share the same task-scoped property view."""
import json
import pytest
from fastapi.testclient import TestClient

from cad_agent.app import create_app
from cad_agent.config import Settings
from test_siwc import make_session
from test_door_schedule import chain


def test_property_tool_and_http_readback_are_task_scoped(tmp_path, demo_bytes):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        workspace = client.app.state.workspace
        session = make_session(workspace, demo_bytes)
        sid = session.request.snapshot_id
        session.register_observation("measure_geometry", {
            "snapshot_id": sid, "measurement_id": "measurement-fixture",
            "object_id": "door-fixture", "status": "measured",
            "unit": "m", "basis": "source_opening",
            "measurements": {"width_m": 0.92},
        })
        session.task.data["observations"] = dict(session.observations)
        session.task.save()
        result = json.loads(session.invoke_tool("query_property_evidence", {
            "subject_id": "door-fixture", "property_name": "width",
        }))
        assert "error" not in result
        assert result["items"][0]["basis"] == "measurement"
        assert result["items"][0]["value"] == 0.92
        url = f"/api/tasks/{session.task.data['task_id']}/property-evidence"
        page = client.get(url, params={"subject_id": "door-fixture"})
        assert page.status_code == 200
        assert page.json()["items"][0]["field_id"] == result["items"][0]["field_id"]
        assert client.get(url, params={"offset": -1}).status_code == 422
        assert client.get(url, params={"limit": 101}).status_code == 422
        other = make_session(workspace, demo_bytes)
        other.task.save()
        isolated = client.get(f"/api/tasks/{other.task.data['task_id']}/property-evidence")
        assert isolated.json()["items"] == []
        assert client.get(f"/api/tasks/{other.task.data['task_id']}/gap-recovery").json() == []


def test_property_view_rejects_observations_from_unselected_snapshot(tmp_path, demo_bytes):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        workspace = client.app.state.workspace
        session = make_session(workspace, demo_bytes)
        session.task.data["observations"] = {
            "foreign": {"observation_id": "foreign", "snapshot_id": "unselected",
                        "tool": "measure_geometry", "data": {
                            "object_id": "door", "status": "measured", "area_m2": 10,
                        }},
        }
        session.task.save()
        page = client.get(f"/api/tasks/{session.task.data['task_id']}/property-evidence")
        assert page.status_code == 200
        assert page.json()["items"] == []
        assert page.json()["diagnostics"][0]["code"] == "PROPERTY_SNAPSHOT_MISMATCH"


def test_property_query_refreshes_after_new_observation(workspace, demo_bytes):
    session = make_session(workspace, demo_bytes)
    assert json.loads(session.invoke_tool("query_property_evidence", {}))["items"] == []
    session.register_observation("measure_geometry", {
        "snapshot_id": session.request.snapshot_id, "measurement_id": "fresh",
        "object_id": "door-fresh", "status": "measured", "unit": "m",
        "basis": "source_opening", "measurements": {"width_m": 0.92},
    })
    result = json.loads(session.invoke_tool("query_property_evidence", {}))
    assert result["items"][0]["value"] == 0.92
    assert "already_returned" not in result


@pytest.mark.parametrize("tool", ["read_door_schedule_table", "trace_door_schedule", "search_similar_objects"])
def test_candidate_tool_observations_cannot_be_published_as_confirmed_facts(workspace, demo_bytes, tool):
    session = make_session(workspace, demo_bytes)
    result = {"snapshot_id": session.request.snapshot_id, "candidates": [],
              "identity_confirmed": False}
    session.register_observation(tool, result)
    claim = dict(text="当前来源中仅登记候选关系，适用性仍待核对。", state="observed",
                 evidence_basis="tool_observation", observation_kind="geometry_observation",
                 observation_ids=[result["observation_id"]])
    with pytest.raises(ValueError, match="候选关系"):
        session.validate_answer(json.dumps({"claims": [claim]}))
    claim["state"] = "derived"
    assert session.validate_answer(json.dumps({"claims": [claim]}))["claims"][0]["state"] == "derived"


def test_similarity_http_and_agent_use_same_native_instances(tmp_path):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from test_similarity_search import source
    path, _, _ = source(tmp_path)
    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as client:
        workspace = client.app.state.workspace
        drawing = workspace.ingest("similar.dxf", path.read_bytes())
        layout = next(l["layout_id"] for l in drawing["layouts"] if l["name"] == "Model")
        args = dict(layout_id=layout, sample_bbox=[-1.0, -1.0, 11.0, 11.0], limit=12)
        url = f"/api/drawings/{drawing['id']}/similarity-search"
        response = client.post(url, json=args)
        assert response.status_code == 200
        assert len(response.json()["candidates"]) == 2
        session = CadAgentSession(workspace, ChatRequest(
            snapshot_id=drawing["id"], question="查找形状相似候选"))
        packet = json.loads(session.invoke_tool("search_similar_objects", args))
        assert "error" not in packet
        assert {r["object_id"] for r in packet["candidates"]} == {
            r["object_id"] for r in response.json()["candidates"]}
        assert session.observations[packet["observation_id"]]["snapshot_id"] == drawing["id"]
        assert not packet["coverage"]["full_drawing_enumeration"]
        assert client.post(url, json=args | {"layout_id": "foreign"}).status_code == 400
        assert client.post(url, json=args | {"sample_bbox": [1, 1, 0, 0]}).status_code == 422


def test_door_relation_http_readback_retains_candidates_and_is_task_scoped(chain, tmp_path):
    from cad_agent.analysis.door_schedule import read_task_door_schedules
    from test_door_schedule import read_page
    session, args, _, _ = chain
    read_page(session, args, limit=3)
    result = json.loads(session.invoke_tool("trace_door_schedule", args.model_dump()))
    assert result["candidate_count"] == 2
    session.task.save()
    # Open the same persisted Store, exercising reload through the HTTP layer.
    with TestClient(create_app(session.w.settings)) as client:
        url = f"/api/tasks/{session.task.data['task_id']}/door-schedule-relations"
        page = client.get(url)
        assert page.status_code == 200
        assert page.json() == read_task_door_schedules(session.task.data)
        assert page.json()["items"][0]["candidate_count"] == 2
        assert client.get(url, params={"object_id": "another-object"}).json()["items"] == []
        assert client.get(url, params={"limit": 101}).status_code == 422
        public = client.get(f"/api/tasks/{session.task.data['task_id']}").json()
        assert public["door_schedule_relation_count"] == 1
        assert "door_schedule_relations" not in public
