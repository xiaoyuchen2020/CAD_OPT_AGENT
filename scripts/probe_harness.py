"""Offline process/protocol probe: real harness, scripted model transport."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
from cad_agent.analysis.agent import AgentRunner
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.demo import create_demo
from cad_agent.service import Workspace


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("runtime", choices=["codex", "deepseek_harness"])
    args = p.parse_args()
    root = Path("artifacts/harness-probe") / args.runtime
    root.mkdir(parents=True, exist_ok=True)
    w = Workspace(
        Settings(
            data_dir=root.resolve(),
            api_key="offline-placeholder",
            api_base="https://api.deepseek.com",
            model="deepseek-flash",
            provider="deepseek",
            protocol="openai",
            agent_timeout_seconds=60,
        )
    )
    create_demo(root / "demo.dxf")
    d = w.ingest("probe.dxf", (root / "demo.dxf").read_bytes(), synthetic=True)
    n = 0

    def chat(messages, tools, timeout):
        nonlocal n
        n += 1
        print("MODEL", n, [t["function"]["name"] for t in tools], flush=True)
        if n == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "probe_call_1",
                        "type": "function",
                        "function": {"name": "get_drawing_overview", "arguments": "{}"},
                    }
                ],
            }, {"total_tokens": 1}
        tool = [m for m in messages if m["role"] == "tool"][-1]
        value = json.loads(tool["content"])
        if isinstance(value, str):
            value = json.loads(value)
        eid = value["items"][0]["evidence"]["evidence_id"]
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "claims": [
                        {
                            "text": "原文中有墙体材料表。",
                            "state": "observed",
                            "evidence_ids": [eid],
                        }
                    ],
                    "missing_information": [],
                },
                ensure_ascii=False,
            ),
        }, {"total_tokens": 1}

    w.gateway.chat_turn = chat
    runner = AgentRunner(
        w,
        ChatRequest(
            snapshot_id=d["id"],
            question="这是什么图",
            mode="agent",
            runtime=args.runtime,
        ),
        progress=lambda **kw: print(kw.get("stage", ""), flush=True),
    )
    try:
        result = runner.run()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        (root / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
    finally:
        print("diagnostics", getattr(runner, "runtime_diagnostics", None))
        w.close()


if __name__ == "__main__":
    main()
