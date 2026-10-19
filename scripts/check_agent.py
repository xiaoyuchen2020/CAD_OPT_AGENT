"""Explicit live tool-loop validation; outputs are local artifacts, not acceptance gold."""

import argparse
from dataclasses import replace
from hashlib import sha256
import json
import time
from pathlib import Path

from cad_agent.analysis.agent import AgentRunner
from cad_agent.adapters.model import ModelError
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.demo import create_demo
from cad_agent.service import Workspace

CASES = [
    {
        "id": "overview",
        "sample": "sample-62",
        "question": "当前这张图纸是什么",
        "expected_terms": ["建筑施工图设计总说明"],
    },
    {
        "id": "cross-line",
        "sample": "sample-59",
        "question": "设计说明“2、本施工图中”这一条，对内墙、外墙厚度分别如何规定？请读取完整条款及上下文，保留备选条件并引用原文。",
        "expected_terms": ["240", "100", "200"],
    },
    {
        "id": "table",
        "sample": "sample-59",
        "question": "隔声相关表格里“200厚B06级蒸压加气混凝土砌块”的做法，砌块和两侧抹灰各多厚？请区分表格中的其他备选墙体并引用对应原文。",
        "expected_terms": ["200", "15", "B06"],
    },
    {
        "id": "scope",
        "sample": "sample-62",
        "question": "住宅与非住宅配套公建、设备用房的外墙材料和强度等级是否相同？请分别查阅1.3和1.4章节的标题与条款，引用来源并比较。",
        "expected_terms": ["B07", "A5.0", "MU10", "混凝土多孔砖"],
    },
    {
        "id": "query",
        "sample": "sample-60",
        "question": "地下墙体章节中，注明“地下室室内部分”的内墙采用什么材料、强度和砂浆等级？请读取来源，明确该限定条件。",
        "expected_terms": ["MU20", "M20", "地下室室内部分"],
    },
    {
        "id": "missing",
        "sample": "sample-61",
        "question": "1.4章节中除注明外的内墙，在这条设计说明里能否直接确定具体厚度？请引用条款和章节范围；若需要其他图纸请明确说明。",
        "expected_terms": ["厚度"],
    },
]


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def run_case(w, drawing, case, output, runtime="native", retrieval_mode="keyword"):
    started = time.monotonic()
    runner = AgentRunner(
        w,
        ChatRequest(
            snapshot_id=drawing["id"],
            question=case["question"],
            mode="agent",
            runtime=runtime,
            allow_fallback=False,
            retrieval_mode=retrieval_mode,
        ),
        progress=lambda **kw: print(
            case["id"] + ": " + kw.get("stage", ""), flush=True
        ),
    )
    record = {
        "case": case,
        "snapshot_id": drawing["id"],
        "input_sha256": drawing["sha256"],
        "live_model": w.settings.model,
        "model_profile": w.settings.model_profile,
        "synthetic": drawing["synthetic"],
        "formal_acceptance": False,
        "semantic_review": "pending",
        "runtime_requested": runtime,
        "retrieval_mode": retrieval_mode,
    }
    original_validate = runner.finish
    rejected = []

    def validate(content):
        try:
            return original_validate(content)
        except ValueError as error:
            rejected.append(
                {"candidate": content, "validation_error": str(error)[:1000]}
            )
            write(output / (case["id"] + "-rejected.json"), rejected)
            raise

    runner.finish = validate
    try:
        result = runner.run()
        regions = {r.region_id: r for r in w.regions(drawing["id"])}
        entities = {
            e["entity_id"]: e
            for e in json.loads(
                w.store.artifact(drawing["id"], "entities.json").read_text()
            )
        }
        evidence_checks = []
        for e in result["citations"]:
            r = regions[e["region_id"]]
            checks = {
                "snapshot_matches": e["snapshot_id"] == drawing["id"],
                "quote_matches": r.text[slice(*e["text_span"])] == e["quote"],
                "entities_match": bool(e["entity_ids"])
                and set(e["entity_ids"]) <= set(r.entity_ids),
                "parts_match": all(
                    entities[p["entity_id"]]["text"][slice(*p["entity_text_span"])]
                    == r.text[slice(*p["text_span"])]
                    for p in e["source_parts"]
                ),
            }
            evidence_checks.append(
                {
                    "evidence_id": e["evidence_id"],
                    **checks,
                    "passed": all(checks.values()),
                }
            )
        terms = {
            term: term in result["answer"] for term in case.get("expected_terms", [])
        }
        record.update(
            status="completed",
            result=result,
            evidence_checks=evidence_checks,
            expected_term_checks=terms,
            automated_passed=bool(evidence_checks)
            and all(e["passed"] for e in evidence_checks)
            and all(terms.values()),
        )
    except ModelError as e:
        record.update(
            status="failed",
            error={"code": e.code, "message": e.message},
            trace=runner.trace,
            usage=runner.usage,
            retrieval_usage=runner.retrieval_usage,
            automated_passed=False,
            runtime_diagnostics=getattr(runner, "runtime_diagnostics", None),
        )
    record["wall_seconds"] = round(time.monotonic() - started, 3)
    record["usage_reported"] = bool(record.get("result", record).get("usage"))
    write(output / (case["id"] + ".json"), record)
    print(
        json.dumps(
            {k: record[k] for k in ("status", "automated_passed")}, ensure_ascii=False
        ),
        flush=True,
    )
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--synthetic",
        action="store_true",
        help="Only sends generated demo drawing text",
    )
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path("artifacts/real-validation-20261014/samples.json"),
    )
    p.add_argument("--case", choices=[c["id"] for c in CASES])
    p.add_argument(
        "--output", type=Path, default=Path("artifacts/agent-validation-20261014")
    )
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.load(), data_dir=(args.output / "data").resolve())
    w = Workspace(settings)
    records = []
    try:
        if args.synthetic:
            path = args.output / "synthetic.dxf"
            create_demo(path)
            d = w.ingest("工具问答合成测试.dxf", path.read_bytes(), synthetic=True)
            records.append(
                run_case(
                    w,
                    d,
                    {
                        "id": "synthetic",
                        "question": "检索内墙条款，读取一条原文及上下文，说明该条款的材料并引用来源。",
                    },
                    args.output,
                )
            )
        else:
            manifest = {
                s["sample_id"]: s for s in json.loads(args.manifest.read_text())
            }
            for case in CASES:
                if args.case and case["id"] != args.case:
                    continue
                source = manifest[case["sample"]]
                path = Path(source["source_path"])
                content = path.read_bytes()
                if sha256(content).hexdigest() != source["sha256"]:
                    raise ValueError("Source hash changed; stop validation")
                drawing = w.ingest(case["sample"] + ".dxf", content)
                record = run_case(w, drawing, case, args.output)
                record["source_unchanged"] = (
                    sha256(path.read_bytes()).hexdigest() == source["sha256"]
                )
                write(args.output / (case["id"] + ".json"), record)
                records.append(record)
        write(
            args.output
            / (
                "synthetic-summary.json"
                if args.synthetic
                else (args.case + "-summary.json" if args.case else "summary.json")
            ),
            [
                {
                    "case": r["case"]["id"],
                    "status": r["status"],
                    "automated_passed": r["automated_passed"],
                    "semantic_review": r["semantic_review"],
                }
                for r in records
            ],
        )
    finally:
        w.close()
    return 0 if records and all(r["automated_passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
