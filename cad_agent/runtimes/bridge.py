"""Loopback model bridge: identical DeepSeek settings and a CAD-only tool boundary.

Harness history accepts text + function calls only. Trusted CAD crops are
attached by SiwcBridge in the parent, never accepted as arbitrary image URLs.
No hosted tools, code interpreter, filesystem, or arbitrary proxy routes.
"""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from cad_agent.adapters.model import ModelError
from cad_agent.analysis.session import tool_definitions
from cad_agent.runtimes import messages as anthropic


def response_messages(payload):
    messages = []
    if payload.get("instructions"):
        messages.append({"role": "system", "content": payload["instructions"]})
    inputs = payload.get("input", [])
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    for item in inputs:
        kind = item.get("type", "message")
        if kind == "function_call":
            call = {
                "id": item["call_id"],
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append(
                    {"role": "assistant", "content": None, "tool_calls": [call]}
                )
        elif kind == "function_call_output":
            output = item["output"]
            if not isinstance(output, str):
                output = "\n".join(
                    p.get("text", "")
                    for p in output
                    if p.get("type") in ("input_text", "text", "output_text")
                )
            messages.append(
                {"role": "tool", "tool_call_id": item["call_id"], "content": output}
            )
        elif kind == "message":
            content = item.get("content", "")
            if isinstance(content, list):
                if any(
                    p.get("type") not in ("input_text", "output_text", "text")
                    for p in content
                ):
                    raise ValueError("Only text inputs supported")
                content = "\n".join(p.get("text", "") for p in content)
            role = item.get("role", "user")
            if (
                role == "assistant"
                and messages
                and messages[-1].get("role") == "assistant"
            ):
                # Responses splits commentary and function calls into items;
                # Chat requires one assistant message before all tool results.
                messages[-1]["content"] = (
                    "\n".join(x for x in (messages[-1].get("content"), content) if x)
                    or None
                )
            else:
                messages.append(
                    {
                        "role": "system" if role == "developer" else role,
                        "content": content,
                    }
                )
        elif kind == "additional_tools" and item.get("role") == "developer":
            # New Codex versions send tool declarations as input items. Their
            # schemas are filtered separately; never turn them into instructions.
            continue
        elif kind != "reasoning":
            raise ValueError("Unsupported Responses item")
    return messages


def response_events(message, usage, model):
    rid = "resp_" + uuid4().hex
    base = {
        "id": rid,
        "object": "response",
        "model": model,
        "status": "in_progress",
        "output": [],
    }
    events = [
        ("response.created", {"response": base}),
        ("response.in_progress", {"response": base}),
    ]
    output = []
    for call in message.get("tool_calls") or []:
        index = len(output)
        fn = call["function"]
        item = {
            "id": "fc_" + uuid4().hex,
            "type": "function_call",
            "call_id": call["id"],
            "name": fn["name"],
            "arguments": fn["arguments"],
            "status": "completed",
        }
        events += [
            (
                "response.output_item.added",
                {
                    "output_index": index,
                    "item": item | {"arguments": "", "status": "in_progress"},
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "output_index": index,
                    "item_id": item["id"],
                    "delta": fn["arguments"],
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "output_index": index,
                    "item_id": item["id"],
                    "arguments": fn["arguments"],
                },
            ),
            ("response.output_item.done", {"output_index": index, "item": item}),
        ]
        output.append(item)
    if message.get("content"):
        index = len(output)
        text = message["content"]
        part = {"type": "output_text", "text": text, "annotations": []}
        item = {
            "id": "msg_" + uuid4().hex,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [part],
        }
        loc = {"item_id": item["id"], "output_index": index, "content_index": 0}
        events += [
            (
                "response.output_item.added",
                {
                    "output_index": index,
                    "item": item | {"content": [], "status": "in_progress"},
                },
            ),
            ("response.content_part.added", loc | {"part": part | {"text": ""}}),
            ("response.output_text.delta", loc | {"delta": text}),
            ("response.output_text.done", loc | {"text": text}),
            ("response.content_part.done", loc | {"part": part}),
            ("response.output_item.done", {"output_index": index, "item": item}),
        ]
        output.append(item)
    response = base | {
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }
    events.append(("response.completed", {"response": response}))
    return response, [
        dict(type=kind, sequence_number=i, **data)
        for i, (kind, data) in enumerate(events)
    ]


class ModelBridge:
    def __init__(self, session, registered_tools=None):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.requests = []
        self.error = None
        # Supplied by our trusted runtime registration, never by an HTTP caller.
        self.registered_tools = {t['function']['name'] for t in (registered_tools or [])}
        self.lock = threading.Lock()
        self.subscription = None
        if session.w.settings.provider == "responses_subscription":
            from cad_agent.runtimes.siwc_bridge import SiwcBridge
            self.subscription = SiwcBridge(session)
            session.runtime_diagnostics = {"runtime": session.request.runtime,
                "model_requests": self.requests, "automatic_retries": 0}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, value):
                raw = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                if (
                    self.headers.get("Authorization") != "Bearer " + outer.token
                    and self.headers.get("x-api-key") != outer.token
                ):
                    return self.send_json(401, {"error": {"message": "Unauthorized"}})
                path = self.path.split("?")[0]
                if path not in (
                    "/v1/responses",
                    "/responses",
                    "/v1/chat/completions",
                    "/chat/completions",
                    "/v1/messages",
                    "/messages",
                    "/v1/cad/tool",
                ):
                    return self.send_json(
                        404, {"error": {"message": "Unsupported route"}}
                    )
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2_000_000:
                        raise ValueError("Request size")
                    payload = json.loads(self.rfile.read(length))
                    if path == "/v1/cad/tool":
                        with outer.lock:
                            result = session.invoke_tool(
                                payload["name"], payload["arguments"]
                            )
                        return self.send_json(200, json.loads(result))
                    with outer.lock:
                        message, usage = outer.complete(
                            payload,
                            path.endswith("/responses"),
                            path.endswith("/messages"),
                        )
                    if path.endswith("/messages"):
                        result, events = anthropic.events(
                            message, usage, session.w.settings.model
                        )
                        if not payload.get("stream"):
                            return self.send_json(200, result)
                        raw = "".join(
                            "event: "
                            + e["type"]
                            + "\ndata: "
                            + json.dumps(e, ensure_ascii=False)
                            + "\n\n"
                            for e in events
                        ).encode()
                    elif path.endswith("/responses"):
                        result, events = response_events(
                            message, usage, session.w.settings.model
                        )
                        if payload.get("stream"):
                            raw = "".join(
                                "event: "
                                + e["type"]
                                + "\ndata: "
                                + json.dumps(e, ensure_ascii=False)
                                + "\n\n"
                                for e in events
                            ).encode()
                        else:
                            return self.send_json(200, result)
                    elif payload.get("stream"):
                        chunk = {
                            "id": "chatcmpl_" + uuid4().hex,
                            "object": "chat.completion.chunk",
                            "model": session.w.settings.model,
                        }
                        delta = dict(message)
                        if delta.get("tool_calls"):
                            delta["tool_calls"] = [
                                dict(index=i, **c)
                                for i, c in enumerate(delta["tool_calls"])
                            ]
                        raw = (
                            "data: "
                            + json.dumps(
                                chunk
                                | {
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": delta,
                                            "finish_reason": None,
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                            + "\n\ndata: "
                            + json.dumps(
                                chunk
                                | {
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {},
                                            "finish_reason": (
                                                "tool_calls"
                                                if message.get("tool_calls")
                                                else "stop"
                                            ),
                                        }
                                    ],
                                    "usage": usage,
                                }
                            )
                            + "\n\ndata: [DONE]\n\n"
                        ).encode()
                    else:
                        return self.send_json(
                            200,
                            {
                                "id": "chatcmpl_" + uuid4().hex,
                                "object": "chat.completion",
                                "model": session.w.settings.model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "message": message,
                                        "finish_reason": (
                                            "tool_calls"
                                            if message.get("tool_calls")
                                            else "stop"
                                        ),
                                    }
                                ],
                                "usage": usage,
                            },
                        )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (ValueError, KeyError, TypeError, ModelError) as error:
                    outer.error = (
                        error
                        if isinstance(error, ModelError)
                        else ModelError(
                            "HARNESS_PROTOCOL_ERROR",
                            "Harness 请求与受限模型桥接协议不兼容。",
                        )
                    )
                    self.send_json(
                        400,
                        {
                            "error": {
                                "message": outer.error.message,
                                "type": outer.error.code,
                            }
                        },
                    )
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def complete(self, payload, responses=False, messages_api=False):
        s = self.session
        if self.error:
            raise self.error  # A harness retry must never create another upstream request.
        s._remaining()
        if payload.get("model") != s.w.settings.model:
            raise ModelError("MODEL_MISMATCH", "Harness 请求的模型与对照模型不一致。")
        if len(self.requests) >= s.round_limit + 1:
            raise ModelError("AGENT_ROUND_LIMIT", "Harness 已达到统一模型请求上限。")
        allowed = {t["function"]["name"] for t in tool_definitions()}
        declared = list(payload.get("tools", []))
        if responses and isinstance(payload.get("input"), list):
            for item in payload["input"]:
                if item.get("type") == "additional_tools" and item.get("role") == "developer":
                    declared.extend(item.get("tools", []))
        offered_items = [child for t in declared
                         for child in (t.get("tools", []) if t.get("type") == "namespace" else [t])]
        offered = {
            t.get("name", t.get("function", {}).get("name", ""))
            for t in offered_items
        }
        if self.subscription and responses:
            # Codex can hide dynamic tools behind its code-mode declaration.
            # Publish only CAD tools actually registered via thread/start; the
            # orchestration/code tool itself is never exposed to the model.
            offered |= self.registered_tools
        # Only the canonical CAD schemas ever reach the real model. Built-in
        # harness tools are stripped here, and forbidden calls are rejected below.
        tools = [t for t in s.offered_tools() if t["function"]["name"] in offered]
        messages = (
            response_messages(payload)
            if responses
            else (anthropic.to_chat(payload) if messages_api else payload["messages"])
        )
        final = (
            len(self.requests) >= s.round_limit - 1
            or s.calls >= s.w.settings.agent_max_tool_calls
            or bool(s.policy.stop_reason)
        )
        if self.subscription:
            reason = self.subscription.controls.stop_reason(s)
            if reason:
                s.policy.stop_reason = reason
                final = True
            if self.requests and self.requests[-1].get("final_only"):
                # Exactly one explicit validator-requested correction, never a
                # harness/provider retry or renewed exploration after synthesis.
                repair = (s.validation_issue and s.answer_repairs == 1
                          and not any(e.get("answer_repair") for e in self.requests))
                if not repair:
                    raise ModelError("AGENT_ROUND_LIMIT", "订阅收尾请求已完成，不再调用模型。")
        if final:
            if s.policy.enabled and not s.policy.stop_reason:
                s.policy.stop_reason = "exploration_budget"
            tools = []
            messages = messages + [
                {
                    "role": "user",
                    "content": "探索预算已结束，仅根据已读取证据返回最终 JSON。不足部分标 unknown，不再调用工具。",
                }
            ]
        self.requests.append(
            {
                "protocol": (
                    "responses"
                    if responses
                    else ("messages" if messages_api else "chat_completions")
                ),
                "model": payload["model"],
                "offered_tools": sorted(offered),
                "forwarded_tools": [t["function"]["name"] for t in tools],
                "final_only": final,
                "message_shape": [
                    {
                        "role": m.get("role"),
                        "call_ids": [c["id"] for c in m.get("tool_calls", [])],
                        "tool_call_id": m.get("tool_call_id"),
                    }
                    for m in messages
                ],
            }
        )
        s.progress(
            stage=f"Harness 模型调用 · 第 {len(self.requests)} 轮",
            trace=s.trace,
            usage=s.usage,
        )
        if self.subscription:
            message, usage = self.subscription.complete(s.model_messages(messages, final), tools, final, self.requests[-1])
        else:
            with s.metrics.measure("model_wait"):
                model_messages = s.model_messages(messages, final)
                message, usage = s.journal.model_call({'messages': model_messages, 'tools': tools},
                    lambda: s.w.gateway.chat_turn(model_messages, tools, timeout=s._remaining()),
                    title=f'桥接模型请求 · 第 {len(self.requests)} 轮')
            s.usage.append(usage)
        for call in message.get("tool_calls") or []:
            if (
                final
                or call["function"]["name"] not in allowed
                or call["function"]["name"]
                not in {t["function"]["name"] for t in tools}
            ):
                raise ModelError(
                    "TOOL_NOT_ALLOWED", "模型返回未授权工具；已阻止交给 Harness 执行。"
                )
        return message, usage

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
