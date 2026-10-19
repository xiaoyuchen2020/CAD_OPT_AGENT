from contextlib import contextmanager
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from cad_agent.adapters.model import ModelError
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.session import tool_definitions
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.subscription import CodexAccount, CodexSubscriptionRuntime, CONFIG
from test_agent import final


def session(workspace, demo_bytes, **kwargs):
    d = workspace.ingest("subscription-test.dxf", demo_bytes)
    return AgentRunner(workspace, ChatRequest(snapshot_id=d["id"], question="内墙材料？", mode="agent",
        runtime="codex_subscription", model_profile=kwargs.pop("model_profile", "codex_sol"), **kwargs))


def test_subscription_profiles_never_inherit_api_credentials(workspace):
    settings = replace(workspace.settings, api_key="private-key", api_base="https://private.test")
    for name in ("codex_sol_56", "codex_sol", "codex_astra"):
        profile = settings.model_catalog()[name]
        assert profile.api_key == profile.api_base == ""
        assert profile.provider == "codex_subscription"


def test_pairing_and_fallback_are_rejected_before_auth(workspace, demo_bytes, monkeypatch):
    monkeypatch.setattr(workspace.codex_account, "status", lambda: pytest.fail("must not request login"))
    with pytest.raises(ModelError, match="回退"):
        session(workspace, demo_bytes, allow_fallback=True).run()
    s = session(workspace, demo_bytes)
    s.request.runtime = "native"
    with pytest.raises(ModelError, match="订阅模型"):
        s.run()


def test_account_status_redacts_identity_and_tokens(tmp_path, monkeypatch):
    a = CodexAccount(tmp_path)
    rpc = Mock()
    rpc.pending = []
    rpc.request.return_value = {"account": {"type": "chatgpt", "planType": "plus", "email": "private@example.test", "accessToken": "private-token"}}
    monkeypatch.setattr(a, "client", lambda: rpc)
    monkeypatch.setattr("cad_agent.runtimes.subscription.shutil.which", lambda _: "/fake/codex")
    status = a.status()
    assert status["connected"] and status["plan_type"] == "plus"
    assert "private" not in json.dumps(status)
    assert not status["model_access_verified"]


def test_login_rejects_non_official_url_and_requires_user_auth(tmp_path, monkeypatch):
    a = CodexAccount(tmp_path)
    monkeypatch.setattr(a, "status", lambda: {"connected": False})
    rpc = Mock()
    rpc.request.return_value = {"authUrl": "https://attacker.test/", "loginId": "test"}
    monkeypatch.setattr(a, "client", lambda: rpc)
    with pytest.raises(ModelError, match="登录地址"):
        a.login()
    with pytest.raises(ModelError, match="登录"):
        with a.lease():
            pytest.fail("must wait for user login")


def test_only_one_subscription_run_and_no_logout_midrun(tmp_path, monkeypatch):
    a = CodexAccount(tmp_path)
    monkeypatch.setattr(a, "status", lambda: {"connected": True})
    with a.lease():
        with pytest.raises(ModelError, match="一个"):
            with a.lease():
                pass
        with pytest.raises(ValueError, match="等待"):
            a.logout()
    assert a.active_runs == 0


def test_switch_to_device_login_cancels_old_attempt_and_returns_only_user_code(tmp_path, monkeypatch):
    a = CodexAccount(tmp_path)
    a.pending_login = {"type": "chatgpt", "loginId": "old", "authUrl": "https://auth.openai.com/oauth/authorize"}
    monkeypatch.setattr(a, "status", lambda: {"connected": False})
    rpc = Mock()
    rpc.request.side_effect = [{}, {"type": "chatgptDeviceCode", "loginId": "new", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "TEST-CODE"}]
    monkeypatch.setattr(a, "client", lambda: rpc)
    result = a.login("device")
    assert result["flow"] == "device" and result["user_code"] == "TEST-CODE"
    assert "loginId" not in result
    assert rpc.request.call_args_list[0].args == ("account/login/cancel", {"loginId": "old"})


def test_failed_login_notification_clears_stale_attempt(tmp_path, monkeypatch):
    a = CodexAccount(tmp_path)
    a.pending_login = {"loginId": "failed"}
    rpc = Mock()
    rpc.pending = [{"method": "account/login/completed", "params": {"loginId": "failed", "success": False, "error": "private diagnostic"}}]
    rpc.request.return_value = {"account": None}
    monkeypatch.setattr(a, "client", lambda: rpc)
    monkeypatch.setattr("cad_agent.runtimes.subscription.shutil.which", lambda _: "/fake/codex")
    status = a.status()
    assert status["login_failed"] and not status["login_pending"]
    assert "private" not in json.dumps(status)


