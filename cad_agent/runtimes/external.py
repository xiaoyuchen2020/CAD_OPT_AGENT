"""Real harness processes, sharing the CAD tools, gateway and final validator."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from contextlib import nullcontext
from uuid import uuid4

from pydantic import ValidationError
from cad_agent.adapters.model import ModelError
from cad_agent.analysis.session import SYSTEM, tool_definitions, validation_feedback
from cad_agent.config import ROOT
from cad_agent.runtimes.bridge import ModelBridge
from cad_agent.runtimes.rpc import RpcProcess

DSH_VERSION = "0.2.0-rc.2"
CODEX_VERSION = "0.162.1"


def runtime_status():
    dsh = ROOT / ".harness/node_modules/@deepseek-ai/dsh/package.json"
    return [
        {"id": "native", "available": True, "label": "自定义 · API"},
        {"id": "responses_subscription", "available": True, "label": "自定义 · 订阅直连"},
        {"id": "codex_subscription", "available": bool(shutil.which("codex")),
         "label": "Codex · 订阅直连", "required_version": CODEX_VERSION},
        {
            "id": "deepseek_harness",
            "available": dsh.exists()
            and json.loads(dsh.read_text()).get("version") == DSH_VERSION,
            "label": "DeepSeek · 桥接",
            "version": DSH_VERSION,
        },
        {
            "id": "codex",
            "available": bool(shutil.which("codex")),
            "label": "Codex · 桥接",
            "required_version": CODEX_VERSION,
        },
    ]


class ExternalRuntime:
    def __init__(self, name):
        self.name = name

    def run(self, session, history=None):
        settings = session.w.settings
        siwc = settings.provider == "responses_subscription"
        if siwc and (settings.model != "gpt-6.1-sol" or session.request.allow_fallback):
            raise ModelError("HARNESS_MODEL_UNSUPPORTED", "订阅桥接仅支持 GPT-6.1 Sol，且禁止回退。")
        if not siwc and not session.w.gateway.status()["configured"]:
            raise ModelError("MODEL_NOT_CONFIGURED", "请先配置 DeepSeek API。")
        if (
            not siwc and (settings.provider not in ("deepseek", "qwen")
            or settings.protocol != "openai"
            )
        ):
            raise ModelError(
                "HARNESS_MODEL_UNSUPPORTED",
                "试验运行时需要已配置的 DeepSeek 或 Qwen Chat Completions 服务。",
            )
        if not next(x for x in runtime_status() if x["id"] == self.name)["available"]:
            raise ModelError(
                "HARNESS_NOT_INSTALLED", "固定版本 Harness 尚未安装；可选择原生运行时。"
            )
        root = settings.data_dir / "harness-runs" / uuid4().hex
        root.mkdir(parents=True, mode=0o700)
        cwd = root / "workspace"
        cwd.mkdir()
        # No inherited provider credentials, shell hooks, or user harness config.
        env = {
            k: os.environ[k]
            for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
            if k in os.environ
        }
        env["TMPDIR"] = str(root.resolve())
        messages = session.build_messages(history)
        prompt = messages[-1]["content"]
        if history:
            prompt = json.dumps(
                {"history": history, "current_request": json.loads(prompt)},
                ensure_ascii=False,
            )
        rpc = None
        with (session.w.siwc_account.lease() if siwc else nullcontext()), ModelBridge(
            session, registered_tools=session.offered_tools()
        ) as bridge:
            try:
                if self.name == "codex":
                    argv, env = self.codex_config(root, cwd, env, bridge)
                else:
                    argv, env = self.dsh_config(root, cwd, env, bridge)
                rpc = RpcProcess(argv, env, str(cwd), session._remaining)
                if self.name == "codex":
                    rpc.request(
                        "initialize",
                        {
                            "clientInfo": {"name": "cad_opt_agent", "version": "0.1.0"},
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                    rpc.send({"jsonrpc": "2.0", "method": "initialized"})
                    thread = rpc.request(
                        "thread/start",
                        {
                            "model": settings.model,
                            "modelProvider": "cad_bridge",
                            "cwd": str(cwd.resolve()),
                            "sandbox": "read-only",
                            "approvalPolicy": "never",
                            "baseInstructions": session.system_prompt,
                            "developerInstructions": "",
                            "ephemeral": True,
                            "environments": [],
                            "dynamicTools": [
                                {
                                    "type": "function",
                                    "name": t["function"]["name"],
                                    "description": t["function"]["description"],
                                    "inputSchema": t["function"]["parameters"],
                                }
                                for t in session.offered_tools()
                            ],
                        },
                    )
                    sid = thread["thread"]["id"]
                else:
                    rpc.request(
                        "initialize",
                        {
                            "cwd": str(cwd.resolve()),
                            "provider": "deepseek-official",
                            "model": settings.model,
                            "maxTokens": 6000,
                        },
                    )
                    sid = "cad-" + uuid4().hex
                for attempt in range(2):
                    text = self.turn(rpc, sid, prompt, session, bridge)
                    if bridge.error:
                        raise bridge.error
                    try:
                        result = session.finish(text)
                        return result | ({"billing": "siwc_subscription", "model_access_verified": True,
                            "model_requests": bridge.requests} if siwc else {}) | {
                            "harness_version": (
                                CODEX_VERSION if self.name == "codex" else DSH_VERSION
                            ),
                            "model_bridge": bridge.requests,
                            "transport": (
                                "harness_to_siwc_responses" if siwc else
                                ("responses_to_chat" if self.name == "codex" else "messages_to_chat")
                            ),
                        }
                    except (ValueError, ValidationError) as error:
                        session.record_validation_issue(error)
                        if attempt or (siwc and not session.can_repair_answer(bridge.subscription.controls)):
                            raise ModelError(
                                "ANSWER_VALIDATION_FAILED",
                                "Harness 答案未通过统一证据校验，未发布。",
                            ) from None
                        issue = validation_feedback(error)
                        session.answer_repairs += 1
                        if session.policy.enabled:
                            session.policy.stop_reason = "answer_repair"
                        session.progress(
                            stage="答案引用校验未通过，正在纠正", validation_issue=issue
                        )
                        prompt = (
                            "答案未通过校验："
                            + issue
                            + "。请修正后仅返回最终 JSON，非 unknown 结论引用已读取证据。"
                        )
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ModelError(
                    "HARNESS_START_FAILED",
                    "Harness 配置或协议不兼容；可切换原生运行时。",
                ) from None
            finally:
                # Diagnostic files contain only local runtime diagnostics, not API keys.
                if rpc:
                    rpc.close()
                    diagnostics = "".join(rpc.stderr).replace(
                        bridge.token, "[REDACTED]"
                    )
                    (root / "diagnostics.txt").write_text(diagnostics[-16000:])
                session.runtime_diagnostics = {
                    "runtime": self.name,
                    "model_bridge": bridge.requests,
                    "diagnostic_dir": str(root),
                }
                (root / "bridge.json").write_text(
                    json.dumps(bridge.requests, ensure_ascii=False, indent=2)
                )

    def codex_config(self, root, cwd, env, bridge):
        binary = shutil.which("codex")
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if version != "codex-cli " + CODEX_VERSION:
            raise ModelError(
                "HARNESS_VERSION_MISMATCH", "Codex 版本与已验证的适配版本不一致。"
            )
        home = root / "codex-home"
        home.mkdir()
        env.update(CODEX_HOME=str(home.resolve()), CAD_BRIDGE_TOKEN=bridge.token)
        config = f"""model = {json.dumps(bridge.session.w.settings.model)}
