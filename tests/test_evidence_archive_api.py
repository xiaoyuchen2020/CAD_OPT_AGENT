"""Verify the human readback route preserves task isolation and exact content."""
import json

from fastapi.testclient import TestClient

from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.analysis.evidence_store import save_session
from test_siwc import make_session


def test_task_archive_http_readback_and_isolation(tmp_path, demo_bytes):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        workspace = app.state.workspace
        session = make_session(workspace, demo_bytes)
        value = {"original_text": "完整建筑原文" * 3000}
        ref = save_session(session, "source_original", value)
        url = f"/api/tasks/{session.task.data['task_id']}/evidence-records"
        catalog = client.get(url)
        assert catalog.status_code == 200
        assert catalog.json()["items"][0]["record_id"] == ref["record_id"]
        chunks, start = [], 0
        while True:
            response = client.get(url, params={
                "record_id": ref["record_id"], "start": start, "length": 5000,
            })
            assert response.status_code == 200
            page = response.json()
            chunks.append(page["content"])
            if not page["has_more"]:
                break
            start = page["next_start"]
        assert json.loads("".join(chunks)) == value
        other = make_session(workspace, demo_bytes)
        other.task.save()
        denied = client.get(
            f"/api/tasks/{other.task.data['task_id']}/evidence-records",
            params={"record_id": ref["record_id"]},
        )
        assert denied.status_code == 400
        assert "未在本任务开放" in denied.json()["error"]["message"]
        assert client.get(url, params={"length": 12001}).status_code == 422
