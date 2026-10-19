"""Freeze local implementation and compare all configured model/runtime pairs."""

import argparse
from collections import Counter
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import shutil
from statistics import mean, median
import subprocess
import sys

from cad_agent.config import ROOT, Settings, local_env


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def summarize(output, profiles):
    rows = []
    for profile in profiles:
        records = []
        for path in (output / profile).glob("*--*--*.json"):
            item = json.loads(path.read_text())
            if isinstance(item, dict) and "status" in item:
                records.append(item)
        for runtime in ("native", "deepseek_harness", "codex"):
            group = [r for r in records if r["runtime_requested"] == runtime]
            good = [r for r in group if r["status"] == "completed"]
            usage_runs = [r for r in group if r.get("usage_reported")]
            tokens = lambda r: sum(
                u.get("total_tokens", 0) for u in r.get("result", r).get("usage", [])
            )
            row = {
                "profile": profile,
                "model": group[0]["live_model"] if group else None,
                "runtime": runtime,
                "runs": len(group),
                "published": len(good),
                "source_and_term_checks_passed": sum(
                    r["automated_passed"] for r in group
                ),
                "success_median_seconds": (
                    round(median(r["wall_seconds"] for r in good), 3) if good else None
                ),
                "all_attempts_median_seconds": (
                    round(median(r["wall_seconds"] for r in group), 3)
                    if group
                    else None
                ),
                "reported_total_tokens": (
                    sum(tokens(r) for r in usage_runs) if usage_runs else None
                ),
                "usage_reported_runs": len(usage_runs),
                "success_mean_tokens": (
                    round(mean(tokens(r) for r in good), 1)
                    if good and all(r["usage_reported"] for r in good)
                    else None
                ),
                "errors": dict(
                    Counter(
                        r["error"]["code"] for r in group if r["status"] != "completed"
                    )
                ),
                "fallback_used": any(
                    r.get("result", {}).get("fallback_used") for r in group
                ),
            }
            rows.append(row)
    write(
        output / "matrix-summary.json",
        {"rows": rows, "semantic_review": "pending", "missing_usage_is_not_zero": True},
    )
    headings = [
        "配置",
        "模型",
        "运行时",
        "次数",
        "发布",
        "来源/关键词通过",
        "成功耗时中位秒",
        "全部耗时中位秒",
        "已报告总token",
        "用量报告次数",
        "成功平均token",
        "错误",
    ]
    keys = [
        "profile",
        "model",
        "runtime",
        "runs",
        "published",
        "source_and_term_checks_passed",
        "success_median_seconds",
        "all_attempts_median_seconds",
        "reported_total_tokens",
        "usage_reported_runs",
        "success_mean_tokens",
        "errors",
    ]
    table = "".join(
        "<tr>"
        + "".join(
            "<td>" + html.escape(str(r[k]) if r[k] is not None else "—") + "</td>"
            for k in keys
        )
        + "</tr>"
        for r in rows
    )
    links = " · ".join(f'<a href="{p}/report.html">{p} 逐条记录</a>' for p in profiles)
    (output / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>模型与 Harness 对照</title><style>body{font:15px system-ui;margin:32px;color:#243b35;background:#fafbf8}table{border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #ccd5cc;text-align:left}th{background:#e8efe4}</style><h1>同一代码快照、问题、DXF 的模型 × Harness 对照</h1><p>发布成功不是语义正确率。失败耗时不代表回答速度。— 表示无成功回答或服务未报告用量，不是 0 token。禁止回退与代码执行。</p><p>'
        + links
        + "</p><table><tr>"
        + "".join("<th>" + h + "</th>" for h in headings)
        + "</tr>"
        + table
        + "</table>"
    )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=["default", "qwen_large", "qwen_medium", "qwen_small"],
        default=["qwen_medium", "qwen_small", "qwen_large", "default"],
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a fresh output directory to preserve earlier evidence")
    if not 1 <= args.repeats <= 5:
        parser.error("repeats must be 1..5")
    output.mkdir(parents=True)
    snapshot = output / "code-snapshot"
    snapshot.mkdir()
    source_paths = sorted(
        [
            p
            for folder in ("cad_agent", "scripts")
            for p in (ROOT / folder).rglob("*")
            if p.suffix in (".py", ".mjs") and "__pycache__" not in p.parts
        ]
    )
    hashes = {}
    for path in source_paths:
        relative = path.relative_to(ROOT)
        content = path.read_bytes()
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[str(relative)] = sha256(content).hexdigest()
    if any(
        sha256(p.read_bytes()).hexdigest() != hashes[str(p.relative_to(ROOT))]
        for p in source_paths
    ):
        raise SystemExit(
            "Source changed while copying; rerun with a new output directory"
        )
    (snapshot / ".harness").symlink_to(ROOT / ".harness", target_is_directory=True)
    manifest = snapshot / "artifacts/real-validation-20261014/samples.json"
    manifest.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "artifacts/real-validation-20261014/samples.json", manifest)
    write(
        output / "code-manifest.json",
        {
            "files": hashes,
            "model_profiles": args.profiles,
            "repeats": args.repeats,
            "credentials_in_snapshot": False,
            "dependency_lock_sha256": sha256(
                (ROOT / "harness/package-lock.json").read_bytes()
            ).hexdigest(),
        },
    )
    # Credentials stay in memory/environment, not the frozen source or reports.
    env = dict(os.environ) | local_env()
    env["PYTHONPATH"] = str(snapshot)
    statuses = {}
    for profile in args.profiles:
        with (output / (profile + "-live.log")).open("w") as log:
            print("START", profile, flush=True)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.compare_runtimes",
                    "--model-profile",
                    profile,
                    "--repeats",
                    str(args.repeats),
                    "--output",
                    str(output / profile),
                ],
                cwd=snapshot,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            statuses[profile] = proc.returncode
        rows = summarize(output, args.profiles)
        write(output / "process-status.json", statuses)
        print(
            "DONE",
            profile,
            json.dumps(
                [r for r in rows if r["profile"] == profile], ensure_ascii=False
            ),
            flush=True,
        )
    return 0 if all(code == 0 for code in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