def test_usage_counts_deltas_once_and_new_threads_independently(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    totals = {}
    for tid, n in (("a", 100), ("a", 100), ("a", 150), ("b", 50)):
        CodexSubscriptionRuntime.usage(s, {"threadId": tid, "tokenUsage": {"total": {
            "totalTokens": n, "inputTokens": n - 10, "outputTokens": 10,
            "cachedInputTokens": 20, "reasoningOutputTokens": 5}}}, totals)
    assert sum(u["total_tokens"] for u in s.usage) == 200
    assert len(s.usage) == 3
    assert all(u["billing"] == "subscription" for u in s.usage)


class FakeRpc:
    def __init__(self, events):
        self.pending = []
        self.events = iter(events)
        self.sent = []
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "t"}, "model": params["model"]}
        if method == "turn/start":
            return {"turn": {"id": "u"}}
        return {}

    def receive(self):
        event = next(self.events)
        return event() if callable(event) else event

    def send(self, event):
        self.sent.append(event)

    def close(self):
        pass


@pytest.mark.parametrize("profile,model", [("codex_sol", "gpt-6.1-sol"), ("codex_sol_56", "gpt-5.6-sol")])
def test_subscription_tool_answer_uses_existing_evidence_validation(workspace, demo_bytes, monkeypatch, profile, model):
    s = session(workspace, demo_bytes, model_profile=profile)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    rpc = FakeRpc([
        {"id": 99, "method": "item/tool/call", "params": {"threadId": "t", "tool": "read_source_context", "arguments": {"region_id": rid}}},
        lambda: {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": final(next(iter(s.evidence)))}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ])
    @contextmanager
    def lease():
        yield
    monkeypatch.setattr(workspace.codex_account, "lease", lease)
    monkeypatch.setattr(workspace.codex_account, "launch", lambda *args: rpc)
    result = s.run()
    assert result["billing"] == "codex_subscription" and result["model"] == model
    assert result["citations"][0]["source_parts"]
    assert result["runtime_used"] == "codex_subscription"
    assert not result["fallback_used"]
    thread = rpc.requests[0][1]
    assert {t['name'] for t in thread['dynamicTools']} == {t['function']['name'] for t in s.offered_tools()}
    assert 'render_region' not in {t['name'] for t in thread['dynamicTools']}
    assert thread["approvalPolicy"] == "never"
    assert 'shell_tool = false' in CONFIG and 'multi_agent = false' in CONFIG


def test_unknown_tool_is_rejected_without_execution(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    rpc = FakeRpc([{"id": 1, "method": "item/tool/call", "params": {"tool": "exec", "arguments": {}}}])
    with pytest.raises(ModelError, match="未授权"):
        CodexSubscriptionRuntime().turn(rpc, s, "t", "question", {})
    assert not s.trace and rpc.sent[0]["error"]["code"] == -32601


def test_stop_interrupts_exploration_for_separate_synthesis(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    s.policy.stop_reason = "no_new_information"
    rpc = FakeRpc([
        {"method": "item/started", "params": {}},
        {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
    ])
    assert CodexSubscriptionRuntime().turn(rpc, s, "t", "question", {}) is None
    assert rpc.requests[-1][0] == "turn/interrupt"


def usage_event(tokens, thread="t"):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread, "tokenUsage": {"total": {
            "totalTokens": tokens, "inputTokens": tokens - 100,
            "outputTokens": 100, "cachedInputTokens": 0,
            "reasoningOutputTokens": 0}}}}


def test_budget_reserves_answer_after_read_and_rejects_late_tools(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    rpc = FakeRpc([
        usage_event(32000),  # Search cost alone does not claim evidence sufficiency.
        {"id": 1, "method": "item/tool/call", "params": {
            "tool": "read_source_context", "arguments": {"region_id": rid}}},
        {"id": 2, "method": "item/tool/call", "params": {
            "tool": "search_sources", "arguments": {"keywords": ["墙"]}}},
        {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
    ])
    runtime = CodexSubscriptionRuntime()
    assert runtime.turn(rpc, s, "t", "question", {}) is None
    assert len(s.trace) == 1 and s.evidence
    assert s.policy.stop_reason == "subscription_answer_reserve"
    assert len([r for r in rpc.requests if r[0] == "turn/interrupt"]) == 1
    assert "EXPLORATION_STOPPED" in rpc.sent[-1]["result"]["contentItems"][0]["text"]
    assert runtime.source_contexts[0]["target_region_id"] == rid


def test_budget_without_evidence_stops_at_exploration_ceiling(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    rpc = FakeRpc([usage_event(41000),
        {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}}])
    assert CodexSubscriptionRuntime().turn(rpc, s, "t", "question", {}) is None
    assert not s.evidence and s.policy.stop_reason == "subscription_exploration_budget"


@pytest.mark.parametrize("usage_first", [False, True])
def test_final_answer_survives_usage_above_old_limit(workspace, demo_bytes, usage_first):
    s = session(workspace, demo_bytes)
    message = {"method": "item/completed", "params": {
        "item": {"type": "agentMessage", "text": "final-json"}}}
    usage = usage_event(60122)
    events = [usage, message] if usage_first else [message, usage]
    rpc = FakeRpc(events + [
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}])
    assert CodexSubscriptionRuntime().turn(rpc, s, "t", "question", {}, final=True) == "final-json"
    assert sum(u["total_tokens"] for u in s.usage) == 60122
    assert not any(m == "turn/interrupt" for m, _ in rpc.requests)


def test_completed_exploration_answer_is_not_discarded_by_late_usage(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    rpc = FakeRpc([
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "answer"}}},
        usage_event(60122),
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ])
    assert CodexSubscriptionRuntime().turn(rpc, s, "t", "question", {}) == "answer"
    assert not any(m == "turn/interrupt" for m, _ in rpc.requests)


def test_budget_handoff_runs_only_one_final_turn_with_original_evidence(workspace, demo_bytes, monkeypatch):
    s = session(workspace, demo_bytes)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    rpc = FakeRpc([
        usage_event(32000),
        {"id": 1, "method": "item/tool/call", "params": {
            "tool": "read_source_context", "arguments": {"region_id": rid}}},
        {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
        usage_event(60122),
        lambda: {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": final(next(iter(s.evidence)))}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ])
    @contextmanager
    def lease():
        yield
    monkeypatch.setattr(workspace.codex_account, "lease", lease)
    monkeypatch.setattr(workspace.codex_account, "launch", lambda *args: rpc)
    result = s.run()
    threads = [p for m, p in rpc.requests if m == "thread/start"]
    turns = [p for m, p in rpc.requests if m == "turn/start"]
    assert len(threads) == len(turns) == 2 and threads[-1]["dynamicTools"] == []
    payload = json.loads(turns[-1]["input"][0]["text"])
    assert payload["source_contexts"][0]["target_region_id"] == rid
    assert {e["evidence_id"] for e in payload["evidence"]} == set(s.evidence)
    assert result["citations"] and not result["fallback_used"]
    assert result["subscription_budget"]["soft_limit_exceeded"]
    assert result["efficiency"]["stop_reason"] == "subscription_answer_reserve"


def test_final_turn_still_rejects_tools(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    rpc = FakeRpc([{"id": 1, "method": "item/tool/call", "params": {
        "tool": "search_sources", "arguments": {"keywords": ["墙"]}}}])
    with pytest.raises(ModelError, match="未授权"):
        CodexSubscriptionRuntime().turn(rpc, s, "t", "question", {}, final=True)
    assert not s.trace


def test_account_routes_and_models_do_not_expose_credentials(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    account = app.state.workspace.codex_account
    monkeypatch.setattr(account, "status", lambda: {"connected": False})
    monkeypatch.setattr(account, "login", lambda flow: {"auth_url": "https://auth.openai.com/example"})
    monkeypatch.setattr(account, "logout", lambda: {"connected": False})
    with TestClient(app) as client:
        assert client.get("/api/codex-account").json() == {"connected": False}
        assert client.post("/api/codex-account/login").status_code == 200
        assert client.post("/api/codex-account/logout").status_code == 200
        profiles = [m for m in client.get("/api/models").json()["items"] if m["billing"] == "subscription"]
        assert len(profiles) == 4 and all(m["required_runtime"] in ("codex_subscription", "responses_subscription") for m in profiles)
        assert client.post("/api/codex-account/login", headers={"origin": "https://attacker.test"}).status_code == 403


def test_prepared_image_grace_is_bounded_and_respects_total_budget(workspace, demo_bytes):
    s=session(workspace,demo_bytes);runtime=CodexSubscriptionRuntime()
    s.visual_images={'im':{}};s.reviewed_image_ids=set()
    s.usage=[{'total_tokens':runtime.exploration_budget(s)+1}]
    s.trace=[{'tool':'prepare_space_connections','status':'completed'}]
    assert runtime.stop_reason(s) is None
    s.trace.append({'tool':'review_space_connections','status':'failed'})
    assert runtime.stop_reason(s) is None
    s.trace.append({'tool':'review_space_connections','status':'failed'})
    assert runtime.stop_reason(s)=='subscription_exploration_budget'
    s.trace=s.trace[:1];s.usage=[{'total_tokens':runtime.total_budget(s)}]
    assert runtime.stop_reason(s)=='subscription_exploration_budget'
