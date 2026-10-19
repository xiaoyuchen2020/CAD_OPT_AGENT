import json
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.general import DrawingIndex
from cad_agent.contracts import ChatRequest
from conftest import dxf_bytes


@pytest.fixture
def general_drawing(workspace, tmp_path):
    def build(doc):
        m = doc.modelspace()
        # Generic (non-masonry) title and an explicit bounded three-column table.
        m.add_text("门窗表", dxfattribs={"insert": (0, 65), "height": 2})
        for y in (0, 15, 30, 45):
            m.add_line((0, y), (120, y), dxfattribs={"layer": "TABLE"})
        for x in (0, 40, 80, 120):
            m.add_line((x, 0), (x, 45), dxfattribs={"layer": "TABLE"})
        for y, values in [
            (38, ["编号", "宽度(mm)", "高度(mm)"]),
            (23, ["M101", "900", "2100"]),
            (8, ["M102", "", "2400"]),
        ]:
            for x, value in zip((5, 45, 85), values):
                if value:
                    m.add_text(
                        value,
                        dxfattribs={"insert": (x, y), "height": 2, "layer": "SCHEDULE"},
                    )
        # Separate colon values plus a string that must never become active SVG.
        m.add_text("防水等级：一级", dxfattribs={"insert": (180, 50), "height": 2})
        m.add_text("保温材料：岩棉", dxfattribs={"insert": (180, 30), "height": 2})
        m.add_text(
            "<script>alert(1)</script>", dxfattribs={"insert": (180, 10), "height": 2}
        )
        b = doc.blocks.new("DOOR")
        b.add_line((0, 0), (10, 0), dxfattribs={"layer": "DOOR"})
        m.add_blockref("DOOR", (250, 80))

    return workspace.ingest("门窗和装修.dxf", dxf_bytes(tmp_path, build))


def session(w, d):
    return CadAgentSession(
        w, ChatRequest(snapshot_id=d["id"], question="门窗信息", mode="agent")
    )


def test_general_search_any_all_and_paging(workspace, general_drawing):
    s = session(workspace, general_drawing)
    any_result = s.execute("search_sources", {"keywords": ["防水", "保温"], "limit": 1})
    assert any_result["total"] == 2 and any_result["has_more"]
    assert any_result["items"][0]["matched_terms"]
    assert (
        s.execute("search_sources", {"keywords": ["防水", "保温"], "match": "all"})[
            "total"
        ]
        == 0
    )
    with pytest.raises(ValueError):
        s.execute("search_sources", {"section_id": "foreign"})


def test_generic_table_preserves_blank_cell_and_citation(workspace, general_drawing):
    s = session(workspace, general_drawing)
    table = s.execute("get_drawing_overview", {"view": "tables"})["items"][0]
    result = s.execute(
        "read_table", {"table_id": table["table_id"], "row_offset": 1, "row_limit": 2}
    )
    assert result["header_state"] == "unconfirmed"
    row = result["items"][0]
    assert [c["text"] for c in row["cells"]] == ["M101", "900", "2100"]
    blank = result["items"][1]["cells"][1]
    assert (
        blank["text"] == ""
        and blank["evidence"] == []
        and blank["value_state"] == "unknown"
    )
    ev = row["cells"][1]["evidence"][0]
    assert ev["quote"] == "900" and ev["evidence_id"] in s.evidence
    region = s.index.by_id[ev["region_id"]]
    a, b = ev["text_span"]
    assert region.text[a:b] == "900"
    assert all(
        p["entity_id"] in ev["entity_ids"]
        for p in s.evidence[ev["evidence_id"]]["source_parts"]
    )
    with pytest.raises(ValueError):
        s.execute("read_table", {"table_id": "foreign"})


def test_generic_facts_are_separate_cached_candidates(workspace, general_drawing):
    s = session(workspace, general_drawing)
    r = s.execute("extract_facts", {"extractor_ref": "generic.table@1"})
    assert r["fact_count"] >= 5
    assert (
        s.fact_set_id is None
        and workspace.store.get("drawing", general_drawing["id"])["latest_fact_set_id"]
        is None
    )
    assert s.execute("extract_facts", {"extractor_ref": "generic.table@1"})["cached"]
    q = s.execute(
        "query_facts", {"fact_type": "generic", "subject": "M101", "property": "宽度"}
    )
    assert q["total"] == 1
    fact = q["items"][0]
    assert fact["value"]["value"] == "900" and fact["property"]["state"] == "derived"
    assert (
        fact["review_status"] == "needs_review" and fact["evidence_roles"]["property"]
    )
    s.execute("get_evidence", {"evidence_ids": fact["evidence_ids"]})
    assert all(e in s.evidence for e in fact["evidence_ids"])


def test_key_value_extraction_does_not_invent_subject(workspace, general_drawing):
    s = session(workspace, general_drawing)
    s.execute("extract_facts", {"extractor_ref": "generic.key_value@1"})
    facts = s.execute(
        "query_facts", {"fact_type": "generic.key_value.v1", "property": "防水等级"}
    )["items"]
    assert len(facts) == 1 and facts[0]["value"]["value"] == "一级"
    assert facts[0]["subject"]["state"] == "unknown"
    with pytest.raises(ValueError):
        s.execute("extract_facts", {"extractor_ref": "generated.python@1"})


