import json
import time
import pytest
import httpx
from fastapi.testclient import TestClient
from cad_agent.analysis.agent import AgentRunner, source_context, tool_definitions
from cad_agent.adapters.model import ModelGateway, ModelError
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest, ExtractionRequest
from conftest import finish


def request(d, **kwargs):
    return ChatRequest(
        snapshot_id=d["id"], question="内墙的材料是什么？", mode="agent", **kwargs
    )


def final(eid):
    return json.dumps(
        {
            "claims": [
                {
                    "text": "该条款的材料要求见引用原文。",
                    "state": "observed",
                    "evidence_ids": [eid],
                }
            ],
            "missing_information": [],
        },
        ensure_ascii=False,
    )


def call(name, args, i=1):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"c{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def scripted_turns(workspace, monkeypatch):
    state = {"n": 0}

    def turn(messages, tools, timeout):
        state["n"] += 1
        n = state["n"]
        if n == 1:
            return call("search_sources", {"query": "内墙"}), {"total_tokens": 10}
        if n == 2:
            result = json.loads(messages[-1]["content"])
            return call(
                "read_source_context", {"region_id": result["items"][0]["region_id"]}, n
            ), {"total_tokens": 10}
        result = json.loads(messages[-1]["content"])
        eid = result["items"][0]["evidence"]["evidence_id"]
        return {"role": "assistant", "content": final(eid)}, {"total_tokens": 10}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)


def test_loop_reads_original_sources_without_fact_set(
    workspace, demo_bytes, monkeypatch
):
    d = workspace.ingest("demo.dxf", demo_bytes)
    scripted_turns(workspace, monkeypatch)
    result = AgentRunner(workspace, request(d)).run()
    assert [x["tool"] for x in result["trace"]] == [
        "search_sources",
        "read_source_context",
    ]
    assert result["fact_set_id"] is None
    assert len(result["citations"]) == 1
    e = result["citations"][0]
    r = next(r for r in workspace.regions(d["id"]) if r.region_id == e["region_id"])
    assert e["quote"] == r.text[slice(*e["text_span"])]
    assert set(e["entity_ids"]) <= set(r.entity_ids)
    assert "[1]" in result["answer"]


