"""Reproducible local general-tool checks on existing real DXF, optional live QA."""

import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import time

from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from scripts.check_agent import run_case, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/general-tools-20261014")
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    w = Workspace(replace(Settings.load(), data_dir=(args.output / "data").resolve()))
    manifest = json.loads(
        Path("artifacts/real-validation-20261014/samples.json").read_text()
    )
    reports = []
    drawings = {}
    try:
        for source in manifest:
            started = time.monotonic()
            content = Path(source["source_path"]).read_bytes()
            assert sha256(content).hexdigest() == source["sha256"]
            d = w.ingest(source["sample_id"] + ".dxf", content)
            drawings[source["sample_id"]] = d
            s = CadAgentSession(
                w,
                ChatRequest(snapshot_id=d["id"], question="通用能力验证", mode="agent"),
            )
            sections = s.execute("get_drawing_overview", {"view": "sections"})
            tables = s.execute("get_drawing_overview", {"view": "tables", "limit": 12})
            topics = {
                term: s.execute("search_sources", {"query": term, "limit": 3})
                for term in ["防水", "保温", "门窗", "装修"]
            }
            kv = s.execute("extract_facts", {"extractor_ref": "generic.key_value@1"})
            tf = s.execute("extract_facts", {"extractor_ref": "generic.table@1"})
            # Inspect all bounded table pages and check exact spans / entity identity.
            checks = 0
            for t in s.index.tables:
                for off in range(0, t["row_count"], 3):
                    page = s.index.read_table(t["table_id"], off, 3)
                    for row in page["items"]:
                        for cell in row["cells"]:
                            for e in cell["evidence"]:
                                r = s.index.by_id[e["region_id"]]
                                a, b = e["text_span"]
                                assert r.text[a:b] == e["quote"] and set(
                                    e["entity_ids"]
                                ) <= set(cell["entity_ids"])
                                checks += 1
            first = next((v["items"][0] for v in topics.values() if v["items"]), None)
            inspect = (
                s.execute("inspect_region", {"region_id": first["region_id"]})
                if first
                else None
            )
            if first:
                (args.output / (source["sample_id"] + "-preview.svg")).write_text(
                    s.index.svg(first["region_id"])
                )
            row = {
                "sample": source["sample_id"],
                "snapshot_id": d["id"],
                "source_sha256": d["sha256"],
                "source_unchanged": sha256(
                    Path(source["source_path"]).read_bytes()
                ).hexdigest()
                == source["sha256"],
                "sections": sections,
                "tables": tables,
                "table_candidates_total": len(s.index.tables),
                "topics": topics,
                "key_value_extraction": kv,
                "table_extraction": tf,
                "table_source_span_checks": checks,
                "inspect": inspect,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            reports.append(row)
            write(args.output / (source["sample_id"] + ".json"), row)
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in [
                            "sample",
                            "table_candidates_total",
                            "table_source_span_checks",
                            "elapsed_seconds",
                        ]
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        write(
            args.output / "summary.json",
            {
                "samples": [
                    {
                        k: r[k]
                        for k in [
                            "sample",
                            "snapshot_id",
                            "source_sha256",
                            "source_unchanged",
                            "table_candidates_total",
                            "table_source_span_checks",
                            "elapsed_seconds",
                        ]
                    }
                    for r in reports
                ],
                "semantic_acceptance": False,
            },
        )
        if args.live:
            cases = [
                {
                    "id": "waterproof",
                    "sample": "sample-62",
                    "question": "图纸对地下室防水等级有什么说明？请检索防水原文与上下文，保留适用条件，不查询砌体做法。",
                    "expected_terms": ["防水"],
                },
                {
                    "id": "insulation",
                    "sample": "sample-59",
                    "question": "图纸中有哪些保温材料或保温做法的明确说明？只列举两条有原文依据的内容，并说明适用范围不明确的部分。",
                    "expected_terms": ["保温"],
                },
                {
                    "id": "table-reading",
                    "sample": "sample-59",
                    "question": "请浏览当前图纸的表格入口，选择一个表格读取前两行，列出原文内容，区分表头候选和数据，不能擅自继承单位。",
                    "expected_terms": [],
                },
                {
                    "id": "spatial-inspect",
                    "sample": "sample-62",
                    "question": "找一处门窗相关文字，查询附近图元并查看局部区域。只报告原文可以证明的信息，不要根据附近线条推断门窗数量或声称看过图片。",
                    "expected_terms": [],
                },
            ]
            live = []
            for case in cases:
                live.append(run_case(w, drawings[case["sample"]], case, args.output))
            write(args.output / "live-summary.json", live)
    finally:
        w.close()


if __name__ == "__main__":
    main()
