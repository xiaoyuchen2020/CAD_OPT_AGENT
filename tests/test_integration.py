import json
import time
import pytest
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ExtractionRequest, ChatRequest
from cad_agent.adapters.model import ModelError
from conftest import finish, dxf_bytes


def test_demo_end_to_end_with_table_and_evidence(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    assert d["text_count"] == 29 and d["region_count"] == 21
    assert len(d["frames"]) == 1
    job = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))
    assert job["status"] == "partial"
    f = workspace.store.get("fact_set", job["result"]["fact_set_id"])
    assert len(f["practices"]) == 9
    assert sum(p["review_status"] == "needs_review" for p in f["practices"]) == 2
    assert f["manifest"]["policy_applied"] is False
    regions = {r.region_id: r for r in workspace.regions(d["id"])}
    for e in f["evidence"]:
        source = regions[e["region_id"]]
        assert source.text[slice(*e["text_span"])] == e["quote"]
        assert e["entity_ids"] == source.entity_ids
        assert e["bbox"] == source.bbox and e["snapshot_id"] == d["id"]
    table = [p for p in f["practices"] if " | " in p["original_text"]]
    assert len(table) == 2
    table_inner = next(p for p in table if "地上" in p["original_text"])
    assert table_inner["attributes"]["strength_grade"]["value"] == "A3.5"
    assert table_inner["attributes"]["thickness_mm"]["value"] == [200]
    assert (
        next(p for p in table if "地上" in p["original_text"])["attributes"][
            "density_grade"
        ]["value"]
        == "B06"
    )
    q = workspace.answer(ChatRequest(snapshot_id=d["id"], question="列出地上内墙材料"))
    assert len(q["citations"]) == 4
    assert (
        "优化或合规"
        in workspace.answer(ChatRequest(snapshot_id=d["id"], question="是否需要优化"))[
            "answer"
        ]
    )
    assert (
        len(
            workspace.answer(
                ChatRequest(snapshot_id=d["id"], question="哪些信息不完整")
            )["citations"]
        )
        == 2
    )


def test_snapshot_idempotency_and_job_cache(workspace, demo_bytes):
    d = workspace.ingest("one.dxf", demo_bytes)
    assert workspace.ingest("renamed.dxf", demo_bytes)["id"] == d["id"]
    req = ExtractionRequest(snapshot_id=d["id"])
    j = finish(workspace, req)
    cached = workspace.start_extract(req)
    assert cached["id"] == j["id"] and cached["cached"]


def test_nested_blocks_instances_and_layouts_stay_distinct(workspace, tmp_path):
    def build(doc):
        block = doc.blocks.new("note")
        block.add_text("内墙采用MU20混凝土实心砖。", dxfattribs={"height": 2})
        nested = doc.blocks.new("nested")
        nested.add_blockref("note", (5, 5))
        doc.modelspace().add_blockref("nested", (0, 0))
        doc.modelspace().add_blockref("nested", (200, 0))
        doc.layouts.new("Sheet").add_blockref("note", (0, 0))

    d = workspace.ingest("blocks.dxf", dxf_bytes(tmp_path, build))
    regs = workspace.regions(d["id"])
    assert len(regs) == 3
    assert len({r.layout_id for r in regs}) == 2
    assert len({r.entity_ids[0] for r in regs}) == 3
    boxes = [r.bbox for r in regs if r.layout_id == regs[0].layout_id]
    assert abs(boxes[0][0] - boxes[1][0]) == 200
    j = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))
    assert j["result"]["practice_count"] == 3


def test_same_title_frames_do_not_merge(workspace, tmp_path):
    def build(doc):
        m = doc.modelspace()
        for off in (0, 500):
            m.add_lwpolyline(
                [(off, 0), (off + 400, 0), (off + 400, 250), (off, 250)], close=True
            )
            for i, t in enumerate(
                [
                    "建筑设计总说明",
                    "地上内墙采用B06级A3.5蒸压加气混凝土砌块。",
                    "注：材料等级须核对。",
                    "编号 A01",
                ]
            ):
                m.add_text(
                    t, dxfattribs={"insert": (off + 10, 220 - i * 20), "height": 3}
                )

    d = workspace.ingest("frames.dxf", dxf_bytes(tmp_path, build))
    assert len(d["frames"]) == 2
    j = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))
    f = workspace.store.get("fact_set", j["result"]["fact_set_id"])
    assert len(f["practices"]) == 2
    assert len({p["frame_id"] for p in f["practices"]}) == 2
    j2 = finish(
        workspace,
        ExtractionRequest(snapshot_id=d["id"], frame_ids=[d["frames"][0]["frame_id"]]),
    )
    assert j2["result"]["practice_count"] == 1


def test_failure_does_not_overwrite_local_facts(workspace, demo_bytes, monkeypatch):
    d = workspace.ingest("a.dxf", demo_bytes)
    local = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))

    def fail(_):
        raise ModelError("MODEL_SERVICE_BUSY", "暂不可用")

    monkeypatch.setattr(workspace.gateway, "propose", fail)
    model = finish(workspace, ExtractionRequest(snapshot_id=d["id"], mode="model"))
    assert model["status"] == "failed" and model["result"] is None
    assert (
        workspace.store.get("drawing", d["id"])["latest_fact_set_id"]
        == local["result"]["fact_set_id"]
    )
    retry = finish(workspace, ExtractionRequest(snapshot_id=d["id"], mode="model"))
    assert retry["id"] != model["id"]


