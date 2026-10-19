"""Read an existing DXF and produce a local-only compatibility report."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from time import monotonic
from cad_agent.config import Settings
from cad_agent.contracts import ExtractionRequest
from cad_agent.service import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drawing", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/drawing-check"))
    args = parser.parse_args()
    raw = args.drawing.read_bytes()
    digest = sha256(raw).hexdigest()
    started = monotonic()
    # Explicit empty model configuration: this command can never transmit text.
    workspace = Workspace(Settings(data_dir=args.output / "data"))
    try:
        drawing = workspace.ingest(args.drawing.name, raw)
        job = workspace.start_extract(ExtractionRequest(snapshot_id=drawing["id"]))
    finally:
        workspace.close()
    job = workspace.store.get("job", job["id"])
    facts = (
        workspace.store.get("fact_set", job["result"]["fact_set_id"])
        if job["result"]
        else None
    )
    summary = {
        "source_path": str(args.drawing.resolve()),
        "input_sha256": digest,
        "source_unchanged": digest == sha256(args.drawing.read_bytes()).hexdigest(),
        "elapsed_seconds": round(monotonic() - started, 2),
        "text_count": drawing["text_count"],
        "regions": drawing["region_count"],
        "frames": len(drawing["frames"]),
        "diagnostics": drawing["diagnostics"],
        "job": job["status"],
        "facts": len(facts["practices"]) if facts else 0,
        "review_count": (
            sum(p["review_status"] == "needs_review" for p in facts["practices"])
            if facts
            else 0
        ),
        "statement_kinds": {},
        "local_only": True,
        "formal_acceptance": False,
    }
    if facts:
        for p in facts["practices"]:
            kind = p["attributes"]["statement_kind"]["value"]
            summary["statement_kinds"][kind] = (
                summary["statement_kinds"].get(kind, 0) + 1
            )
        (args.output / "facts.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=2)
        )
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if job["status"] == "failed" or not summary["source_unchanged"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
