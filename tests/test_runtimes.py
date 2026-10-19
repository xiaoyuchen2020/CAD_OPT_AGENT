import json
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.analysis.agent import AgentRunner, SYSTEM, tool_definitions
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.bridge import ModelBridge, response_messages, response_events
from cad_agent.runtimes.messages import to_chat, events as message_events
from cad_agent.runtimes.registry import run_agent
from cad_agent.runtimes.external import ExternalRuntime


def session(workspace, demo_bytes, **kwargs):
    drawing = workspace.ingest("runtime-test.dxf", demo_bytes)
    return AgentRunner(
        workspace,
        ChatRequest(
            snapshot_id=drawing["id"], question="这是什么图", mode="agent", **kwargs
        ),
    )


def test_native_is_default_and_does_not_launch_process(
    workspace, demo_bytes, monkeypatch
):
    s = session(workspace, demo_bytes)
    monkeypatch.setattr(
        "cad_agent.runtimes.registry.NativeRuntime.run",
        lambda runtime, context, history: {"answer": "verified fixture"},
    )
    result = s.run()
    assert result["runtime_used"] == "native" and not result["fallback_used"]


def test_failed_external_does_not_silently_fallback(workspace, demo_bytes, monkeypatch):
    s = session(workspace, demo_bytes, runtime="codex")
    fake = Mock()
    fake.run.side_effect = ModelError("UNAVAILABLE", "unavailable")
    monkeypatch.setattr("cad_agent.runtimes.registry.get_runtime", lambda name: fake)
    with pytest.raises(ModelError) as e:
        s.run()
    assert e.value.code == "UNAVAILABLE" and fake.run.call_count == 1


def test_explicit_fallback_preserves_task_and_records_attempt(
    workspace, demo_bytes, monkeypatch
):
    s = session(workspace, demo_bytes, runtime="codex", allow_fallback=True)

    class Broken:
        def run(self, context, history):
            context.invoke_tool("get_drawing_overview", {})
            raise ModelError("TEST_FAILURE", "failed")

    monkeypatch.setattr(
        "cad_agent.runtimes.registry.get_runtime", lambda name: Broken()
    )

    def fallback(runtime, context, history):
        assert context is s and context.task is s.task and context.calls == 1
        assert context.request.runtime == "native"
        assert not context.policy.cache and not context.policy.seen_evidence
        return {"answer": "verified fixture"}

    monkeypatch.setattr("cad_agent.runtimes.registry.NativeRuntime.run", fallback)
    result = s.run()
    assert result["runtime_requested"] == "codex"
    assert result["runtime_used"] == "native" and result["fallback_used"]
    assert result["runtime_attempts"][0]["error_code"] == "TEST_FAILURE"
    assert len(result["runtime_attempts"][0]["trace"]) == 1


def test_bridge_filters_native_tools_and_rejects_forbidden_model_calls(
    workspace, demo_bytes, monkeypatch
):
    s = session(workspace, demo_bytes)
    workspace.settings = replace(workspace.settings, model="deepseek-flash")
    bridge = ModelBridge(s)
    called = []

    def chat(messages, tools, timeout):
        called.extend(t["function"]["name"] for t in tools)
        return {
            "tool_calls": [
                {
                    "id": "x",
                    "function": {
                        "name": "exec_command",
                        "arguments": '{"cmd":"touch bad"}',
                    },
                }
            ]
        }, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", chat)
    try:
        with pytest.raises(ModelError) as e:
            bridge.complete(
                {
                    "model": "deepseek-flash",
                    "messages": [],
                    "tools": tool_definitions()
                    + [{"type": "function", "function": {"name": "exec_command"}}],
                }
            )
        assert e.value.code == "TOOL_NOT_ALLOWED"
        assert set(called) == {t["function"]["name"] for t in s.offered_tools()}
        assert "render_region" not in called
        assert not s.trace
    finally:
        bridge.server.server_close()


