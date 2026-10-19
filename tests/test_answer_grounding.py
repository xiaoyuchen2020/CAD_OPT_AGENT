import json
import os

import pytest

from cad_agent.analysis.coverage import EvidenceCoverage, question_parts
from cad_agent.analysis.session import Claim, tool_definitions
from cad_agent.analysis.grounding import verify_calculations
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.runtimes.bridge import ModelBridge
from cad_agent.adapters.model import ModelError
from test_siwc import make_session, connected_lease, response
from test_siwc_stopping import answer
from test_agent import final


def calculation_claim():
    return Claim(text="由原文计算约为66.7，适用条件仍需核对。", state="derived", evidence_ids=["a", "b"],
                 calculations=[{"operation": "multiply", "operands": [
                     {"value": "200", "evidence_id": "a"}, {"value": "1/3", "evidence_id": "b"}],
                     "result": "66.7", "decimal_places": 1}])


def test_derived_fraction_calculation_requires_both_sources():
    c = calculation_claim()
    assert verify_calculations(c, {"a": {"quote": "墙厚200"}, "b": {"quote": "不宜超过墙厚1/3"}}) == {"66.7"}


@pytest.mark.parametrize("fault", ["observed", "unknown", "unread", "uncited", "wrong_source", "substring", "sign", "wrong_result", "zero", "absent_text", "observation"])
def test_invalid_calculation_cannot_bypass_provenance(fault):
    c = calculation_claim(); e = {"a": {"quote": "墙厚200"}, "b": {"quote": "不宜超过墙厚1/3"}}
    if fault in ("observed", "unknown"): c.state = fault
    elif fault == "unread": del e["b"]
    elif fault == "uncited": c.evidence_ids = ["a"]
    elif fault == "wrong_source": c.calculations[0].operands[0].evidence_id = "b"
    elif fault == "substring": e["a"]["quote"] = "墙厚1200"
    elif fault == "sign": e["a"]["quote"] = "数值-200"
    elif fault == "wrong_result": c.calculations[0].result = "66.8"
    elif fault == "zero":
        c.calculations[0].operands[1].value = "1/0"; e["b"]["quote"] = "1/0"
    elif fault == "absent_text": c.text = "这是计算结果。"
    elif fault == "observation": c.evidence_basis = "tool_observation"
    with pytest.raises(ValueError): verify_calculations(c, e)


def test_arithmetic_is_a_closed_schema_not_executable():
    c = calculation_claim().model_dump()
    c["calculations"][0]["operands"][0]["value"] = "__import__('os').system('true')"
    with pytest.raises(ValueError): Claim.model_validate(c)
    c = calculation_claim().model_dump(); c["calculations"][0]["operation"] = "eval"
    with pytest.raises(ValueError): Claim.model_validate(c)


