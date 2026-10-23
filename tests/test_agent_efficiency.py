import json
from cad_agent.runtimes.bridge import ModelBridge

from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.efficiency import ExplorationPolicy
from cad_agent.analysis.session import source_context
from cad_agent.contracts import ChatRequest
from test_agent import call, final


def runner(workspace, demo_bytes, policy="efficient"):
    d = workspace.ingest("efficiency.dxf", demo_bytes)
    return AgentRunner(workspace, ChatRequest(
        snapshot_id=d["id"], question="内墙材料是什么", mode="agent", agent_policy=policy,
    ))


def test_same_defaults_reuse_but_paging_and_scope_do_not(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    original = s.execute
    calls = []

    def execute(name, args, **kw):
        calls.append(args)
        return original(name, args, **kw)

    monkeypatch.setattr(s, "execute", execute)
    s.invoke_tool("search_sources", {"query": "内墙"})
    result = json.loads(s.invoke_tool("search_sources", {"query": "内墙", "limit": 5}))
    assert result["already_returned"] and len(calls) == 1
    s.invoke_tool("search_sources", {"query": "内墙", "offset": 1})
    frame = s.index.regions[0].frame_id
    s.invoke_tool("search_sources", {"query": "内墙", "frame_id": frame})
    assert len(calls) == 3


def test_repeated_evidence_keeps_original_quote_and_full_ui_location(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    rid = s.w.regions(s.request.snapshot_id)[0].region_id
    first = json.loads(s.invoke_tool("read_source_context", {"region_id": rid}))
    e = first["items"][0]["evidence"]
    assert "text" not in first["items"][0] and "entity_ids" not in e
    second = json.loads(s.invoke_tool("get_evidence", {"evidence_ids": [e["evidence_id"]]}))
    assert second["items"][0] == {"evidence_id": e["evidence_id"], "already_read": True}
    actual = s.finish(final(e["evidence_id"]))["citations"][0]
    assert actual["quote"] == e["quote"] and actual["entity_ids"] and actual["source_parts"]


def test_bounded_context_paginates_exact_spans_without_reading_tail(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    region = s.w.regions(s.request.snapshot_id)[0]
    region = region.model_copy(update={"text": "设计要求" * 1100, "context_region_ids": []})
    monkeypatch.setattr(workspace, "regions", lambda _: [region])
    bounded = source_context(workspace, s.request.snapshot_id, region.region_id, bounded=True)
    first = bounded["items"][0]
    assert first["next_start"] == 2000 and first["truncated"]
    second = source_context(workspace, s.request.snapshot_id, region.region_id, start=2000, bounded=True)
    assert second["items"][0]["text_span"] == [2000, 4000]
    for item in [first, second["items"][0]]:
        assert item["evidence"]["quote"] == region.text[slice(*item["text_span"])]
    assert len(source_context(workspace, s.request.snapshot_id, region.region_id)["items"][0]["text"]) == 4400


def test_new_search_results_are_not_evidence_sufficiency():
    p = ExplorationPolicy()
    for i in range(5):
        p.record("search_sources", {"items": [{"region_id": str(i)}]}, set(), False, 100)
    assert p.stop_reason is None
    for _ in range(3):
        p.record("search_sources", {"items": [{"region_id": "4"}]}, set(), False, 100)
    assert p.stop_reason == "no_new_information"


def test_fact_extraction_invalidates_query_cache(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    assert json.loads(s.invoke_tool("query_facts", {}))["needs_extraction"]
    s.invoke_tool("extract_facts", {})
    result = json.loads(s.invoke_tool("query_facts", {}))
    assert result["items"] and not result.get("already_returned")


def test_repeated_no_progress_forces_answer_not_more_tools(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    rounds = []

    def turn(messages, tools, timeout):
        rounds.append(bool(tools))
        if tools:
            return call("search_sources", {"query": "不存在的条款"}, len(rounds)), {}
        return {"content": json.dumps({"claims": [{"text": "已检索范围内无法确定。", "state": "unknown"}]})}, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)
    result = s.run()
    assert rounds == [True, True, True, False]
    assert result["efficiency"]["stop_reason"] == "no_requirement_progress"
    assert result["efficiency"]["duplicate_calls"] == 2


def test_legacy_preserves_payload_and_repeat_execution(workspace, demo_bytes):
    s = runner(workspace, demo_bytes, "legacy")
    rid = s.w.regions(s.request.snapshot_id)[0].region_id
    for _ in range(2):
        result = json.loads(s.invoke_tool("read_source_context", {"region_id": rid}))
        assert result["items"][0]["text"] and result["items"][0]["evidence"]["entity_ids"]
        assert "already_returned" not in result
    assert s.round_limit == 8 and not s.policy.stop_reason


def test_final_validation_repair_disables_exploration(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    rid = s.w.regions(s.request.snapshot_id)[0].region_id
    n = 0

    def turn(messages, tools, timeout):
        nonlocal n
        n += 1
        if n == 1:
            return call("read_source_context", {"region_id": rid}), {}
        if n == 2:
            return {"content": "bad json"}, {}
        assert not tools
        eid = next(iter(s.evidence))
        return {"content": final(eid)}, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)
    assert s.run()["citations"] and n == 3


def test_bridge_applies_shared_stop_without_enabling_tools(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    s.policy.stop_reason = "no_new_information"

    def turn(messages, tools, timeout):
        assert not tools and "探索预算" in messages[-1]["content"]
        return {"content": "{}"}, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", turn)
    with ModelBridge(s) as bridge:
        bridge.complete({"model": s.w.settings.model, "messages": s.build_messages(),
                         "tools": [{"type": "function", "function": {"name": "search_sources"}}]})
        assert bridge.requests[-1]["final_only"]
