import json
from copy import deepcopy

from cad_agent.analysis.error_feedback import build_feedback
from cad_agent.analysis.evidence_store import EvidenceRecordRead, read
from cad_agent.analysis.failure_guard import blocked, progress_signature
from test_agent_efficiency import runner


def test_error_packet_is_incremental_and_full_record_recovers(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    s.task.auto_plan()
    s.task.data["spatial_objects"] = {"object-a": {"records": [{"source": "原文" * 30000}]}}
    error = ValueError("specific missing dependency: source-a")
    first = build_feedback(s, "read_geometry", {}, {"error": {"code": "INVALID", "message": str(error)}},
                           error, ["q1"])
    second = build_feedback(s, "read_geometry", {}, {"error": {"code": "INVALID", "message": str(error)}},
                            error, ["q1"])
    assert len(json.dumps(first, ensure_ascii=False)) < 12000
    assert "records" not in first["repair_context"]
    assert first["repair_context"]["state_delta"]
    assert second["repair_context"]["state_unchanged"]
    ref = first["error_record"]
    content, start = "", 0
    while True:
        page = read(s, EvidenceRecordRead(record_id=ref["record_id"], start=start))
        content += page["content"]
        if not page["has_more"]:
            break
        start = page["next_start"]
    full = json.loads(content)
    assert full["task"]["spatial_objects"] == s.task.data["spatial_objects"]
    assert full["exception_message"] == str(error)


def test_argument_noise_does_not_count_progress_but_valid_fix_runs(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    for limit, count in [(-1, 1), (-2, 2), (-3, 3)]:
        result = json.loads(s.invoke_tool("search_sources", {"query": "内墙", "limit": limit}))
        assert result["retry"]["identical_failure_count"] == count
        assert result["repair_context"]["field_issues"][0]["path"] == "limit"
        assert s.policy.stop_reason is None
    result = json.loads(s.invoke_tool("search_sources", {"query": "内墙", "limit": 5}))
    assert "error" not in result and result["items"]


def test_repeat_blocked_but_source_progress_reopens_original_call(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    bad = {"query": "内墙", "limit": -1}
    first = json.loads(s.invoke_tool("search_sources", bad))
    s.invoke_tool("search_sources", bad)
    assert blocked(s, "search_sources", bad)
    # Reading diagnostic pages grants no new source facts.
    s.invoke_tool("read_evidence_record", {"record_id": first["error_record"]["record_id"]})
    assert blocked(s, "search_sources", bad)
    rid = s.w.regions(s.request.snapshot_id)[0].region_id
    assert "error" not in json.loads(s.invoke_tool("read_source_context", {"region_id": rid}))
    assert not blocked(s, "search_sources", bad)
    retry = json.loads(s.invoke_tool("search_sources", bad))
    assert retry["retry"]["identical_failure_count"] == 1


def test_repeated_failure_terminates_without_model_loop(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    bad = {"query": "内墙", "limit": -1}
    for _ in range(2):
        assert json.loads(s.invoke_tool("search_sources", bad))["error"]["code"] == "INVALID_TOOL_INPUT"
    assert json.loads(s.invoke_tool("search_sources", bad))["error"]["code"] == "REPEATED_TOOL_FAILURE"
    assert s.policy.stop_reason == "repeated_tool_failure"
    assert json.loads(s.invoke_tool("search_sources", bad))["error"]["code"] == "AGENT_TOOL_LIMIT"


def test_controls_and_observation_id_churn_are_not_progress(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    s.task.auto_plan()
    s.observations["one"] = dict(tool="measure_geometry", observation_id="one", data={"value": 5})
    before = progress_signature(s)
    s.observations["two"] = dict(tool="measure_geometry", observation_id="two", data={"value": 5})
    s.observations["control"] = dict(tool="request_toolset", data={"accepted": True})
    s.task.data["subtasks"][0]["calls"] += 1
    assert progress_signature(s) == before
    s.observations["three"] = dict(tool="measure_geometry", observation_id="three", data={"value": 6})
    assert progress_signature(s) != before


def test_different_targets_and_error_dependencies_remain_distinct(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    monkeypatch.setattr(s, "execute", lambda *a, **k: (_ for _ in ()).throw(ValueError("unit missing")))
    for target in ("a", "b"):
        result = json.loads(s.invoke_tool("read_geometry", {"entity_ids": [target]}))
        assert result["retry"]["identical_failure_count"] == 1
    monkeypatch.setattr(s, "execute", lambda *a, **k: (_ for _ in ()).throw(ValueError("scope missing")))
    result = json.loads(s.invoke_tool("read_geometry", {"entity_ids": ["a"]}))
    assert result["retry"]["identical_failure_count"] == 1
    assert not blocked(s, "read_geometry", {"entity_ids": ["a"]})


def test_other_requirement_can_continue_after_repeated_failure(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    s.task.auto_plan()
    other = deepcopy(s.task.data["subtasks"][0])
    other["id"] = "q2"
    s.task.data["subtasks"].append(other)
    for _ in range(3):
        result = json.loads(s.invoke_tool("search_sources", {"query": "内墙", "limit": -1, "task_ids": ["q1"]}))
    assert result["error"]["code"] == "REPEATED_TOOL_FAILURE"
    assert s.policy.stop_reason is None
    # The same arguments against another owner are not that owner's failure.
    other_error = json.loads(s.invoke_tool("search_sources", {"query": "内墙", "limit": -1, "task_ids": ["q2"]}))
    assert other_error["error"]["code"] == "INVALID_TOOL_INPUT"
    assert "error" not in json.loads(s.invoke_tool("search_sources", {"query": "内墙", "task_ids": ["q2"]}))


def test_legacy_error_path_and_disabled_batch_are_preserved(workspace, demo_bytes):
    s = runner(workspace, demo_bytes, "legacy")
    result = json.loads(s.invoke_tool("batch_read_sources", {"reads": []}))
    assert result["error"]["code"] == "SOURCE_READ_BATCHING_DISABLED"
    assert "error_record" not in result


def test_malformed_envelopes_keep_original_error_without_crashing(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    for args in ("{", "123", '{"task_ids": [{}], "query": "墙"}'):
        result = json.loads(s.invoke_tool("search_sources", args))
        assert result["error"]["code"] == "INVALID_TOOL_INPUT"
        assert result["error_record"]["record_id"].startswith("archive_")


def test_real_coverage_progress_resets_guard_but_duplicate_receipts_do_not(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    s.task.auto_plan()
    step = s.task.data["subtasks"][0]
    receipt = dict(operation="geometry", fulfilled=True, measurement_id="measurement-a")
    step["operation_receipts"] = [receipt]
    before = progress_signature(s)
    step["operation_receipts"].append(deepcopy(receipt))
    assert progress_signature(s) == before
    step["coverage"] = [dict(object="墙", field="材料", state="source_candidate", evidence_ids=["ev_a"])]
    assert progress_signature(s) != before
    before = progress_signature(s)
    s.policy.seen_results.add(("search_sources", "new-source", 0))
    assert progress_signature(s) != before
    before = progress_signature(s)
    s.sent_image_ids.add("actually-sent-image")
    assert progress_signature(s) != before


def test_small_wire_limit_keeps_full_diagnostic_reference(workspace, demo_bytes):
    s = runner(workspace, demo_bytes)
    s._tool_response_limit = 1000
    result = json.loads(s.invoke_tool("search_sources", {"query": "墙", "limit": -1}))
    assert result["error"]["code"] == "INVALID_TOOL_INPUT"
    assert result["error_record"]["record_id"] == result["next_read"]["arguments"]["record_id"]


def test_archive_excludes_execution_controls_without_touching_task(workspace, demo_bytes, monkeypatch):
    s = runner(workspace, demo_bytes)
    s.task.auto_plan()
    s.task.data.update(execution_token="test-control-token", execution_owner="test-owner")
    monkeypatch.setattr(s.task, "save", lambda: None)
    error = ValueError("missing source")
    result = build_feedback(s, "read_geometry", {}, {"error": {"code": "INVALID", "message": str(error)}},
                            error, ["q1"])
    stored = json.loads(s.w.store.get("evidence_archive", result["error_record"]["record_id"])["text"])
    assert not any(k.startswith("execution_") for k in stored["task"])
    assert s.task.data["execution_token"] == "test-control-token"
