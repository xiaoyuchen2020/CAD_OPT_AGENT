from dataclasses import replace
import json
import numpy as np
import pytest
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis import retrieval
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.adapters.embedding import EmbeddingUnavailable
from cad_agent.app import create_app
from cad_agent.config import Settings
from fastapi.testclient import TestClient
from conftest import dxf_bytes


class FakeEmbedding:
    identity = "test-only-semantic-vectors"

    def __init__(self):
        self.calls = []

    def encode(self, texts, query=False):
        self.calls.append((len(texts), query))
        rows = []
        for t in texts:
            v = np.array(
                [
                    1.0 if any(s in t for s in ["岩棉", "隔热"]) else 0.0,
                    1.0 if "防水" in t else 0.0,
                    0.01,
                ]
            )
            rows.append(v / np.linalg.norm(v))
        return np.asarray(rows), {
            "input_tokens": sum(map(len, texts)),
            "seconds": 0.01,
            "cache_hit": False,
        }


@pytest.fixture
def drawing(workspace, tmp_path):
    def build(doc):
        for i, t in enumerate(
            ["保温材料：岩棉", "防水等级：一级", "M102宽度900", "M103宽度1000"]
        ):
            doc.modelspace().add_text(
                t, dxfattribs={"insert": (i * 500, 0), "height": 2}
            )
        doc.layouts.new("另一布局").add_text(
            "保温材料：聚苯板", dxfattribs={"height": 2}
        )

    return workspace.ingest("检索.dxf", dxf_bytes(tmp_path, build))


@pytest.fixture
def fake(monkeypatch):
    model = FakeEmbedding()
    monkeypatch.setattr(retrieval, "get_embedder", lambda _: model)
    return model


def test_hybrid_source_pointer_and_scope(workspace, drawing, fake):
    idx = DrawingIndex(workspace, drawing["id"])
    target = next(r for r in idx.regions if "岩棉" in r.text)
    result = idx.search("隔热", mode="hybrid", layout_id=target.layout_id, limit=1)
    hit = result["items"][0]
    assert hit["region_id"] == target.region_id
    assert target.text[slice(*hit["text_span"])] == hit["snippet"]
    assert "evidence_id" not in hit
    assert result["retrieval"]["effective_mode"] == "hybrid"
    assert all(
        idx.by_id[x["region_id"]].layout_id == target.layout_id for x in result["items"]
    )
    with pytest.raises(ValueError):
        idx.search("隔热", mode="hybrid", frame_id="foreign")


def test_cache_pagination_all_and_identity(workspace, drawing, fake):
    idx = DrawingIndex(workspace, drawing["id"])
    first = idx.search("隔热", mode="hybrid", limit=1)
    second = idx.search("隔热", mode="hybrid", offset=1, limit=1)
    assert second["retrieval"]["indexing"]["cache_hit"]
    assert first["items"][0]["region_id"] != second["items"][0]["region_id"]
    assert len([x for x in fake.calls if not x[1]]) == 1
    empty = idx.search("隔热", keywords=["防水"], match="all", mode="hybrid")
    assert empty["total"] == 0
    fake.identity = "test-only-changed-model"
    assert not idx.search("隔热", mode="hybrid")["retrieval"]["indexing"]["cache_hit"]


def test_snapshot_cache_and_corruption(workspace, drawing, fake):
    idx = DrawingIndex(workspace, drawing["id"])
    idx.search("隔热", mode="hybrid")
    cache = next((workspace.store.root / "retrieval").glob("*.npz"))
    cache.write_bytes(b"corrupt")
    assert not idx.search("隔热", mode="hybrid")["retrieval"]["indexing"]["cache_hit"]
    old = idx.snapshot
    idx.snapshot = old + "new"
    assert not idx.search("隔热", mode="hybrid")["retrieval"]["indexing"]["cache_hit"]