model_provider = "cad_bridge"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[features]
shell_tool = false
unified_exec = false
apply_patch_freeform = false
multi_agent = false
js_repl = false
code_mode = false
code_mode_host = false
code_mode_only = false
code_mode_tool_search = false
[model_providers.cad_bridge]
name = "CAD local model bridge"
base_url = "{bridge.url}"
wire_api = "responses"
env_key = "CAD_BRIDGE_TOKEN"
request_max_retries = 0
stream_max_retries = 0
supports_websockets = false
[analytics]
enabled = false
"""
        (home / "config.toml").write_text(config)
        return [binary, "app-server", "--listen", "stdio://"], env

    def dsh_config(self, root, cwd, env, bridge):
        binary = ROOT / ".harness/node_modules/@deepseek-ai/dsh/lib/bin.js"
        schema = root / "tools.json"
        schema.write_text(json.dumps(bridge.session.offered_tools(), ensure_ascii=False))
        patch = root / "cad.patch.yml"
        disabled = [
            "persistent-bash",
            "persistent-pwsh",
            "terminal-bash",
            "terminal-pwsh",
            "pty",
        ]
        rows = [{"id": i, "disabled": True} for i in disabled]
        rows += [
            {
                "id": "llm-deepseek",
                "config": {
                    "apiKeyEnv": "CAD_BRIDGE_TOKEN",
                    "baseURL": bridge.url,
                    "thinking": "disabled",
                    "maxTokens": 6000,
                },
            },
            {
                "id": "system-prompt",
                "config": {
                    "includeHarnessIdentity": False,
                    "includeRuntimeContext": False,
                    "personaPrefix": bridge.session.system_prompt,
                },
            },
            {
                "insert": [
                    {
                        "id": "cad-tools",
                        "name": str(
                            (ROOT / "cad_agent/runtimes/dsh_tools.mjs").resolve()
                        ),
                        "inject": ["tools"],
                    }
                ]
            },
        ]
        # JSON is a YAML subset; no generated JavaScript in configuration.
        patch.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        env.update(
            DSH_HOME=str((root / "dsh-home").resolve()),
            CAD_BRIDGE_TOKEN=bridge.token,
            CAD_BRIDGE_URL=bridge.url,
            CAD_TOOL_SCHEMA_FILE=str(schema.resolve()),
        )
        return [
            shutil.which("node"),
            str(binary),
            "--profile",
            "sdk-minimal",
            "--patch",
            str(patch.resolve()),
        ], env

    def turn(self, rpc, sid, prompt, session, bridge):
        text = ""
        completed = False
        if self.name == "codex":
            rpc.request(
                "turn/start",
                {"threadId": sid, "input": [{"type": "text", "text": prompt}]},
            )
        else:
            rpc.request(
                "session/prompt",
                {"sessionId": sid, "contentBlocks": [{"type": "text", "text": prompt}]},
            )
        for event in rpc.events():
            session._remaining()
            if bridge.error:
                raise bridge.error
            method = event.get("method")
            params = event.get("params") or {}
            if "id" in event and method:
                if self.name == "codex" and method == "item/tool/call":
                    value = session.invoke_tool(params["tool"], params["arguments"])
                    rpc.send(
                        {
                            "jsonrpc": "2.0",
                            "id": event["id"],
                            "result": {
                                "contentItems": [{"type": "inputText", "text": value}],
                                "success": "error" not in json.loads(value),
                            },
                        }
                    )
                else:
                    rpc.send(
                        {
                            "jsonrpc": "2.0",
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "Only CAD function tools authorized",
                            },
                        }
                    )
                    raise ModelError(
                        "HARNESS_PERMISSION_DENIED",
                        "Harness 请求了 CAD 工具以外的操作，已拒绝。",
                    )
            if self.name == "codex":
                if (
                    method == "item/completed"
                    and params.get("item", {}).get("type") == "agentMessage"
                ):
                    text = params["item"].get("text", "")
                if method == "turn/completed":
                    if params["turn"]["status"] != "completed":
                        raise ModelError("HARNESS_TURN_FAILED", "Codex 回合未完成。")
                    return text
            else:
                if params.get("sessionId") != sid:
                    continue
                if method == "session.event":
                    item = params["event"]
                    if item.get("type") == "assistant/message":
                        text = "".join(
                            x.get("text", "")
                            for x in item.get("data", {})
                            .get("message", {})
                            .get("content", [])
                            if x.get("type") == "text"
                        )
                    if item.get("type") == "turn/end":
                        completed = (
                            item.get("data", {}).get("reason", {}).get("kind")
                            == "completed"
                        )
                        if not completed:
                            raise ModelError(
                                "HARNESS_TURN_FAILED", "DeepSeek Harness 回合未完成。"
                            )
                if (
                    method == "session.status"
                    and params.get("status") == "idle"
                    and completed
                ):
                    return text
