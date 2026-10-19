"""Summarize all attempts; keep service failures and label review visible."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output", type=Path, default=Path("artifacts/hybrid-retrieval-20261015")
    )
    a = p.parse_args()
    root = a.output
    retrieval = json.loads((root / "retrieval.json").read_text())
    review = {
        "59-id": {
            "disposition": "exclude_from_primary_hit_metric",
            "reason": "The query is only DPM15, but the frozen label selects one B06 wall. All three methods returned other valid DPM15 source spans. The query does not identify that one wall.",
            "original_gold_preserved": True,
        }
    }
    metrics = {}
    for mode in ["keyword", "lexical", "hybrid"]:
        valid = [
            r
            for r in retrieval
            if r["mode"] == mode
            and r["hit_at_5"] is not None
            and r["case_id"] not in review
        ]
        metrics[mode] = {
            "hits": sum(r["hit_at_5"] for r in valid),
            "eligible_cases": len(valid),
        }
    attempts = []
    for phase in ["live", "live-retry"]:
        for path in sorted((root / phase).glob("*/*.json")):
            row = json.loads(path.read_text())
            if not isinstance(row, dict) or "status" not in row:
                continue
            result = row.get("result", row)
            usage = result.get("usage", [])
            embeddings = result.get("retrieval_usage", [])
            attempts.append(
                {
                    "phase": phase,
                    "case_id": row["case"]["id"],
                    "mode": row["retrieval_mode"],
                    "status": row["status"],
                    "error": row.get("error", {}).get("code"),
                    "total_tokens_reported": sum(
                        x.get("total_tokens", 0) for x in usage
                    ),
                    "prompt_tokens_reported": sum(
                        x.get("prompt_tokens", 0) for x in usage
                    ),
                    "completion_tokens_reported": sum(
                        x.get("completion_tokens", 0) for x in usage
                    ),
                    "cache_hit_tokens_reported": sum(
                        x.get("prompt_cache_hit_tokens", 0) for x in usage
                    ),
                    "wall_seconds": row["wall_seconds"],
                    "model_successful_turns": len(usage),
                    "tool_calls": len(result.get("trace", [])),
                    "retry_count": len(row.get("benchmark_retries", [])),
                    "embedding_tokens_computed": sum(
                        x.get("indexing", {}).get("input_tokens", 0)
                        + x.get("query_embedding", {}).get("input_tokens", 0)
                        for x in embeddings
                    ),
                    "fallback_count": sum(
                        x.get("fallback_reason") == "embedding_unavailable"
                        for x in embeddings
                    ),
                    "file": str(path.relative_to(root)),
                }
            )
    pairs = []
    retry = [r for r in attempts if r["phase"] == "live-retry"]
    for cid in sorted(set(r["case_id"] for r in retry)):
        pair = {r["mode"]: r for r in retry if r["case_id"] == cid}
        if set(pair) == {"keyword", "hybrid"} and all(
            r["status"] == "completed" for r in pair.values()
        ):
            pairs.append(
                {"case_id": cid, "keyword": pair["keyword"], "hybrid": pair["hybrid"]}
            )
    pair_summary = {}
    for mode in ["keyword", "hybrid"]:
        pair_summary[mode] = {
            "completed_pairs": len(pairs),
            "total_tokens_reported": sum(
                p[mode]["total_tokens_reported"] for p in pairs
            ),
            "wall_seconds": round(sum(p[mode]["wall_seconds"] for p in pairs), 3),
        }
    data = {
        "label_review": review,
        "reviewed_retrieval": metrics,
        "attempts": attempts,
        "attempt_statuses": dict(Counter(r["status"] for r in attempts)),
        "failure_codes": dict(Counter(r["error"] for r in attempts if r["error"])),
        "completed_pairs": pairs,
        "paired_totals": pair_summary,
        "limitations": [
            "Small related-source sample, not generalization accuracy.",
            "Only reported model usage; HTTP 503 unreported usage is unknown.",
            "Latency includes benchmark retries; no reliable cost or speed conclusion.",
            "Automated citation validity is not complete semantic correctness.",
        ],
    }
    (root / "reviewed-summary.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                k: data[k]
                for k in [
                    "reviewed_retrieval",
                    "attempt_statuses",
                    "failure_codes",
                    "paired_totals",
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
