"""Independent checks on saved component output; no recognizer or model calls."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("directory",type=Path)
    args=parser.parse_args();root=args.directory
    checks=[]
    for path in sorted(root.glob("*-dimensions.json")):
        record=json.loads(path.read_text());paper=path.name.split("-")[0]
        for item in record["dimensions"]:
            if "native_and_recomputed_measurement_conflict" not in item["diagnostics"]:
                continue
            values=[float(t) for t in item["rendered_texts"] if re.fullmatch(r"[+-]?\d+(?:\.\d+)?",t)]
            expected=item["geometry_recomputed"]*item["style"]["dimlfac"]
            tolerance=.5*10**(-item["style"]["dimdec"])
            matches=any(math.isclose(v,expected,abs_tol=tolerance,rel_tol=1e-7) for v in values)
            checks.append(dict(paper_id=paper,entity_id=item["object_id"],parser_value=item["actual_measurement"],
                rendered_texts=item["rendered_texts"],geometry_recomputed=item["geometry_recomputed"],
                rounding_tolerance=tolerance,display_matches_recomputed_with_style=matches))
    result=dict(method="Compare numeric native dimension-block display text against definition-point recomputation with dimlfac and dimdec rounding; no source modification",
        conflict_count=len(checks),display_matches_recomputed=sum(c["display_matches_recomputed_with_style"] for c in checks),
        conflicts_by_paper=dict(Counter(c["paper_id"] for c in checks)),checks=checks,
        note="Agreement supports the recomputation for this sample; it does not establish design correctness or justify silently replacing parser values.")
    (root/"dimension-display-verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="checks"},ensure_ascii=False))


if __name__=="__main__":main()
