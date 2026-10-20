"""Behavioral regressions for lossless context, repeat guards and budget policy."""
import json
from dataclasses import replace
import pytest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.evidence_store import save_session, read, EvidenceRecordRead, working_result
from cad_agent.analysis.failure_guard import register_failure, blocked, progress_signature
from cad_agent.runtimes.budget_policy import STANDARD, DEEP, for_session, preflight, stop_reason, cache_summary
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.adapters.model import ModelError
from test_siwc import make_session, connected_lease, response
from test_agent import final


def test_profiles_are_standard_and_deep_with_legacy_escape_hatch(workspace):
    catalog = workspace.settings.model_catalog()
    low, high = catalog["siwc_sol"], catalog["siwc_sol_high"]
    assert (low.model, low.reasoning_effort, low.agent_max_rounds, low.agent_max_tool_calls, low.agent_timeout_seconds) == ("gpt-6.1-sol", "low", 32, 40, 600)
    assert (high.model, high.reasoning_effort, high.agent_max_rounds, high.agent_max_tool_calls, high.agent_timeout_seconds) == ("gpt-6.1-sol", "high", 48, 64, 900)
    legacy = replace(workspace.settings, subscription_budget_policy="legacy").model_catalog()["siwc_sol"]
    assert legacy.agent_max_rounds == workspace.settings.agent_max_rounds