def test_cross_snapshot_and_unknown_tool_rejected(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    runner = AgentRunner(workspace, request(d))
    for name, args in [
        ("exec", {"code": "print(1)"}),
        ("read_source_context", {"region_id": "foreign"}),
        ("search_sources", {"query": "", "snapshot_id": "foreign"}),
        ("search_sources", {"frame_id": "foreign"}),
    ]:
        with pytest.raises(ValueError):
            runner.execute(name, args)
    assert {t["function"]["name"] for t in tool_definitions()} == {
        "extract_planning_metrics", "extract_building_schedule",
        "read_requirement_sources", "select_source_records",
        "read_assembly", "calculate_facts", "resolve_drawing_scope", "inspect_scope_objects", "read_project_sources",
        "plan_task", "analyze_measurements", "get_analysis_results", "check_dimension_chain", "inspect_enclosure",
        "locate_space_labels", "prepare_space_connections", "review_space_connections", "prepare_discovery_review", "review_discovered_spaces",
        "analyze_room_batch", "resolve_room_units",
        "get_drawing_overview",
        "search_sources",
        "read_source_context",
        "extract_facts",
        "query_facts",
        "get_evidence",
        "read_table", "query_entities", "inspect_region",
        "read_geometry", "query_spatial", "measure_geometry", "measure_room", "get_measurement_evidence", "discover_rooms", "get_room_contours", "measure_room_area",
        "render_region", "bind_visual_objects", "prepare_room_boundaries", "select_room_boundaries",
        "read_dimensions", "query_building_relations", "get_drawing_context", "get_component_evidence",
        "locate_by_axes", "query_annotations", "read_table_semantics", "trace_detail",
        "bind_spatial_object", "get_spatial_object", "inspect_spatial_boundary",
        "measure_spatial_area", "measure_spatial_relation", "inspect_door_sweep",
        "measure_door_sweep_relation", "measure_spatial_sections", "measure_elevation_relation",
        "read_evidence_record", "query_property_evidence",
        "read_door_schedule_table", "trace_door_schedule", "search_similar_objects",
    }


def test_evidence_must_be_read_and_numbers_must_be_grounded(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    runner = AgentRunner(workspace, request(d))
    with pytest.raises(ValueError):
        runner.validate_answer(final("ev_invented"))
    region = workspace.regions(d["id"])[0]
    item = runner.execute(
        "read_source_context", {"region_id": region.region_id, "neighbors": 0}
    )["items"][0]
    eid = item["evidence"]["evidence_id"]
    bad = json.loads(final(eid))
    bad["claims"][0]["text"] = "材料强度为 A999.9"
    with pytest.raises(ValueError):
        runner.validate_answer(json.dumps(bad))
    bad["claims"][0]["text"] = "普通材料"
    bad["claims"][0]["evidence_ids"] = []
    with pytest.raises(ValueError):
        runner.validate_answer(json.dumps(bad))
    assert runner.validate_answer(final(eid))["citations"][0]["evidence_id"] == eid


def test_agent_can_extract_then_query_and_read_evidence(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    runner = AgentRunner(workspace, request(d))
    assert runner.execute("query_facts", {})["needs_extraction"]
    result = runner.execute("extract_facts", {})
    assert result["practice_count"] == 9
    facts = runner.execute("query_facts", {"limit": 1})
    assert facts["has_more"]
    eid = facts["items"][0]["evidence_ids"][0]
    with pytest.raises(ValueError):
        runner.validate_answer(final(eid))
    runner.execute("get_evidence", {"evidence_ids": [eid]})
    assert runner.validate_answer(final(eid))["fact_set_id"] == result["fact_set_id"]


def test_invalid_tool_can_be_repaired_without_executing_it(
    workspace, demo_bytes, monkeypatch
):
    d = workspace.ingest("demo.dxf", demo_bytes)
    n = 0

    def turn(messages, tools, timeout):
        nonlocal n
        n += 1
        if n == 1:
            return call("shell", {"cmd": "bad"}), {}
        if n == 2:
            assert "INVALID_TOOL_INPUT" in messages[-1]["content"]
            return call("search_sources", {"query": "不存在"}), {}
        return {
            "content": json.dumps(
                {"claims": [{"text": "当前检索未找到，无法确定。", "state": "unknown"}]}
            )
        }, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)
    result = AgentRunner(workspace, request(d)).run()
    assert result["trace"][0]["status"] == "failed"
    assert not result["citations"]


def test_loop_budget_and_invalid_final_are_explicit_failures(
    workspace, demo_bytes, monkeypatch
):
    d = workspace.ingest("demo.dxf", demo_bytes)
    monkeypatch.setattr(
        workspace.gateway,
        "chat_turn",
        lambda *a, **kw: (call("search_sources", {"query": ""}), {}),
    )
    with pytest.raises(ModelError, match="轮数上限"):
        AgentRunner(workspace, request(d)).run()
    monkeypatch.setattr(
        workspace.gateway,
        "chat_turn",
        lambda *a, **kw: ({"content": final("invented")}, {}),
    )
    with pytest.raises(ModelError) as exc:
        AgentRunner(workspace, request(d)).run()
    assert exc.value.code == "ANSWER_VALIDATION_FAILED"


def test_background_api_persists_evidence_and_history(
    tmp_path, demo_bytes, monkeypatch
):
    app = create_app(Settings(data_dir=tmp_path / "api"))
    w = app.state.workspace
    d = w.ingest("demo.dxf", demo_bytes)
    scripted_turns(w, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/api/chat-runs", json=request(d).model_dump())
        assert response.status_code == 202
        rid = response.json()["id"]
        for _ in range(100):
            run = client.get(f"/api/chat-runs/{rid}").json()
            if run["status"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        assert run["status"] == "completed", run
        e = run["result"]["citations"][0]
        assert client.get(e["evidence_url"]).json()["quote"] == e["quote"]
        assert client.get(f"/api/chat-runs/{rid}/evidence/foreign").status_code == 404
        history = w.chat_history(request(d, parent_run_id=rid))
        assert len(history) == 2 and history[0]["content"] == request(d).question
        region = e["region_id"]
        assert (
            client.get(f'/api/drawings/{d["id"]}/sources/{region}/context').status_code
            == 200
        )
        assert (
            client.get(f'/api/drawings/{d["id"]}/sources/foreign/context').status_code
            == 400
        )


def test_deepseek_key_does_not_fall_back_to_old_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "cad_agent.config.local_env",
        lambda: {
            "CAD_AGENT_PROVIDER": "deepseek",
            "CAD_AGENT_API_KEY": "old-secret",
            "CAD_AGENT_DATA_DIR": str(tmp_path),
        },
    )
    s = Settings.load()
    assert s.api_key == ""
    assert s.api_base == "https://api.deepseek.com"
    assert s.model == "deepseek-flash"


def test_deepseek_tool_protocol_and_error_redaction(tmp_path, monkeypatch):
    s = Settings(
        data_dir=tmp_path,
        api_base="https://api.deepseek.com",
        api_key="private-key",
        model="deepseek-flash",
        protocol="openai",
        provider="deepseek",
    )
    original = httpx.Client

    def handle(req):
        payload = json.loads(req.content)
        assert req.url.path == "/chat/completions"
        assert payload["thinking"] == {"type": "disabled"}
        assert len(payload["tools"]) == len(tool_definitions())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "tool_calls", "message": call("query_facts", {})}
                ],
                "usage": {"total_tokens": 12},
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    msg, usage = ModelGateway(s).chat_turn(
        [{"role": "user", "content": "test"}], tool_definitions()
    )
    assert msg["tool_calls"][0]["function"]["name"] == "query_facts"
    assert usage["total_tokens"] == 12
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, text="private-key")
            ),
            **kw,
        ),
    )
    with pytest.raises(ModelError) as e:
        ModelGateway(s).chat_turn([], tool_definitions())
    assert "private-key" not in str(e.value)