def test_numeric_validation_and_calc_result_are_claim_local(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    s.evidence = {"a": {"quote": "墙厚200", "evidence_id": "a"}, "b": {"quote": "不宜超过墙厚1/3", "evidence_id": "b"}}
    c = calculation_claim().model_dump()
    assert s.validate_answer(json.dumps({"claims": [c]}))["claims"][0]["calculations"]
    c["calculations"] = []
    with pytest.raises(ValueError, match="66.7"): s.validate_answer(json.dumps({"claims": [c]}))
    c.update(text="墙厚20", state="observed")
    with pytest.raises(ValueError, match="20"): s.validate_answer(json.dumps({"claims": [c]}))


def test_multipart_coverage_requires_answer_or_explicit_unknown(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes); s.coverage = EvidenceCoverage("请说明①构造；②施工时机。")
    c = {"text": "未找到构造依据。", "state": "unknown", "question_part_ids": ["q1"]}
    with pytest.raises(ValueError, match="q2"): s.validate_answer(json.dumps({"claims": [c]}))
    result = s.validate_answer(json.dumps({"claims": [c, {"text": "施工时机尚缺原文，无法确定。", "state": "unknown", "question_part_ids": ["q2"]}]}))
    assert not result["citations"]
    assert not question_parts("距洞口300，厚度200？")


def test_candidate_coverage_tracks_offsets_not_search_hits():
    c = EvidenceCoverage("query")
    c.record("search_sources", {"items": [{"region_id": "r", "snippet_start": 2000}]})
    c.record("read_source_context", {"evidence_id": "e", "region_id": "r", "quote": "x", "text_span": [0, 2000]})
    assert c.pending() == [{"region_id": "r", "start": 2000, "neighbors": 1}]
    c.record("read_source_context", {"evidence_id": "e2", "region_id": "r", "quote": "y", "text_span": [2000, 2100]})
    assert not c.pending()


def test_reserve_prefers_two_top_hits_of_latest_search():
    c = EvidenceCoverage("query")
    c.record("search_sources", {"items": [{"region_id": "old"}]})
    c.record("search_sources", {"items": [{"region_id": rid} for rid in ["general", "table", "noise"]]})
    assert [p["region_id"] for p in c.pending()] == ["general", "table"]
    assert all(rid != "noise" for rid, _ in c.candidates)


def seed_candidate(s):
    rid = s.w.regions(s.request.snapshot_id)[0].region_id
    s.coverage.record("search_sources", {"items": [{"region_id": rid}]})
    # Successful search without pretending it read source evidence.
    s.trace.append({"tool": "search_sources", "status": "completed"})
    return rid


@pytest.mark.parametrize("runtime", ["custom", "codex"])
def test_soft_stop_reads_pending_source_once_in_shared_final_context(workspace, demo_bytes, monkeypatch, runtime):
    s = make_session(workspace, demo_bytes); connected_lease(workspace, monkeypatch)
    rid = seed_candidate(s); s.usage.append({"total_tokens": 31000})
    # Trigger answer reserve using some already read source, as in the real failure.
    s.evidence["old"] = {"evidence_id": "old", "region_id": "old", "quote": "旧证据", "text_span": [0, 3]}
    calls = []
    def request(self, session, payload):
        calls.append(payload)
        assert payload["tool_choice"] == "none" and not payload["tools"]
        context = json.loads(payload["input"][0]["content"])
        assert not context["evidence_coverage"]["pending_source_reads"]
        assert any(x["region_id"] == rid for x in context["evidence"])
        return answer(final(next(e for e in s.evidence if e != "old")))
    monkeypatch.setattr(ResponsesTransport, "request", request)
    if runtime == "custom": result = s.run()
    else:
        with ModelBridge(s) as bridge:
            msg, _ = bridge.complete({"model": s.w.settings.model, "messages": [], "tools": tool_definitions()})
            result = s.finish(msg["content"])
    assert len(calls) == 1 and result["citations"]
    assert s.trace[-1]["trigger"] == "coverage_reserve"
    assert len([x for x in s.trace if x["tool"] == "read_source_context"]) == 1


@pytest.mark.parametrize("reason", ["subscription_answer_time_reserve", "tool_call_budget", "no_new_information", "answer_repair"])
def test_coverage_read_does_not_override_hard_stops(workspace, demo_bytes, reason):
    s = make_session(workspace, demo_bytes); seed_candidate(s)
    before = len(s.trace)
    s.final_context(CodexSubscriptionRuntime(), reason)
    assert len(s.trace) == before and not s.evidence


@pytest.mark.parametrize("runtime", ["custom", "codex"])
def test_forced_final_retains_validation_feedback_and_allows_only_one_repair(workspace, demo_bytes, monkeypatch, runtime):
    s = make_session(workspace, demo_bytes); connected_lease(workspace, monkeypatch)
    s.invoke_tool("read_source_context", {"region_id": s.w.regions(s.request.snapshot_id)[0].region_id})
    s.usage.append({"total_tokens": 40000}); calls = []
    def request(self, session, payload):
        calls.append(payload)
        assert payload["tool_choice"] == "none" and not payload["tools"]
        if len(calls) == 1: return answer("invalid JSON private-marker")
        ctx = json.loads(payload["input"][0]["content"])
        assert ctx["validation_issue"] and "private-marker" not in ctx["validation_issue"]
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport, "request", request)
    if runtime == "custom":
        assert s.run()["citations"]
    else:
        with ModelBridge(s) as b:
            payload = {"model": s.w.settings.model, "messages": [], "tools": tool_definitions()}
            msg, _ = b.complete(payload)
            try: s.finish(msg["content"])
            except ValueError as error: s.record_validation_issue(error)
            s.answer_repairs += 1; s.policy.stop_reason = "answer_repair"
            msg, _ = b.complete(payload)
            assert s.finish(msg["content"])["citations"]
            with pytest.raises(ModelError): b.complete(payload)
    assert len(calls) == 2 and s.answer_repairs == 1


