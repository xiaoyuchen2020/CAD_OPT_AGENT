"""Validate independently specified field expectations against local real captures."""

import argparse
import json
from pathlib import Path
from scripts.compare_drawings import capture, write, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--expectations",
        type=Path,
        default=Path("tests/fixtures/real_masonry_expectations.json"),
    )
    args = parser.parse_args()
    checks = read(args.expectations)["checks"]
    results = []
    field_count = 0
    for check in checks:
        facts, entities, regions = capture(args.root / "after" / check["sample_id"])
        matches = [
            p
            for p in facts["practices"]
            if check["contains"] in p["original_text"]
            and (
                not check["application_contains"]
                or check["application_contains"]
                in str(p["attributes"]["application_scope"]["value"])
            )
        ]
        failures = []
        if len(matches) != 1:
            failures.append(f"Expected unique clause, got {len(matches)}")
        else:
            p = matches[0]
            for key, wanted in check["expected"].items():
                field_count += 1
                got = p["attributes"][key]["value"]
                if got != wanted:
                    failures.append({"field": key, "expected": wanted, "actual": got})
        results.append(check | {"passed": not failures, "failures": failures})
    result = {
        "checks": len(checks),
        "field_assertions": field_count,
        "passed": all(r["passed"] for r in results),
        "results": results,
    }
    write(args.root / "field-verification.json", result)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "results"}, ensure_ascii=False
        )
    )
    for r in results:
        if not r["passed"]:
            print(json.dumps(r, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
