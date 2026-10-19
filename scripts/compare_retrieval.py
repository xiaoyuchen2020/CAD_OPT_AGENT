"""Frozen source-grounded retrieval ablation and optional paired live agent runs."""

import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean, median
import time
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.retrieval import chunks, document_vectors
from cad_agent.adapters.embedding import get_embedder
from cad_agent.adapters.model import ModelError
from scripts.check_agent import run_case, write


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output", type=Path, default=Path("artifacts/hybrid-retrieval-20261015")
    )
    p.add_argument("--live", action="store_true")
    p.add_argument("--live-only", action="store_true")
    p.add_argument("--case", default="")
    p.add_argument("--live-label", default="live", choices=["live", "live-retry"])
    p.add_argument("--retry-busy", type=int, choices=[0, 1, 2], default=0)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    fixture = Path("tests/fixtures/retrieval_cases.json")
    cases = json.loads(fixture.read_text())
    manifest = json.loads(
        Path("artifacts/real-validation-20261014/samples.json").read_text()
    )
    w = Workspace(replace(Settings.load(), data_dir=(a.output / "data").resolve()))
    sources, indices, builds = {}, {}, []
    try:
        model_start = time.monotonic()
        embedder = get_embedder(str(w.settings.embedding_model_dir))
        model_seconds = time.monotonic() - model_start
        for source in manifest:
            content = Path(source["source_path"]).read_bytes()
            assert sha256(content).hexdigest() == source["sha256"]
            d = w.ingest(source["sample_id"] + ".dxf", content)
            idx = DrawingIndex(w, d["id"])
            sources[source["sample_id"]] = d
            indices[source["sample_id"]] = idx
            if not a.live_only:
                tick = time.monotonic()
                cs = chunks(idx)
                _, usage = document_vectors(idx, cs, embedder)
                builds.append(
                    {
                        "sample": source["sample_id"],
                        "snapshot_id": d["id"],
                        "source_sha256": d["sha256"],
                        "chunks": len(cs),
                        "usage": usage,
                        "seconds": round(time.monotonic() - tick, 4),
                    }
                )
                print("indexed", source["sample_id"], len(cs), flush=True)
        # Freeze region IDs and full relevant source quotes BEFORE examining rankings.
        gold = []
        for c in cases:
            idx = indices[c["sample"]]
            matches = [
                r for r in idx.regions if any(s in r.text for s in c["gold_contains"])
            ]
            assert bool(matches) == bool(c["gold_contains"]), c["id"]
            if c["category"] == "negative":
                assert not any(
                    "巡检机器人" in r.text or "电池容量" in r.text for r in idx.regions
                )
            gold.append(
                c
                | {
                    "snapshot_id": idx.snapshot,
                    "gold_region_ids": [r.region_id for r in matches],
                    "gold_sources": [idx.evidence(r) for r in matches],
                }
            )
        write(
            a.output / "gold.json",
            {
                "fixture_sha256": sha256(fixture.read_bytes()).hexdigest(),
                "label_basis": "Manually selected source fragments; not independent engineering acceptance. Negative cases mean no parsed supporting source.",
                "cases": gold,
            },
        )
        if not a.live_only:
            records = []
            for n, c in enumerate(gold):
                idx = indices[c["sample"]]
                modes = ["keyword", "lexical", "hybrid"]
                modes = modes[n % 3 :] + modes[: n % 3]
                for mode in modes:
                    timings = []
                    for rep in range(3):
                        tick = time.monotonic()
                        result = idx.search(
                            query=c["query"], keywords=c["keywords"], mode=mode, limit=5
                        )
                        timings.append(time.monotonic() - tick)
                        if rep == 0:
                            first = result
                    if mode == "hybrid":
                        assert first["retrieval"]["effective_mode"] == "hybrid", first[
                            "retrieval"
                        ]
                    ids = [x["region_id"] for x in first["items"]]
                    rank = next(
                        (
                            i + 1
                            for i, rid in enumerate(ids)
                            if rid in c["gold_region_ids"]
                        ),
                        None,
                    )
                    for item in first["items"]:
                        r = idx.by_id[item["region_id"]]
                        assert (
                            r.text[
                                item["snippet_start"] : item["snippet_start"]
                                + len(item["snippet"])
                            ]
                            == item["snippet"]
                        )
                    records.append(
                        {
                            "case_id": c["id"],
                            "category": c["category"],
                            "sample": c["sample"],
                            "mode": mode,
                            "hit_at_5": bool(rank) if c["gold_region_ids"] else None,
                            "reciprocal_rank": 1 / rank if rank else 0,
                            "first_seconds": round(timings[0], 4),
                            "warm_median_seconds": round(median(timings[1:]), 4),
                            "negative_returns_candidates": (
                                bool(ids) if not c["gold_region_ids"] else None
                            ),
                            "result": first,
                        }
                    )
                print("retrieved", c["id"], flush=True)
            write(a.output / "retrieval.json", records)
            summary = {}
            for mode in ["keyword", "lexical", "hybrid"]:
                rows = [r for r in records if r["mode"] == mode]
                positive = [r for r in rows if r["hit_at_5"] is not None]
                summary[mode] = {
                    "hits": sum(r["hit_at_5"] for r in positive),
                    "positive_cases": len(positive),
                    "mrr_at_5": mean(r["reciprocal_rank"] for r in positive),
                    "mean_first_seconds": mean(r["first_seconds"] for r in rows),
                    "median_warm_seconds": median(
                        r["warm_median_seconds"] for r in rows
                    ),
                    "query_embedding_tokens": sum(
                        r["result"]
                        .get("retrieval", {})
                        .get("query_embedding", {})
                        .get("input_tokens", 0)
                        for r in rows
                    ),
                    "negative_candidate_cases": sum(
                        r["negative_returns_candidates"] is True for r in rows
                    ),
                }
            write(
                a.output / "summary.json",
                {
                    "fixture_sha256": sha256(fixture.read_bytes()).hexdigest(),
                    "model": embedder.identity,
                    "model_load_seconds": model_seconds,
                    "index_builds": builds,
                    "retrieval": summary,
                    "note": "4 existing samples are related variants, not independent project generalization. Hit@5 is any relevant source, not complete answer recall. Local embedding tokenizer counts are not chat billing tokens.",
                },
            )
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        if a.live:
            original_gateway = w.gateway
            retry_log = []

            class BenchmarkGateway:
                def chat_turn(self, messages, tools, timeout=60):
                    deadline = time.monotonic() + timeout
                    for attempt in range(a.retry_busy + 1):
                        try:
                            return original_gateway.chat_turn(
                                messages,
                                tools,
                                timeout=max(0.1, deadline - time.monotonic()),
                            )
                        except ModelError as exc:
                            if (
                                exc.code != "MODEL_SERVICE_BUSY"
                                or attempt == a.retry_busy
                                or deadline - time.monotonic() < 2
                            ):
                                raise
                            retry_log.append(
                                {
                                    "error": exc.code,
                                    "attempt": attempt + 1,
                                    "usage_reported": False,
                                }
                            )
                            print("benchmark-only retry HTTP 503", flush=True)
                            time.sleep(0.5 * (attempt + 1))

            w.gateway = BenchmarkGateway()
            live = []
            for n, c in enumerate(
                x
                for x in cases
                if x.get("live") and (not a.case or x["id"] in a.case.split(","))
            ):
                modes = ["keyword", "hybrid"] if n % 2 == 0 else ["hybrid", "keyword"]
                for mode in modes:
                    directory = a.output / a.live_label / mode
                    directory.mkdir(parents=True, exist_ok=True)
                    case = c | {
                        "question": c["query"]
                        + "？请检索并读取原文后回答，保留适用范围；无法确定时明确说明。",
                        "expected_terms": [],
                    }
                    retry_log.clear()
                    row = run_case(
                        w, sources[c["sample"]], case, directory, retrieval_mode=mode
                    )
                    row["benchmark_retries"] = list(retry_log)
                    row["retry_policy"] = a.retry_busy
                    write(directory / (c["id"] + ".json"), row)
                    live.append(row)
                    write(a.output / (a.live_label + "-summary.json"), live)
        assert all(
            sha256(Path(s["source_path"]).read_bytes()).hexdigest() == s["sha256"]
            for s in manifest
        )
    finally:
        w.close()


if __name__ == "__main__":
    main()