def test_context_distinguishes_preceding_section_and_real_children(
    workspace, demo_bytes, monkeypatch
):
    from conftest import region

    d = workspace.ingest("demo.dxf", demo_bytes)
    heading_a = region("1.3、住宅墙体材料：", "heading-a", column_key="col")
    previous = region(
        "（3）其他内墙200厚。",
        "previous",
        column_key="col",
        context_region_ids=["heading-a"],
    )
    heading_b = region("1.4、公建墙体材料：", "heading-b", column_key="col")
    child = region(
        "（1）外墙240厚。", "child", column_key="col", context_region_ids=["heading-b"]
    )
    child_two = region(
        "（2）内墙厚度详建施图。",
        "child-two",
        column_key="col",
        context_region_ids=["heading-b"],
    )
    monkeypatch.setattr(
        workspace,
        "regions",
        lambda _: [heading_a, previous, heading_b, child, child_two],
    )
    result = source_context(workspace, d["id"], "heading-b")
    items = {r["region_id"]: r for r in result["items"]}
    assert items["previous"]["position_relative_to_target"] == "before"
    assert items["previous"]["parser_context_region_ids"] == ["heading-a"]
    assert items["child-two"]["relationship"] == "section_child"
    assert items["child-two"]["parser_context_region_ids"] == ["heading-b"]
    assert items["heading-a"]["is_heading"]
    assert items["previous"]["reading_order"] < items["heading-b"]["reading_order"]


def test_unconfigured_background_chat_is_failed_not_fallback(tmp_path, demo_bytes):
    app = create_app(Settings(data_dir=tmp_path / "api"))
    w = app.state.workspace
    d = w.ingest("demo.dxf", demo_bytes)
    with TestClient(app) as client:
        rid = client.post("/api/chat-runs", json=request(d).model_dump()).json()["id"]
        for _ in range(100):
            run = client.get(f"/api/chat-runs/{rid}").json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)
        assert run["status"] == "failed"
        assert run["error"]["code"] == "MODEL_NOT_CONFIGURED"
        assert run["result"] is None
        assert (
            client.post(
                "/api/chat", json={"snapshot_id": d["id"], "question": "内墙"}
            ).status_code
            == 200
        )


def test_cross_snapshot_fact_set_and_followup_rejected(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    workspace.store.put("fact_set", "foreign", {"snapshot_id": "another"})
    with pytest.raises(ValueError):
        AgentRunner(workspace, request(d, fact_set_id="foreign"))
    workspace.store.put(
        "chat_run", "foreign-chat", {"snapshot_id": "another", "status": "completed"}
    )
    with pytest.raises(ValueError):
        workspace.chat_history(request(d, parent_run_id="foreign-chat"))


def test_last_normal_round_has_one_bounded_citation_repair(
    workspace, demo_bytes, monkeypatch
):
    from dataclasses import replace

    workspace.settings = replace(workspace.settings, agent_max_rounds=2)
    d = workspace.ingest("demo.dxf", demo_bytes)
    region_id = workspace.regions(d["id"])[0].region_id
    n = 0

    def turn(messages, tools, timeout):
        nonlocal n
        n += 1
        if n == 1:
            return (
                call("read_source_context", {"region_id": region_id, "neighbors": 0}),
                {},
            )
        if n == 2:
            return {"role": "assistant", "content": final("ev_missing")}, {}
        tool_result = next(
            json.loads(m["content"]) for m in messages if m["role"] == "tool"
        )
        eid = tool_result["items"][0]["evidence"]["evidence_id"]
        return {"role": "assistant", "content": final(eid)}, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)
    result = AgentRunner(workspace, request(d)).run()
    assert result["citations"] and n == 3 and len(result["trace"]) == 1


