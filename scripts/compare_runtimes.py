"""Same-model CAD harness experiment. Never silently substitutes a runtime/model."""

import argparse
from dataclasses import replace
from hashlib import sha256
import html
import json
from pathlib import Path
from statistics import median
import time

from cad_agent.config import Settings
from cad_agent.contracts import ExtractionRequest
from cad_agent.analysis.session import SYSTEM, tool_definitions
from cad_agent.service import Workspace
from scripts.check_agent import CASES, run_case, write


def summarize(output, records, manifest):
    summary = []
    for name in manifest["runtimes"]:
        group = [r for r in records if r["runtime_requested"] == name]
        complete = [r for r in group if r["status"] == "completed"]
        summary.append(
            {
                "runtime": name,
                "runs": len(group),
                "completed": len(complete),
                "source_and_term_checks_passed": sum(
                    r["automated_passed"] for r in group
                ),
                "median_seconds": (
                    round(median(r["result"]["elapsed_seconds"] for r in complete), 3)
                    if complete
                    else None
                ),
                "median_tool_calls": (
                    median(len(r["result"]["trace"]) for r in complete)
                    if complete
                    else None
                ),
                "total_tokens": sum(
                    u.get("total_tokens", 0)
                    for r in group
                    for u in r.get("result", r).get("usage", [])
                ),
                "semantic_review": "pending",
                "fallback_used": False,
            }
        )
    write(output / "summary.json", {"manifest": manifest, "summary": summary})
    rows = "".join(
        "<tr>"
        + "".join(
            "<td>" + html.escape(str(x.get(k, ""))) + "</td>"
            for k in [
                "runtime",
                "runs",
                "completed",
                "source_and_term_checks_passed",
                "median_seconds",
                "median_tool_calls",
                "total_tokens",
            ]
        )
        + "</tr>"
        for x in summary
    )
    items = []
    for r in records:
        result = r.get("result", {})
        quotes = "".join(
            "<li>"
            + html.escape(e["quote"])
            + "<br><small>"
            + html.escape(e["evidence_id"])
            + "</small></li>"
            for e in result.get("citations", [])
        )
        items.append(
            "<details><summary>"
            + html.escape(r["case"]["id"])
            + " · "
            + r["status"]
            + "</summary><p>"
            + html.escape(r["case"]["question"])
            + "</p><pre>"
            + html.escape(result.get("answer", str(r.get("error", ""))))
            + "</pre><ul>"
            + quotes
            + "</ul></details>"
        )
    (output / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>CAD Runtime 对照</title><style>body{max-width:1100px;margin:40px auto;font:16px system-ui;color:#243b35;background:#fafbf8}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap}details{padding:18px;border-bottom:1px solid #ccc}small{color:#708078}</style><h1>相同模型、CAD 工具与真实 DXF 的运行时对照</h1><p>固定所选模型 · 非思考模式 · 六个通用工具 · 禁止代码执行 · 禁止静默回退</p><p>自动检查不等于语义正确率；候选标题、适用范围与完整性仍需逐条复核。桥接只支持文本与函数调用，不能代表各 Harness 的完整产品能力。</p><table><tr><th>Runtime</th><th>运行数</th><th>完成</th><th>来源/关键词通过</th><th>耗时中位数</th><th>工具次数中位数</th><th>总 token</th></tr>'
        + rows
        + "</table>"
        + "".join(items),
        encoding="utf-8",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output", type=Path, default=Path("artifacts/runtime-comparison-20261014")
    )
    p.add_argument(
        "--runtimes",
        nargs="+",
        choices=["native", "deepseek_harness", "codex"],
        default=["native", "deepseek_harness", "codex"],
    )
    p.add_argument(
        "--cases",
        nargs="+",
        choices=[c["id"] for c in CASES],
        default=[c["id"] for c in CASES],
    )
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument(
        "--model-profile",
        choices=["default", "qwen_large", "qwen_medium", "qwen_small"],
        default="default",
    )
    args = p.parse_args()
    if not 1 <= args.repeats <= 5:
        p.error("repeats must be 1..5")
    args.output.mkdir(parents=True, exist_ok=True)
    selected = Settings.load().model_catalog()[args.model_profile]
    settings = replace(selected, data_dir=(args.output / "data").resolve())
    w = Workspace(settings)
    records = []
    sources = {
        s["sample_id"]: s
        for s in json.loads(
            Path("artifacts/real-validation-20261014/samples.json").read_text()
        )
    }
    cases = [c for c in CASES if c["id"] in args.cases]
    manifest = {
        "model": settings.model,
        "provider": settings.provider,
        "thinking": "disabled",
        "model_profile": settings.model_profile,
        "configured_context_length": settings.context_length,
        "max_output_tokens": 6000,
        "max_model_requests": settings.agent_max_rounds + 1,
        "max_tool_calls": settings.agent_max_tool_calls,
        "timeout_seconds": settings.agent_timeout_seconds,
        "runtimes": args.runtimes,
        "repeats": args.repeats,
        "system_prompt_sha256": sha256(SYSTEM.encode()).hexdigest(),
        "tool_schema_sha256": sha256(
            json.dumps(tool_definitions(), sort_keys=True).encode()
        ).hexdigest(),
        "sources": {},
        "code_execution_enabled": False,
        "allow_fallback": False,
        "semantic_review": "pending",
    }
    try:
        drawings = {}
        for sid in sorted({c["sample"] for c in cases}):
            source = sources[sid]
            content = Path(source["source_path"]).read_bytes()
            assert sha256(content).hexdigest() == source["sha256"]
            d = w.ingest(sid + ".dxf", content)
            drawings[sid] = d
            job = w.start_extract(ExtractionRequest(snapshot_id=d["id"]))
            while job["status"] in ("queued", "running"):
                time.sleep(0.05)
                job = w.store.get("job", job["id"])
            if job["status"] not in ("completed", "partial"):
                raise RuntimeError("Shared fixture extraction failed")
            manifest["sources"][sid] = {
                "sha256": source["sha256"],
                "snapshot_id": d["id"],
                "initial_fact_set_id": job["result"]["fact_set_id"],
            }
        write(args.output / "manifest.json", manifest)
        for repeat in range(args.repeats):
            for case in cases:
                # Rotate order to reduce systematic cache/order bias.
                order = (
                    args.runtimes[repeat % len(args.runtimes) :]
                    + args.runtimes[: repeat % len(args.runtimes)]
                )
                for runtime in order:
                    labeled = case | {"id": f"{case['id']}--{runtime}--{repeat+1}"}
                    record = run_case(
                        w, drawings[case["sample"]], labeled, args.output, runtime
                    )
                    record.update(
                        base_case=case["id"],
                        repeat=repeat + 1,
                        source_unchanged=sha256(
                            Path(sources[case["sample"]]["source_path"]).read_bytes()
                        ).hexdigest()
                        == sources[case["sample"]]["sha256"],
                    )
                    write(args.output / (labeled["id"] + ".json"), record)
                    records.append(record)
                    summarize(args.output, records, manifest)
    finally:
        w.close()
    return 0 if all(r["automated_passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