def test_empty_image_text_is_distinct_from_zero_masonry(workspace, tmp_path):
    def no_text(doc):
        doc.modelspace().add_circle((0, 0), 10)

    d = workspace.ingest("empty.dxf", dxf_bytes(tmp_path, no_text))
    assert any(x["code"] == "NO_NATIVE_TEXT" for x in d["diagnostics"])
    j = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))
    assert j["status"] == "failed"

    def text(doc):
        doc.modelspace().add_text("门窗详见附表")

    d2 = workspace.ingest("other.dxf", dxf_bytes(tmp_path, text))
    j2 = finish(workspace, ExtractionRequest(snapshot_id=d2["id"]))
    assert j2["result"]["practice_count"] == 0


def test_reject_foreign_scope(workspace, demo_bytes):
    d = workspace.ingest("a.dxf", demo_bytes)
    with pytest.raises(ValueError):
        workspace.start_extract(
            ExtractionRequest(snapshot_id=d["id"], frame_ids=["foreign"])
        )


def test_api_upload_query_evidence_security(tmp_path, demo_bytes):
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            api_key="test-private-secret",
            api_base="https://unused",
            model="test",
        )
    )
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/assets/app.js").status_code == 200
        health = c.get("/api/health")
        assert "test-private-secret" not in health.text
        assert c.get("/.env").status_code == 404
        assert (
            c.post(
                "/api/demo", headers={"Origin": "https://external.example"}
            ).status_code
            == 403
        )
        assert (
            c.get("/api/health", headers={"Host": "external.example"}).status_code
            == 400
        )
        assert (
            c.post("/api/drawings", files={"file": ("a.dwg", b"data")}).status_code
            == 400
        )
        assert (
            c.post(
                "/api/drawings", files={"file": ("broken.dxf", b"invalid")}
            ).status_code
            == 400
        )
        d = c.post("/api/drawings", files={"file": ("a.dxf", demo_bytes)}).json()
        j = finish(app.state.workspace, ExtractionRequest(snapshot_id=d["id"]))
        fid = j["result"]["fact_set_id"]
        facts = c.get(f"/api/fact-sets/{fid}?wall_role=outer").json()
        assert facts["total"] == 2
        assert c.get(f"/api/fact-sets/{fid}?offset=-1").status_code == 422
        p = facts["items"][0]
        ev = c.get(f'/api/fact-sets/{fid}/evidence/{p["evidence_ids"][0]}').json()
        assert ev["snapshot_id"] == d["id"]
        native = c.get(f'/api/drawings/{d["id"]}/entities/{ev["entity_ids"][0]}').json()
        assert native["entity_path"] and native["layout_id"] == ev["layout_id"]
        assert c.get(f'/api/drawings/{d["id"]}/entities/foreign').status_code == 404
        assert c.get(f"/api/fact-sets/{fid}/evidence/foreign").status_code == 404
        assert c.get(f'/api/drawings/{d["id"]}/source').content == demo_bytes
        sources = c.get(f'/api/drawings/{d["id"]}/sources?q=B05').json()
        assert sources["total"] == 1
        result = c.post(
            "/api/chat",
            json={
                "snapshot_id": d["id"],
                "question": "列出地上内墙材料",
                "fact_set_id": fid,
            },
        ).json()
        assert len(result["citations"]) == 4
        assert (
            c.post(
                "/api/chat",
                json={
                    "snapshot_id": d["id"],
                    "question": "这条材料",
                    "selected_practice_id": "foreign",
                },
            ).status_code
            == 400
        )
        assert c.get("/api/tools").json()["schema_version"] == "1.0"


def test_fact_set_cannot_cross_snapshot(workspace, demo_bytes, tmp_path):
    d = workspace.ingest("one.dxf", demo_bytes)
    j = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))
    d2 = workspace.ingest(
        "two.dxf",
        dxf_bytes(tmp_path, lambda doc: doc.modelspace().add_text("别的图纸")),
    )
    with pytest.raises(ValueError):
        workspace.answer(
            ChatRequest(
                snapshot_id=d2["id"],
                question="材料",
                fact_set_id=j["result"]["fact_set_id"],
            )
        )


def test_upload_limit(tmp_path):
    with TestClient(
        create_app(Settings(data_dir=tmp_path / "data", max_upload_bytes=8))
    ) as c:
        assert (
            c.post("/api/drawings", files={"file": ("a.dxf", b"a" * 9)}).status_code
            == 413
        )


def test_queries_respect_special_uses_and_multiple_conditions(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    finish(workspace, ExtractionRequest(snapshot_id=d["id"]))

    def ask(q):
        return workspace.answer(ChatRequest(snapshot_id=d["id"], question=q))

    assert len(ask("卫生间材料")["citations"]) == 1
    assert len(ask("B06加气材料")["citations"]) == 2
    assert len(ask("B05或B06材料")["citations"]) == 3
    assert len(ask("厚度为200的做法")["citations"]) == 4
    assert len(ask("120厚的墙体")["citations"]) == 1
    assert "不支持排除" in ask("除了B06以外的材料")["answer"]


def test_multi_insert_array_does_not_drop_instances(workspace, tmp_path):
    def build(doc):
        block = doc.blocks.new("array_note")
        block.add_text("内墙采用MU20混凝土实心砖。", dxfattribs={"height": 2})
        doc.modelspace().add_blockref(
            "array_note", (0, 0), dxfattribs={"column_count": 3, "column_spacing": 200}
        )

    d = workspace.ingest("array.dxf", dxf_bytes(tmp_path, build))
    regions = workspace.regions(d["id"])
    assert len(regions) == 3
    assert len({r.entity_ids[0] for r in regions}) == 3
