import json
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient

from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.adapters.model import ModelGateway, ModelError
from cad_agent.runtimes.bridge import ModelBridge
from cad_agent.runtimes.external import ExternalRuntime
from test_model import mock_client


def test_model_catalog_is_allowlisted_and_secrets_stay_server_side(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "cad_agent.config.local_env",
        lambda: {
            "CAD_QWEN_LARGE_BASE_URL": "https://qwen.private/v1",
            "CAD_QWEN_LARGE_API_KEY": "qwen-private-key",
            "CAD_QWEN_LARGE_MODEL": "Qwen-test",
            "CAD_QWEN_LARGE_CONTEXT_LENGTH": "262144",
        },
    )
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.get("/api/models")
        assert response.status_code == 200
        data = response.json()
        assert data["default"] == "default" and len(data["items"]) == 8
        large = next(x for x in data["items"] if x["id"] == "qwen_large")
        assert large["model"] == "Qwen-test" and large["configured"]
        assert "qwen-private-key" not in response.text
        assert "https://qwen.private" not in response.text
        invalid = client.post(
            "/api/chat-runs",
            json={
                "snapshot_id": "x",
                "question": "x",
                "mode": "agent",
                "model_profile": "https://attacker.test",
            },
        )
        assert invalid.status_code == 422


def test_selection_is_per_session_and_fallback_keeps_model(
    workspace, demo_bytes, monkeypatch
):
    original = workspace.settings
    selected = replace(
        original,
        model_profile="qwen_large",
        provider="qwen",
        model="Qwen-test",
        api_key="qwen-key",
        api_base="https://qwen.test/v1",
        protocol="openai",
    )
    workspace.model_catalog["qwen_large"] = selected
    drawing = workspace.ingest("model.dxf", demo_bytes)
    request = ChatRequest(
        snapshot_id=drawing["id"],
        question="图名",
        mode="agent",
        model_profile="qwen_large",
        runtime="codex",
        allow_fallback=True,
    )
    agent = AgentRunner(workspace, request)
    other = AgentRunner(workspace, request.model_copy(update={"model_profile": None}))
    assert agent.w.settings is selected
    assert agent.w.gateway.settings is selected
    assert other.w is workspace and workspace.settings is original
    assert agent.w.store is workspace.store and agent.w.lock is workspace.lock

    class Broken:
        def run(self, session, history):
            raise ModelError("UNAVAILABLE", "fixture")

    monkeypatch.setattr(
        "cad_agent.runtimes.registry.get_runtime", lambda name: Broken()
    )

    def fallback(runtime, session, history):
        assert session.w.settings is selected
        return {"answer": "fixture", "model": selected.model}

    monkeypatch.setattr("cad_agent.runtimes.registry.NativeRuntime.run", fallback)
    assert agent.run()["model"] == "Qwen-test"


def test_qwen_normalizes_system_messages_and_preserves_tools(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path,
        api_base="https://qwen.test/v1",
        api_key="private-key",
        model="Qwen-test",
        provider="qwen",
        protocol="openai",
    )
    messages = [
        {"role": "system", "content": "first"},
        {"role": "system", "content": "second"},
        {"role": "user", "content": "question"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def handle(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == "Qwen-test"
        assert payload["messages"] == [
            {"role": "system", "content": "first\n\nsecond"},
            messages[-1],
        ]
        assert payload["tools"] == tools and payload["max_tokens"] == 6000
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert "thinking" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "answer"}}
                ],
                "usage": {"total_tokens": 8},
            },
        )

    mock_client(monkeypatch, handle)
    assert ModelGateway(settings).chat_turn(messages, tools)[1]["total_tokens"] == 8
    assert len(messages) == 3 and messages[0]["content"] == "first"


@pytest.mark.parametrize(
    "body,code",
    [
        (
            "requires --enable-auto-tool-choice and --tool-call-parser",
            "MODEL_TOOLS_UNSUPPORTED",
        ),
        ("maximum context length is 2048", "MODEL_CONTEXT_LIMIT"),
    ],
)
def test_qwen_capability_failures_do_not_expose_provider_body(
    tmp_path, monkeypatch, body, code
):
    settings = Settings(
        data_dir=tmp_path,
        api_base="https://qwen.test/v1",
        api_key="private-key",
        model="Qwen-test",
        provider="qwen",
        protocol="openai",
    )
    mock_client(
        monkeypatch,
        lambda r: httpx.Response(
            400, json={"error": {"message": body + " private-key"}}
        ),
    )
    with pytest.raises(ModelError) as error:
        ModelGateway(settings).chat_turn([], [])
    assert error.value.code == code and "private-key" not in error.value.message


def test_codex_config_uses_selected_model_not_deepseek(
    workspace, demo_bytes, tmp_path, monkeypatch
):
    workspace.settings = replace(workspace.settings, model="Qwen-test")
    drawing = workspace.ingest("qwen.dxf", demo_bytes)
    agent = AgentRunner(
        workspace, ChatRequest(snapshot_id=drawing["id"], question="图名")
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        "cad_agent.runtimes.external.shutil.which", lambda name: "/test/codex"
    )
    monkeypatch.setattr(
        "cad_agent.runtimes.external.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout="codex-cli 0.162.1"),
    )
    bridge = SimpleNamespace(
        session=agent, token="ephemeral", url="http://127.0.0.1:123/v1"
    )
    root = tmp_path / "harness"
    root.mkdir()
    ExternalRuntime("codex").codex_config(root, root, {}, bridge)
    config = (root / "codex-home/config.toml").read_text()
    assert 'model = "Qwen-test"' in config and "deepseek-flash" not in config


def test_matrix_does_not_count_missing_usage_as_zero_or_failure_speed_as_success(
    tmp_path,
):
    from scripts.compare_model_matrix import summarize

    directory = tmp_path / "qwen_small"
    directory.mkdir()
    for repeat in (1, 2):
        (directory / f"overview--native--{repeat}.json").write_text(
            json.dumps(
                {
                    "runtime_requested": "native",
                    "live_model": "Qwen-test",
                    "status": "failed",
                    "automated_passed": False,
                    "wall_seconds": 0.1,
                    "usage_reported": False,
                    "usage": [],
                    "error": {"code": "MODEL_TOOLS_UNSUPPORTED"},
                }
            )
        )
    row = summarize(tmp_path, ["qwen_small"])[0]
    assert row["runs"] == 2 and row["published"] == 0
    assert row["reported_total_tokens"] is None
    assert row["success_median_seconds"] is None
    assert row["all_attempts_median_seconds"] == 0.1


def test_connected_subscription_defaults_to_custom_runtime_profile(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    monkeypatch.setattr(app.state.workspace.siwc_account, 'status', lambda: {'connected': True})
    with TestClient(app) as client:
        catalog = client.get('/api/models').json()
        assert catalog['default'] == 'siwc_sol'
        selected = next(m for m in catalog['items'] if m['id'] == catalog['default'])
        assert selected['required_runtime'] == 'responses_subscription'
        assert selected['billing'] == 'subscription'
