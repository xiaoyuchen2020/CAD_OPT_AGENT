"""Text/function-only Messages transport for the pinned DeepSeek Harness."""

import json
from uuid import uuid4


def to_chat(payload):
    messages = []
    system = payload.get("system", "")
    if isinstance(system, list):
        system = "\n".join(p.get("text", "") for p in system)
    if system:
        messages.append({"role": "system", "content": system})
    for message in payload.get("messages", []):
        content = message["content"]
        if isinstance(content, str):
            messages.append({"role": message["role"], "content": content})
            continue
        text = "\n".join(p["text"] for p in content if p["type"] == "text")
        calls = [
            {
                "id": p["id"],
                "type": "function",
                "function": {
                    "name": p["name"],
                    "arguments": json.dumps(p["input"], ensure_ascii=False),
                },
            }
            for p in content
            if p["type"] == "tool_use"
        ]
        for p in content:
            if p["type"] != "tool_result":
                continue
            value = p.get("content", "")
            if isinstance(value, list):
                value = "\n".join(v.get("text", "") for v in value)
            messages.append(
                {"role": "tool", "tool_call_id": p["tool_use_id"], "content": value}
            )
        if text or calls:
            item = {"role": message["role"], "content": text or None}
            if calls:
                item["tool_calls"] = calls
            messages.append(item)
        if any(p["type"] not in ("text", "tool_use", "tool_result") for p in content):
            raise ValueError("Unsupported Messages block")
    return messages


def events(message, usage, model):
    result = {
        "id": "msg_" + uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": 0},
    }
    stream = [{"type": "message_start", "message": result.copy()}]
    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["function"]["name"],
                "input": json.loads(call["function"]["arguments"]),
            }
        )
    for i, block in enumerate(blocks):
        text = block["type"] == "text"
        stream.extend(
            [
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": block | ({"text": ""} if text else {"input": {}}),
                },
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": (
                        {"type": "text_delta", "text": block["text"]}
                        if text
                        else {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block["input"]),
                        }
                    ),
                },
                {"type": "content_block_stop", "index": i},
            ]
        )
    reason = "tool_use" if message.get("tool_calls") else "end_turn"
    stream.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": reason, "stop_sequence": None},
                "usage": {"output_tokens": usage.get("completion_tokens", 0)},
            },
            {"type": "message_stop"},
        ]
    )
    return (
        result
        | {
            "content": blocks,
            "stop_reason": reason,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        },
        stream,
    )
