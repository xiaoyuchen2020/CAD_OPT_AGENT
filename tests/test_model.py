import json
import httpx
import pytest
from cad_agent.adapters.model import ModelGateway, ModelError
from cad_agent.config import Settings
from conftest import region


def gateway(tmp_path, protocol="responses"):
    return ModelGateway(
        Settings(
            data_dir=tmp_path,
            api_key="private-test-key",
            api_base="https://provider.test",
            model="gpt-6-astra",
            protocol=protocol,
        )
    )


def mock_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize("protocol", ["responses", "anthropic", "openai"])
def test_protocol_payload_and_json_validation(tmp_path, monkeypatch, protocol):
    r = region("地上内墙采用混凝土实心砖。")

    def handle(request):
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-6-astra"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        text = json.dumps({"items": [{"region_id": "r1", "quote": r.text}]})
        if protocol == "responses":
            assert request.url.path == "/v1/responses"
            assert payload["store"] is False
            assert payload["max_output_tokens"] == 6000
            body = {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    },
                ],
            }
        elif protocol == "anthropic":
            assert request.url.path == "/v1/messages"
            body = {"content": [{"type": "text", "text": text}]}
        else:
            assert request.url.path == "/v1/chat/completions"
            body = {"choices": [{"message": {"content": text}}]}
        return httpx.Response(200, json=body)

    mock_client(monkeypatch, handle)
    proposals, _ = gateway(tmp_path, protocol).propose([r])
    assert proposals[0]["quote"] == r.text


@pytest.mark.parametrize(
    "status,code",
    [
        (404, "MODEL_HTTP_ERROR"),
        (500, "MODEL_SERVICE_BUSY"),
        (429, "MODEL_SERVICE_BUSY"),
    ],
)
def test_errors_never_echo_provider_secrets(tmp_path, monkeypatch, status, code):
    mock_client(
        monkeypatch,
        lambda r: httpx.Response(status, json={"error": "private-test-key"}),
    )
    with pytest.raises(ModelError) as e:
        gateway(tmp_path).propose([region("hello")])
    assert e.value.code == code
    assert "private-test-key" not in str(e.value)


@pytest.mark.parametrize(
    "body",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "output": None},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "not JSON"}],
                }
            ],
        },
    ],
)
def test_invalid_or_incomplete_response_is_not_success(tmp_path, monkeypatch, body):
    mock_client(monkeypatch, lambda r: httpx.Response(200, json=body))
    with pytest.raises(ModelError) as e:
        gateway(tmp_path).propose([region("hello")])
    assert e.value.code == "MODEL_INVALID_RESPONSE"


def test_unconfigured_model_fails_without_network(tmp_path):
    with pytest.raises(ModelError) as e:
        ModelGateway(Settings(data_dir=tmp_path)).propose([])
    assert e.value.code == "MODEL_NOT_CONFIGURED"