def test_geometry_metadata_block_identity_and_scope(workspace, general_drawing):
    s = session(workspace, general_drawing)
    result = s.execute("query_entities", {"kind": "LINE", "layer": "DOOR"})
    assert result["total"] == 1
    e = result["items"][0]
    assert e["block_name"] == "DOOR" and len(e["entity_path"]) >= 2
    assert e["bbox"] == [250, 80, 260, 80]
    assert (
        s.execute("query_entities", {"bbox": [0.0, 0.0, 120.0, 45.0], "layer": "DOOR"})[
            "total"
        ]
        == 0
    )
    with pytest.raises(ValueError):
        s.execute("query_entities", {"bbox": [float("nan"), 0.0, 10.0, 20.0]})
    with pytest.raises(ValueError):
        s.execute("query_entities", {"frame_id": "foreign"})


def test_entity_observation_keeps_text_source_bridge_and_pagination(workspace,general_drawing):
    s=session(workspace,general_drawing)
    result=json.loads(s.invoke_tool('query_entities',{'kind':'TEXT','limit':1}))
    observation=s.observations[result['observation_id']]['data']
    assert observation['items'][0]['text']==result['items'][0]['text']
    assert observation['items'][0]['source_region_ids']==result['items'][0]['source_region_ids']
    assert observation['items'][0]['layout_id']==result['items'][0]['layout_id']
    assert observation['has_more']==result['has_more'] and observation['offset']==0


def test_relation_tools_open_after_input_artifacts_exist(workspace,general_drawing):
    s=session(workspace,general_drawing);s.task.auto_plan()
    s.task.data['subtasks'][0]['operations']=['relations','vision','geometry']
    assert 'query_entities' in s.focus.names()
    assert not {'query_building_relations','measure_geometry'}&s.focus.names()
    s.invoke_tool('query_entities',{'kind':'TEXT','limit':1})
    assert {'query_building_relations','render_region'}<=s.focus.names()
    assert 'measure_geometry' not in s.focus.names()


def test_preview_is_escaped_and_does_not_claim_model_vision(workspace, general_drawing):
    s = session(workspace, general_drawing)
    r = s.execute("search_sources", {"query": "<script>"})["items"][0]
    out = s.execute("inspect_region", {"region_id": r["region_id"]})
    assert (
        out["visual_input_to_model"] is False
        and out["evidence"]["evidence_id"] in s.evidence
    )
    svg = s.index.svg(r["region_id"])
    root = ElementTree.fromstring(svg)
    assert all(not e.tag.endswith("script") for e in root.iter())
    assert "&lt;script&gt;" in svg


def test_fact_evidence_is_snapshot_scoped(workspace, general_drawing, demo_bytes):
    s = session(workspace, general_drawing)
    s.execute("extract_facts", {"extractor_ref": "generic.key_value@1"})
    fact = s.execute("query_facts", {"fact_type": "generic"})["items"][0]
    other = session(workspace, workspace.ingest("other.dxf", demo_bytes))
    with pytest.raises(ValueError):
        other.execute("get_evidence", {"evidence_ids": fact["evidence_ids"]})


def test_answer_evidence_role_ids_must_be_read_and_declared(workspace, general_drawing):
    s = session(workspace, general_drawing)
    with pytest.raises(ValueError):
        s.validate_answer(
            json.dumps(
                {
                    "claims": [
                        {
                            "text": "未知",
                            "state": "unknown",
                            "evidence_ids": [],
                            "evidence_roles": {"scope": ["foreign"]},
                        }
                    ]
                }
            )
        )


def test_structure_and_preview_http_routes(workspace, general_drawing):
    # All existing work is idle before a second test app opens the fixture store.
    app = create_app(workspace.settings)
    with TestClient(app) as client:
        prefix = "/api/drawings/" + general_drawing["id"]
        ts = client.get(prefix + "/structure?view=tables").json()
        assert ts["total"] == 1
        assert (
            client.get(prefix + "/tables/" + ts["items"][0]["table_id"]).status_code
            == 200
        )
        rid = workspace.regions(general_drawing["id"])[0].region_id
        response = client.get(prefix + "/sources/" + rid + "/preview.svg")
        assert (
            response.status_code == 200
            and "image/svg+xml" in response.headers["content-type"]
        )
        assert (
            client.get(
                prefix + "/sources/" + rid + "/preview.svg?padding=9"
            ).status_code
            == 422
        )
        assert client.get(prefix + "/structure?view=exec").status_code == 422