def test_failed_second_answer_does_not_retry_forever(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes); connected_lease(workspace, monkeypatch)
    s.invoke_tool("read_source_context", {"region_id": s.w.regions(s.request.snapshot_id)[0].region_id})
    calls = []
    def request(*args): calls.append(1); return answer("bad JSON")
    monkeypatch.setattr(ResponsesTransport, "request", request)
    with pytest.raises(ModelError, match="纠错"): s.run()
    assert len(calls) == 2


def test_total_budget_blocks_validation_repair(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes); connected_lease(workspace, monkeypatch)
    s.invoke_tool("read_source_context", {"region_id": s.w.regions(s.request.snapshot_id)[0].region_id})
    s.usage.append({"total_tokens": CodexSubscriptionRuntime().total_budget(s)-10000}); calls = []
    def request(self, session, payload):
        calls.append(payload); session.usage.append({"total_tokens": 10000})
        return answer("bad JSON")
    monkeypatch.setattr(ResponsesTransport, "request", request)
    with pytest.raises(ModelError): s.run()
    assert len(calls) == 1 and not s.answer_repairs


def test_large_final_packet_keeps_one_reserved_correction(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    s.invoke_tool('read_source_context',{'region_id':s.w.regions(s.request.snapshot_id)[0].region_id})
    s.usage.append({'total_tokens':40000});calls=[]
    def request(self,session,payload):
        calls.append(payload);assert payload['tool_choice']=='none' and not payload['tools']
        session.usage.append({'total_tokens':18000})
        return answer('invalid JSON') if len(calls)==1 else answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    assert s.run()['citations']
    assert len(calls)==2 and s.answer_repairs==1
    assert len(s.runtime_diagnostics['rejected_answers'])==1


def test_reserve_reads_at_most_two_candidates_once(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    regions = s.w.regions(s.request.snapshot_id)
    for region in regions[:4]:
        s.coverage.record("search_sources", {"items": [{"region_id": region.region_id}]})
    controls = CodexSubscriptionRuntime()
    s.final_context(controls, "exploration_budget")
    count = s.calls
    assert 0 < count <= 2
    s.final_context(controls, "exploration_budget")
    assert s.calls == count


@pytest.mark.skipif(os.environ.get('CAD_TEST_EXTERNAL_HARNESSES') != '1', reason='Real CLI, mocked upstream only')
def test_real_codex_correction_receives_structured_feedback(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes); connected_lease(workspace, monkeypatch)
    s.request.runtime = "codex"
    rid = s.w.regions(s.request.snapshot_id)[0].region_id
    calls = []
    def request(self, session, payload):
        calls.append(payload)
        if len(calls) == 1:
            return response([{"type": "function_call", "namespace": "cad", "name": "read_source_context",
                              "arguments": json.dumps({"region_id": rid}), "call_id": "c1"}])
        if len(calls) == 2: return answer("invalid JSON")
        assert len(calls) == 3 and payload["tool_choice"] == "none" and not payload["tools"]
        assert json.loads(payload["input"][0]["content"])["validation_issue"]
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport, "request", request)
    monkeypatch.setattr(workspace.gateway, "chat_turn", lambda *a, **k: pytest.fail("No paid API"))
    result = s.run()
    assert len(calls) == 3 and result["citations"] and not result["fallback_used"]
    assert result["efficiency"]["answer_repairs"] == 1