def test_bridge_rejects_different_model_and_enforces_final_budget(
    workspace, demo_bytes, monkeypatch
):
    s = session(workspace, demo_bytes)
    workspace.settings = replace(
        workspace.settings, model="deepseek-flash", agent_max_rounds=2
    )
    bridge = ModelBridge(s)

    def chat(messages, tools, timeout):
        assert not tools and "探索预算" in messages[-1]["content"]
        return {"content": "{}"}, {}

    monkeypatch.setattr(workspace.gateway, "chat_turn", chat)
    try:
        with pytest.raises(ModelError, match="模型"):
            bridge.complete({"model": "other", "messages": []})
        bridge.requests.append({})
        bridge.complete(
            {"model": "deepseek-flash", "messages": [], "tools": tool_definitions()}
        )
        assert bridge.requests[-1]["final_only"]
        bridge.requests.extend([{}])
        with pytest.raises(ModelError) as e:
            bridge.complete({"model": "deepseek-flash", "messages": []})
        assert e.value.code == "AGENT_ROUND_LIMIT"
    finally:
        bridge.server.server_close()


def test_tool_dispatch_is_bounded_and_rejects_foreign_scope(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    workspace.settings = replace(workspace.settings, agent_max_tool_calls=2)
    for name, args in [
        ("exec_command", {"cmd": "bad"}),
        ("search_sources", {"snapshot_id": "foreign"}),
    ]:
        assert (
            json.loads(s.invoke_tool(name, args))["error"]["code"]
            == "INVALID_TOOL_INPUT"
        )
    assert (
        json.loads(s.invoke_tool("get_drawing_overview", {}))["error"]["code"]
        == "AGENT_TOOL_LIMIT"
    )
    assert len(s.trace) == 2 and not s.evidence


def test_responses_roundtrip_parallel_calls_and_unicode():
    calls = [
        {
            "id": str(i),
            "type": "function",
            "function": {
                "name": "search_sources",
                "arguments": json.dumps({"query": word}, ensure_ascii=False),
            },
        }
        for i, word in enumerate(["住宅", "公建"])
    ]
    response, stream = response_events(
        {"tool_calls": calls},
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "deepseek-flash",
    )
    payload = {
        "instructions": SYSTEM,
        "input": response["output"]
        + [
            {"type": "function_call_output", "call_id": "0", "output": "原文一"},
            {
                "type": "function_call_output",
                "call_id": "1",
                "output": [{"type": "input_text", "text": "原文二"}],
            },
        ],
    }
    result = response_messages(payload)
    assert result[0]["content"] == SYSTEM
    assert result[1]["tool_calls"] == calls
    assert result[-1]["content"] == "原文二"
    assert stream[-1]["type"] == "response.completed"
    assert response["usage"]["total_tokens"] == 15


def test_messages_roundtrip_tools_and_final_text():
    message = {
        "tool_calls": [
            {
                "id": "one",
                "function": {"name": "get_drawing_overview", "arguments": "{}"},
            }
        ]
    }
    response, stream = message_events(
        message, {"prompt_tokens": 3, "completion_tokens": 2}, "deepseek-flash"
    )
    result = to_chat(
        {
            "system": SYSTEM,
            "messages": [
                {"role": "assistant", "content": response["content"]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "one",
                            "content": [{"type": "text", "text": "标题原文"}],
                        }
                    ],
                },
            ],
        }
    )
    assert result[1]["tool_calls"][0]["function"]["name"] == "get_drawing_overview"
    assert result[2]["role"] == "tool" and result[2]["content"] == "标题原文"
    assert stream[-1]["type"] == "message_stop"
    response, stream = message_events({"content": "中文答案"}, {}, "deepseek-flash")
    assert (
        response["stop_reason"] == "end_turn"
        and response["content"][0]["text"] == "中文答案"
    )