def test_tool_observation_distinct_from_drawing_evidence(workspace, general_drawing):
    s = session(workspace, general_drawing)
    result = json.loads(
        s.invoke_tool("query_entities", {"kind": "LINE", "layer": "DOOR"})
    )
    oid = result["observation_id"]
    answer = s.validate_answer(
        json.dumps(
            {
                "claims": [
                    {
                        "text": "工具查到1个DOOR图层的LINE图元。",
                        "state": "observed",
                        "evidence_basis": "tool_observation",
                        "observation_ids": [oid],
                    }
                ]
            }
        )
    )
    assert (
        answer["citations"] == []
        and answer["tool_observations"][0]["observation_id"] == oid
    )
    assert "非原文结论" in answer["answer"]
    with pytest.raises(ValueError):
        s.validate_answer(
            json.dumps(
                {
                    "claims": [
                        {"text": "900厚", "state": "observed", "observation_ids": [oid]}
                    ]
                }
            )
        )
    with pytest.raises(ValueError):
        s.validate_answer(
            json.dumps(
                {
                    "claims": [
                        {
                            "text": "2个图元",
                            "state": "observed",
                            "evidence_basis": "tool_observation",
                            "observation_ids": ["foreign"],
                        }
                    ]
                }
            )
        )


def test_table_metadata_rows_can_be_cited_without_fake_source_quote(
    workspace, general_drawing
):
    s = session(workspace, general_drawing)
    tid = s.index.tables[0]["table_id"]
    result = json.loads(s.invoke_tool("read_table", {"table_id": tid, "row_limit": 2}))
    result = s.validate_answer(
        json.dumps(
            {
                "claims": [
                    {
                        "text": "工具读取了第2行单元格。",
                        "state": "observed",
                        "evidence_basis": "tool_observation",
                        "observation_ids": [result["observation_id"]],
                    }
                ]
            }
        )
    )
    assert result["tool_observations"] and not result["citations"]


def test_merged_header_keeps_span_without_inheriting_unit(workspace, tmp_path):
    def build(doc):
        m = doc.modelspace()
        for y in (0, 15, 30):
            m.add_line((0, y), (120, y))
        for x in (0, 80, 120):
            m.add_line((x, 0), (x, 30))
        m.add_line((40, 0), (40, 15))
        for x, y, text in [
            (5, 23, "门窗尺寸(mm)"),
            (85, 23, "备注"),
            (5, 8, "M1"),
            (45, 8, "900"),
            (85, 8, "详图"),
        ]:
            m.add_text(text, dxfattribs={"insert": (x, y), "height": 2})

    d = workspace.ingest("merged.dxf", dxf_bytes(tmp_path, build))
    index = DrawingIndex(workspace, d["id"])
    t = index.read_table(index.tables[0]["table_id"])
    assert t["items"][0]["cells"][0]["column_span"] == 2
    assert all(c["unit"] is None for row in t["items"] for c in row["cells"])


def test_generic_section_scope_does_not_cross_sibling_heading(workspace, tmp_path):
    def build(doc):
        m = doc.modelspace()
        # Bordered reading column, not a table.
        m.add_lwpolyline([(0, 0), (120, 0), (120, 100), (0, 100)], close=True)
        for y, text in [
            (94, "建筑设计说明"),
            (80, "一、防水工程"),
            (68, "防水等级：一级"),
            (50, "二、保温工程"),
            (38, "保温材料：岩棉"),
        ]:
            m.add_text(text, dxfattribs={"insert": (5, y), "height": 2})

    d = workspace.ingest("sections.dxf", dxf_bytes(tmp_path, build))
    s = session(workspace, d)
    heading = next(
        i
        for i in s.execute("get_drawing_overview", {"view": "sections"})["items"]
        if "防水工程" in i["title_candidate"]
    )
    assert (
        s.execute(
            "search_sources", {"section_id": heading["region_id"], "query": "一级"}
        )["total"]
        == 1
    )
    assert (
        s.execute(
            "search_sources", {"section_id": heading["region_id"], "query": "岩棉"}
        )["total"]
        == 0
    )


def test_generic_extraction_observation_is_not_fact_set_id(workspace, general_drawing):
    s = session(workspace, general_drawing)
    result = json.loads(
        s.invoke_tool("extract_facts", {"extractor_ref": "generic.key_value@1"})
    )
    assert result["observation_id"].startswith("obs_")
    assert result["observation_id"] != result["fact_set_id"]
    assert (
        s.observations[result["observation_id"]]["data"]["coverage"]
        == result["coverage"]
    )


def test_single_role_reference_normalizes_but_stays_source_checked(
    workspace, general_drawing
):
    s = session(workspace, general_drawing)
    rid = s.execute("search_sources", {"query": "防水等级"})["items"][0]["region_id"]
    e = s.execute("read_source_context", {"region_id": rid, "neighbors": 0})["items"][
        0
    ]["evidence"]["evidence_id"]
    result = s.validate_answer(
        json.dumps(
            {
                "claims": [
                    {
                        "text": "防水等级：一级",
                        "state": "observed",
                        "evidence_ids": [e],
                        "evidence_roles": {"value": e},
                    }
                ]
            }
        )
    )
    assert result["claims"][0]["evidence_roles"]["value"] == [e]
    with pytest.raises(ValueError):
        s.validate_answer(
            json.dumps(
                {
                    "claims": [
                        {
                            "text": "防水等级：一级",
                            "state": "observed",
                            "evidence_ids": [e],
                            "evidence_roles": {"value": "foreign"},
                        }
                    ]
                }
            )
        )