def test_fallback_preserves_keyword_behavior(workspace, drawing, monkeypatch):
    idx = DrawingIndex(workspace, drawing["id"])

    def unavailable(_):
        raise EmbeddingUnavailable("not prepared")

    monkeypatch.setattr(retrieval, "get_embedder", unavailable)
    original = idx.search("防水")
    fallback = idx.search("防水", mode="hybrid")
    assert {k: v for k, v in fallback.items() if k != "retrieval"} == original
    assert fallback["retrieval"]["effective_mode"] == "keyword"
    assert fallback["retrieval"]["fallback_reason"] == "embedding_unavailable"
    assert (
        idx.search("防水", mode="lexical")["retrieval"]["effective_mode"] == "lexical"
    )


def test_exact_identifier_and_empty_query(workspace, drawing, fake):
    idx = DrawingIndex(workspace, drawing["id"])
    assert "M102" in idx.search("M102", mode="hybrid")["items"][0]["snippet"]
    assert idx.search("", mode="hybrid")["total"] == idx.search("")["total"]


def test_session_does_not_register_search_as_evidence(workspace, drawing, fake):
    s = CadAgentSession(
        workspace,
        ChatRequest(
            snapshot_id=drawing["id"],
            question="隔热",
            mode="agent",
            retrieval_mode="hybrid",
        ),
    )
    result = json.loads(s.invoke_tool("search_sources", {"query": "隔热", "limit": 1}))
    assert not s.evidence
    hit = result["items"][0]
    s.execute(
        "read_source_context",
        {"region_id": hit["region_id"], "start": hit["snippet_start"], "neighbors": 0},
    )
    assert s.evidence
    assert s.trace[0]["result_summary"]["retrieval"]["effective_mode"] == "hybrid"
    assert s.retrieval_usage
    assert (
        ChatRequest(snapshot_id=drawing["id"], question="q").retrieval_mode == "keyword"
    )


def test_api_separate_from_legacy_sources(tmp_path, fake):
    app = create_app(Settings(data_dir=tmp_path / "api"))
    with TestClient(app) as c:
        d = c.post("/api/demo").json()
        # API/demo response shape belongs to the existing endpoint.
        snapshot = d["drawing"]["snapshot_id"]
        r = c.get(
            f"/api/drawings/{snapshot}/retrieval",
            params={"q": "内墙", "mode": "hybrid"},
        )
        assert r.status_code == 200
        assert r.json()["retrieval"]["effective_mode"] == "hybrid"
        assert (
            c.get(
                f"/api/drawings/{snapshot}/retrieval", params={"mode": "bad"}
            ).status_code
            == 422
        )


def test_long_region_chunks_keep_tail_and_exact_offsets(workspace, drawing):
    idx = DrawingIndex(workspace, drawing["id"])
    target = next(r for r in idx.regions if "岩棉" in r.text)
    target.text = "普通说明。" * 180 + "尾部编号M999，保温材料：岩棉。"
    rows = retrieval.chunks(idx)
    own = [c for c in rows if c["region_id"] == target.region_id]
    assert own[-1]["end"] == len(target.text)
    assert all(target.text[c["start"] : c["end"]] == c["text"] for c in own)
    result = idx.search("M999", mode="lexical", limit=1)["items"][0]
    assert result["region_id"] == target.region_id
    assert "M999" in result["snippet"] and result["snippet_start"] > 0


def test_all_requires_literal_matches_and_filtered_cache(workspace, drawing, fake):
    idx = DrawingIndex(workspace, drawing["id"])
    all_scope = idx.search("保温", mode="hybrid")
    target = next(r for r in idx.regions if "岩棉" in r.text)
    scoped = idx.search("保温", mode="hybrid", layout_id=target.layout_id)
    assert (
        scoped["retrieval"]["indexing"]["index_id"]
        != all_scope["retrieval"]["indexing"]["index_id"]
    )
    result = idx.search("保温", keywords=["岩棉"], match="all", mode="hybrid")
    assert result["total"] == 1
    assert result["items"][0]["region_id"] == target.region_id


def test_missing_model_falls_back_without_optional_dependencies(
    workspace, drawing, tmp_path
):
    workspace.settings = replace(
        workspace.settings, embedding_model_dir=tmp_path / "missing-model"
    )
    idx = DrawingIndex(workspace, drawing["id"])
    result = idx.search("防水", mode="hybrid")
    assert result["retrieval"]["fallback_reason"] == "embedding_unavailable"
    assert result["items"] == idx.search("防水")["items"]