def test_archive_roundtrip_page_hash_and_cross_task_rejection(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    original = {"quote":"建筑原文：保留全部条件与例外。" * 1500, "geometry":[[i,i/7] for i in range(6000)]}
    ref = save_session(s, "test_full_record", original)
    pieces, start = [], 0
    while True:
        page = read(s, EvidenceRecordRead(record_id=ref["record_id"], start=start))
        assert len(page["content"]) <= 6000
        pieces.append(page["content"])
        if not page["has_more"]:break
        start = page["next_start"]
    assert json.loads("".join(pieces)) == original
    assert s.evidence == {} and s.observations == {}  # storage never grants read credit
    other = make_session(workspace, demo_bytes)
    with pytest.raises(ValueError, match="未在本任务"):
        read(other, EvidenceRecordRead(record_id=ref["record_id"]))
    record = workspace.store.get("evidence_archive", ref["record_id"])
    workspace.store.put("evidence_archive", ref["record_id"], record | {"text": record["text"]+"x"})
    with pytest.raises(ValueError, match="完整性"):
        read(s, EvidenceRecordRead(record_id=ref["record_id"]))


def test_archive_catalog_and_source_snapshot_mismatch(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    refs = [save_session(s, "test", {"i":i}) for i in range(15)]
    page = read(s, EvidenceRecordRead(view="catalog"))
    assert len(page["items"]) == 12 and page["next_start"] == 12
    drawing = workspace.store.get("drawing", s.request.snapshot_id)
    workspace.store.put("drawing", s.request.snapshot_id, drawing | {"sha256":"changed"})
    with pytest.raises(ValueError, match="版本"):
        read(s, EvidenceRecordRead(record_id=refs[0]["record_id"]))


def test_tool_error_never_replays_full_task_and_third_execution_is_blocked(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes)
    s.task.data["large_geometry"] = list(range(400000))
    calls = []
    def invalid(*a, **kw):
        calls.append(1)
        raise ValueError("缺少来源依据")
    monkeypatch.setattr(s, "execute", invalid)
    args = {"query":"混凝土"}
    first = json.loads(s.invoke_tool("search_sources", args))
    second = json.loads(s.invoke_tool("search_sources", args))
    third = json.loads(s.invoke_tool("search_sources", args))
    assert len(json.dumps(first)) < 6000 and "task" not in first
    assert second["retry"]["unchanged_retry_allowed"] is False
    assert third["error"]["code"] == "REPEATED_TOOL_FAILURE"
    assert len(calls) == 2 and s.policy.stop_reason == "repeated_tool_failure"


def test_real_progress_reopens_failed_call_but_control_bookkeeping_does_not(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    s.task.auto_plan()
    args = {"region_id":"bad", "neighbors":1, "start":0}
    result = {"error":{"code":"INVALID_TOOL_INPUT","message":"invalid source"}}
    for _ in range(2):register_failure(s, "read_source_context", args, result)
    assert blocked(s, "read_source_context", args)
    s.calls += 3
    assert blocked(s, "read_source_context", args)
    s.observations["new"] = {"tool":"measure_geometry","data":{"value":15,"unit":"mm"}}
    assert not blocked(s, "read_source_context", args)


def test_cached_unknown_is_not_zero(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    s.runtime_diagnostics = {"model_requests":[{}]}
    ResponsesTransport.record_usage(s, response([]))
    r = response([]); r["usage"].pop("input_tokens_details")
    ResponsesTransport.record_usage(s, r)
    summary = cache_summary(s.usage)
    assert summary["cached_input_tokens"] == 80 and summary["cached_input_ratio"] == .8
    assert summary["missing_cache_usage_requests"] == 1
    assert s.runtime_diagnostics["model_requests"][-1]["cache_usage"]["cache_hit"] is None


def test_preflight_reserves_final_and_unknown_usage(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes)
    s.runtime_diagnostics = {"reserved_unknown_usage_tokens":40000}
    monkeypatch.setattr("cad_agent.runtimes.budget_policy.estimate_input", lambda p:(60000,"test_bound"))
    payload = {"max_output_tokens":8192}
    s.usage = [{"total_tokens":750000}]
    with pytest.raises(ModelError, match="预留"):
        preflight(s, payload, {})
    s.usage = [{"total_tokens":650000}]
    entry = {}
    preflight(s, payload, entry)
    assert entry["budget_preflight"]["charged_before"] == 690000
    assert entry["budget_preflight"]["finalization_reserve"] == 150000


def test_soft_budget_requires_new_fact_progress(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    s.runtime_diagnostics = {"model_requests":[]}
    s.usage = [{"total_tokens":500000}]
    assert stop_reason(s) is None
    s.usage = [{"total_tokens":600000}]
    assert stop_reason(s) == "soft_budget_no_progress"
    s.observations["measure"] = {"tool":"measure_geometry","data":{"value":10}}
    assert stop_reason(s) is None
    assert stop_reason(s) == "soft_budget_no_progress"
    s.usage = [{"total_tokens":850000}]
    assert stop_reason(s) == "finalization_token_reserve"


def test_prefix_is_stable_through_repair_and_records_actual_cache(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes)
    connected_lease(workspace, monkeypatch)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    payloads = []
    def request(self, session, payload):
        from copy import deepcopy
        payloads.append(deepcopy(payload))
        if len(payloads) == 1:
            r = response([{"type":"function_call","namespace":"cad","name":"read_source_context",
                          "arguments":json.dumps({"region_id":rid}),"call_id":"read"}])
        elif len(payloads) == 2:
            r = response([{"type":"message","content":[{"type":"output_text","text":"not JSON"}]}])
        else:
            r = response([{"type":"message","content":[{"type":"output_text","text":final(next(iter(s.evidence)))}]}])
        self.record_usage(session, r)
        return r
    monkeypatch.setattr(ResponsesTransport, "request", request)
    result = s.run()
    assert len(payloads) == 3 and result["citations"]
    assert all(p["instructions"] == payloads[0]["instructions"] and p["tools"] == payloads[0]["tools"] for p in payloads)
    assert payloads[-1]["tool_choice"] == "none"
    assert result["cache_usage"]["cached_input_tokens"] == 240
    assert len({r["prefix_sha256"] for r in result["model_requests"]}) == 1
    assert payloads[1]["input"][:len(payloads[0]["input"])] == payloads[0]["input"]


def test_full_geometry_is_readable_beyond_model_excerpt(workspace):
    import math
    from test_room_geometry import ingest
    def build(doc, pt, line):
        doc.modelspace().add_lwpolyline([(15000+2000*math.cos(i*.02),15000+2000*math.sin(i*.02))
                                       for i in range(400)], close=True)
    geometry, _, drawing = ingest(workspace, build)
    source = next(e for e in geometry.index.data["entities"] if e["kind"] == "LWPOLYLINE")
    s = AgentRunner(workspace, ChatRequest(snapshot_id=drawing["id"], question="读取几何",
        mode="agent",runtime="responses_subscription",model_profile="siwc_sol"))
    result = json.loads(s.invoke_tool("read_geometry", {"entity_ids":[source["entity_id"]]}))
    assert "error" not in result
    ref = result["full_geometry_record"]
    chunks, cursor = [], 0
    while True:
        page = read(s, EvidenceRecordRead(record_id=ref["record_id"], start=cursor, length=12000))
        chunks.append(page["content"])
        if not page["has_more"]:break
        cursor = page["next_start"]
    saved = json.loads("".join(chunks))["items"][0]
    assert len(saved["paths"][0]) > 64
    assert saved == geometry.records([source["entity_id"]])[0]


def test_stable_catalog_excludes_unneeded_families_and_only_appends(workspace, demo_bytes):
    from cad_agent.runtimes.stable_prefix import StableTools
    s = make_session(workspace, demo_bytes)
    tools = StableTools(s)
    original = list(tools.definitions)
    names = {t["function"]["name"] for t in original}
    assert {"read_source_context", "read_evidence_record", "plan_task", "finish_analysis"} <= names
    assert "review_space_connections" not in names
    assert tools.include_required(["read_source_context"]) == []
    assert tools.definitions == original
    assert tools.include_required(["render_region"]) == ["render_region"]
    assert tools.definitions[:-1] == original
    assert tools.include_required(["read_source_context"]) == []
    assert tools.definitions[-1]["function"]["name"] == "render_region"


def test_exploration_admission_failure_uses_reserved_finalization(workspace, demo_bytes, monkeypatch):
    s = make_session(workspace, demo_bytes)
    connected_lease(workspace, monkeypatch)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    payloads = []
    for module in ("budget_policy", "responses_subscription"):
        monkeypatch.setattr(f"cad_agent.runtimes.{module}.estimate_input",
                            lambda p: (50000, "test_bound"))

    def request(self, session, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            r = response([{"type": "function_call", "namespace": "cad",
                           "name": "read_source_context",
                           "arguments": json.dumps({"region_id": rid}), "call_id": "read"}])
            r["usage"].update(input_tokens=829900, output_tokens=100, total_tokens=830000)
        else:
            r = response([{"type": "message", "content": [{
                "type": "output_text", "text": final(next(iter(s.evidence))),
            }]}])
        self.record_usage(session, r)
        return r

    monkeypatch.setattr(ResponsesTransport, "request", request)
    result = s.run()
    assert result["citations"] and len(payloads) == 2
    assert payloads[-1]["tool_choice"] == "none"
    assert len(result["model_requests"]) == 2
    assert result["model_requests"][-1]["stop_reason"] == "finalization_request_reserve"
    rejection = s.runtime_diagnostics["admission_rejections"][0]
    assert rejection["sent"] is False
    assert rejection["budget_preflight"]["charged_before"] == 830000


def test_large_spatial_packet_keeps_exact_bounded_continuations(workspace, demo_bytes):
    s = make_session(workspace, demo_bytes)
    calls = [{"tool": "measure_spatial_area", "arguments": {
        "object_id": "source_object", "contour_id": f"contour_{i}",
    }} for i in range(12)]
    value = {
        "object_id": "source_object", "other_object_id": "other_object",
        "contour_id": "contour_0", "window_id": "source_window",
        "engineering_confirmed": False,
        "spatial_fact_scope": {"identity_confirmed": False, "engineering_confirmed": False},
        "geometry": [[i, i / 3] for i in range(10000)],
        "next_calls": calls,
        "review_calls": [{"tool": "read_geometry", "arguments": {"entity_ids": ["x"]}}],
        "subdivision_windows": [{"window_id": f"w{i}", "bbox": [0, 0, 1, 1]} for i in range(8)],
    }
    ref = save_session(s, "spatial_test", value)
    projected = working_result(value, ref)
    assert projected["result_excerpt"] and len(json.dumps(projected)) <= 18000
    for key in ("object_id", "other_object_id", "contour_id", "window_id", "spatial_fact_scope"):
        assert projected[key] == value[key]
    assert projected["engineering_confirmed"] is False
    assert projected["next_calls"] == calls[:3]
    assert projected["review_calls"] == value["review_calls"]
    assert projected["continuation_excerpt"]["next_calls"]["total"] == 12
    assert projected["continuation_excerpt"]["next_calls"]["has_more"] is True
    assert projected["continuation_excerpt"]["subdivision_windows"]["shown"] == 3
    archived = s.w.store.get("evidence_archive", ref["record_id"])
    assert json.loads(archived["text"]) == value
    assert not s.evidence and not s.observations
    huge = value | {"next_calls": [{"tool": "read_geometry", "arguments": {"entity_ids": ["x"] * 10000}}]}
    huge_ref = save_session(s, "spatial_test", huge)
    compact = working_result(huge, huge_ref)
    assert compact["next_calls"] == []
    assert compact["continuation_excerpt"]["next_calls"]["has_more"] is True
    assert compact["next_read"]["arguments"]["record_id"] == huge_ref["record_id"]