def test_transports_reject_multimodal_inputs():
    with pytest.raises(ValueError):
        response_messages(
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "file:///secret"}
                        ],
                    }
                ]
            }
        )
    with pytest.raises(ValueError):
        to_chat(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image", "source": {}}]}
                ]
            }
        )


def test_dsh_config_disables_execution_tools_and_uses_only_bridge_credential(
    workspace, demo_bytes, tmp_path
):
    s = session(workspace, demo_bytes)
    bridge = ModelBridge(s)
    root = tmp_path / "run"
    root.mkdir()
    cwd = root / "workspace"
    cwd.mkdir()
    try:
        argv, env = ExternalRuntime("deepseek_harness").dsh_config(
            root, cwd, {}, bridge
        )
        rows = json.loads((root / "cad.patch.yml").read_text())
        assert {
            "persistent-bash",
            "persistent-pwsh",
            "terminal-bash",
            "terminal-pwsh",
            "pty",
        } <= {r["id"] for r in rows if r.get("disabled")}
        assert "DEEPSEEK_API_KEY" not in env
        assert env["CAD_BRIDGE_TOKEN"] == bridge.token
        assert "--profile" in argv and "sdk-minimal" in argv
    finally:
        bridge.server.server_close()


def test_responses_commentary_and_parallel_calls_remain_one_assistant_turn():
    payload = {
        "input": [
            {
                "type": "function_call",
                "call_id": "a",
                "name": "search_sources",
                "arguments": "{}",
            },
            {
                "type": "function_call",
                "call_id": "b",
                "name": "query_facts",
                "arguments": "{}",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "正在查阅。"}],
            },
            {"type": "function_call_output", "call_id": "a", "output": "原文"},
            {"type": "function_call_output", "call_id": "b", "output": "事实"},
        ]
    }
    messages = response_messages(payload)
    assert [m["role"] for m in messages] == ["assistant", "tool", "tool"]
    assert messages[0]["content"] == "正在查阅。"
    assert [c["id"] for c in messages[0]["tool_calls"]] == ["a", "b"]


def test_runtime_api_and_request_validation(tmp_path):
    from fastapi.testclient import TestClient
    from pydantic import ValidationError
    from cad_agent.app import create_app
    from cad_agent.config import Settings

    with TestClient(create_app(Settings(data_dir=tmp_path / "api"))) as client:
        response = client.get("/api/runtimes")
        assert response.status_code == 200
        result = response.json()
        assert (
            result["default"] == "native" and result["code_execution_enabled"] is False
        )
        assert {r["id"] for r in result["items"]} == {
            "native",
            "deepseek_harness",
            "codex",
            "codex_subscription",
            "responses_subscription",
        }
        assert "api_key" not in json.dumps(result)
    with pytest.raises(ValidationError):
        ChatRequest(snapshot_id="x", question="test", runtime="shell")


def test_loopback_bridge_authentication_and_cad_only_dispatch(workspace, demo_bytes):
    s = session(workspace, demo_bytes)
    with ModelBridge(s) as bridge:
        with httpx.Client(trust_env=False) as client:
            url = bridge.url + "/cad/tool"
            assert (
                client.post(
                    url, json={"name": "get_drawing_overview", "arguments": {}}
                ).status_code
                == 401
            )
            headers = {"Authorization": "Bearer " + bridge.token}
            result = client.post(
                url,
                headers=headers,
                json={"name": "exec_command", "arguments": {"command": "bad"}},
            )
            assert result.json()["error"]["code"] == "INVALID_TOOL_INPUT"
            result = client.post(
                url,
                headers=headers,
                json={"name": "get_drawing_overview", "arguments": {}},
            )
            assert result.status_code == 200 and result.json()["items"]
            assert (
                client.post(
                    bridge.url + "/arbitrary-proxy", headers=headers, json={}
                ).status_code
                == 404
            )
        assert len(s.trace) == 2