def test_large_coverage_does_not_hide_fact_results(workspace, demo_bytes):
    d = workspace.ingest("demo.dxf", demo_bytes)
    job = finish(workspace, ExtractionRequest(snapshot_id=d["id"]))
    fid = job["result"]["fact_set_id"]
    facts = workspace.store.get("fact_set", fid)
    facts["coverage"]["skipped_region_ids"] = ["region_" + str(i) for i in range(10000)]
    workspace.store.put("fact_set", fid, facts)
    runner = AgentRunner(workspace, request(d, fact_set_id=fid))
    result = runner.execute("query_facts", {"limit": 1})
    assert result["items"]
    assert result["coverage"]["skipped_region_count"] == 10000
    assert "skipped_region_ids" not in result["coverage"]
    assert len(json.dumps(result)) < 50000


def test_overview_titles_are_citable_source_spans_not_file_names(workspace, tmp_path):
    from conftest import dxf_bytes

    def build(doc):
        m = doc.modelspace()
        for index, text in enumerate(
            [
                "建筑施工图设计总说明一",
                "建筑施工图设计总说明二",
                "建筑施工图设计总说明一",
                "依据《建筑设计说明》执行。",
            ]
        ):
            m.add_text(text, dxfattribs={"insert": (0, index * 50), "height": 3})

    d = workspace.ingest("不是图名-99.dxf", dxf_bytes(tmp_path, build))
    runner = AgentRunner(workspace, request(d))
    result = runner.execute("get_drawing_overview", {})
    assert result["file_metadata"]["name"] == "不是图名-99.dxf"
    assert result["total"] == 2
    assert sorted(i["occurrence_count"] for i in result["items"]) == [1, 2]
    regions = {r.region_id: r for r in workspace.regions(d["id"])}
    for item in result["items"]:
        e = runner.evidence[item["evidence"]["evidence_id"]]
        assert e["quote"] == regions[e["region_id"]].text[slice(*e["text_span"])]
        assert e["entity_ids"] and e["bbox"] and e["source_parts"]
    assert runner.validate_answer(final(result["items"][0]["evidence"]["evidence_id"]))[
        "citations"
    ]


def test_overview_without_title_does_not_invent_evidence(
    workspace, demo_bytes, monkeypatch
):
    d = workspace.ingest("建筑施工图设计总说明.dxf", demo_bytes)
    monkeypatch.setattr(workspace, "regions", lambda snapshot: [])
    runner = AgentRunner(workspace, request(d))
    result = runner.execute("get_drawing_overview", {})
    assert result["items"] == [] and result["total"] == 0
    assert not runner.evidence


@pytest.mark.parametrize("tool_limit", [1, 16])
def test_budget_reserves_final_answer_with_existing_evidence(
    workspace, demo_bytes, monkeypatch, tool_limit
):
    from dataclasses import replace

    workspace.settings = replace(
        workspace.settings, agent_max_rounds=3, agent_max_tool_calls=tool_limit
    )
    d = workspace.ingest("demo.dxf", demo_bytes)
    rid = workspace.regions(d["id"])[0].region_id
    rounds = []

    def turn(messages, tools, timeout):
        rounds.append(bool(tools))
        if tools:
            return (
                call(
                    "read_source_context",
                    {"region_id": rid, "neighbors": 0},
                    len(rounds),
                ),
                {},
            )
        result = next(json.loads(m["content"]) for m in messages if m["role"] == "tool")
        eid = result["items"][0]["evidence"]["evidence_id"]
        return {"role": "assistant", "content": final(eid)}, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)
    result = AgentRunner(workspace, request(d)).run()
    assert rounds == ([True, False] if tool_limit == 1 else [True, True, False])
    assert result["citations"]


def test_gateway_disables_tools_for_final_synthesis(tmp_path, monkeypatch):
    original = httpx.Client

    def handle(req):
        payload = json.loads(req.content)
        assert payload["tool_choice"] == "none"
        assert "tools" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "answer"},
                    }
                ]
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    s = Settings(
        data_dir=tmp_path,
        api_base="https://api.deepseek.com",
        api_key="test-only",
        model="deepseek-flash",
        protocol="openai",
        provider="deepseek",
    )
    assert ModelGateway(s).chat_turn([], [])[0]["content"] == "answer"
