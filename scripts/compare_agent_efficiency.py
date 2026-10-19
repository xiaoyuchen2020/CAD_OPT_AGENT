"""Replay frozen real-DXF tool traces for free; optionally run four bounded live cases.

Historical baselines are reused, never rerun. Live results cannot be overwritten.
No model retries or harness fallbacks. Failed requests still consume call budget.
"""

import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import time

from cad_agent.adapters.model import ModelError
from cad_agent.analysis.agent import AgentRunner
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.service import Workspace
from scripts.check_agent import run_case, write

BASE = Path("artifacts/hybrid-retrieval-20261015")
CASES = ("59-chase", "59-table", "62-water", "62-absent")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/agent-efficiency-20261015"))
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if args.live and (out / "live-budget.json").exists():
        raise SystemExit("Live batch already attempted; refusing a duplicate paid run.")
    baseline = {k: json.loads((BASE / "live-retry/hybrid" / (k + ".json")).read_text()) for k in CASES}
    settings = replace(Settings.load(), data_dir=(BASE / "data").resolve(), agent_max_rounds=4)
    w = Workspace(settings)
    budget = {"max_requests": 20, "max_requests_per_case": 5,
              "reported_token_stop": 80000, "reported_tokens": 0,
              "requests": 0, "automatic_retries": 0, "attempts": []}
    try:
        manifest = {s["sample_id"]: s for s in json.loads(Path("artifacts/real-validation-20261014/samples.json").read_text())}
        drawings = {}
        for b in baseline.values():
            sample = b["case"]["sample"]
            source = manifest[sample]
            assert sha256(Path(source["source_path"]).read_bytes()).hexdigest() == source["sha256"]
            drawing = w.store.get("drawing", b["snapshot_id"])
            assert drawing["sha256"] == source["sha256"] == b["input_sha256"]
            drawings[sample] = drawing
        replay = []
        # The exact old tool arguments are replayed; this measures payload size,
        # not whether the model would choose the same sequence under new prompts.
        for key, b in baseline.items():
            rows = {}
            for policy in ("legacy", "efficient"):
                s = AgentRunner(w, ChatRequest(snapshot_id=b["snapshot_id"], question=b["case"]["question"],
                    mode="agent", retrieval_mode="hybrid", agent_policy=policy))
                wires = []
                for t in b["result"]["trace"]:
                    if "arguments" not in t:
                        continue
                    wire = s.invoke_tool(t["tool"], t["arguments"])
                    wires.append({"tool": t["tool"], "chars": len(wire), "result": json.loads(wire)})
                regions = {r.region_id: r for r in w.regions(b["snapshot_id"])}
                assert all(regions[e["region_id"]].text[slice(*e["text_span"])] == e["quote"] for e in s.evidence.values())
                rows[policy] = {"response_chars": sum(x["chars"] for x in wires),
                                "stop_reason": s.policy.stop_reason, "trace": s.trace, "responses": wires}
            replay.append({"case_id": key, **rows})
            print("offline replay", key, {k: v["response_chars"] for k, v in rows.items()}, flush=True)
        write(out / "offline-replay.json", replay)
        write(out / "source-manifest.json", list(manifest.values()))
        if not args.live:
            return
        write(out / "live-budget.json", budget)
        original = w.gateway.chat_turn
        current = {"case": None, "calls": 0}

        def bounded_turn(messages, tools, timeout=60):
            if budget["requests"] >= budget["max_requests"] or current["calls"] >= 5 or budget["reported_tokens"] >= 80000:
                raise ModelError("BENCHMARK_BUDGET", "测试请求预算已用完；不重试。")
            budget["requests"] += 1
            current["calls"] += 1
            attempt = {"case": current["case"], "number": budget["requests"],
                       "message_chars": len(json.dumps(messages, ensure_ascii=False)), "tools_enabled": bool(tools)}
            budget["attempts"].append(attempt)
            write(out / "live-budget.json", budget)
            tick = time.monotonic()
            try:
                msg, usage = original(messages, tools, timeout)
                attempt.update(status="completed", usage=usage)
                budget["reported_tokens"] += usage.get("total_tokens", 0)
                # Store exact model input/output for evidence and stopping review,
                # without endpoint headers, credentials or private reasoning.
                write(out / f'request-{budget["requests"]:02d}.json', {
                    "case": current["case"], "messages": messages, "response": msg, "usage": usage,
                })
                return msg, usage
            except ModelError as error:
                attempt.update(status="failed", error_code=error.code, usage_unknown=True)
                raise
            finally:
                attempt["seconds"] = round(time.monotonic() - tick, 3)
                write(out / "live-budget.json", budget)

        w.gateway.chat_turn = bounded_turn
        records = []
        for key, b in baseline.items():
            current.update(case=key, calls=0)
            r = run_case(w, drawings[b["case"]["sample"]], b["case"], out, retrieval_mode="hybrid")
            r["historical_baseline"] = str(BASE / "live-retry/hybrid" / (key + ".json"))
            r["comparison_limit"] = "Historical, non-randomized baseline; old runs include HTTP retries. Cache, service load and sampling confound latency."
            write(out / (key + ".json"), r)
            records.append(r)
        summary = []
        for r in records:
            key = r["case"]["id"]
            entry = {"case_id": key}
            for label, record in (("before", baseline[key]), ("after", r)):
                result = record.get("result", record)
                entry[label] = {"status": record["status"], "seconds": record["wall_seconds"],
                    "tokens": sum(u.get("total_tokens", 0) for u in result.get("usage", [])) if result.get("usage") else None,
                    "token_measurement": "reported_only; requests without usage have unknown cost",
                    "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in result.get("usage", [])),
                    "completion_tokens": sum(u.get("completion_tokens", 0) for u in result.get("usage", [])),
                    "cache_hit_tokens": sum(u.get("prompt_cache_hit_tokens", 0) for u in result.get("usage", [])),
                    "model_turns": len(result.get("usage", [])), "tool_calls": len(result.get("trace", [])),
                    "evidence_checks": all(c["passed"] for c in record["evidence_checks"]) if record.get("evidence_checks") else None}
            summary.append(entry)
        write(out / "comparison.json", summary)
    finally:
        w.close()


if __name__ == "__main__":
    main()
