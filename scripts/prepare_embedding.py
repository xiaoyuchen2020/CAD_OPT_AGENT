"""Explicit download of pinned, safetensors-only local embedding assets."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from cad_agent.config import ROOT
from cad_agent.adapters.embedding import MODEL_ID


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--revision", default="main")
    p.add_argument(
        "--output", type=Path, default=ROOT / ".data/models/bge-small-zh-v1.5"
    )
    a = p.parse_args()
    from huggingface_hub import HfApi, snapshot_download

    revision = HfApi().model_info(MODEL_ID, revision=a.revision).sha
    a.output.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        MODEL_ID,
        revision=revision,
        local_dir=a.output,
        allow_patterns=["*.json", "*.safetensors", "vocab.txt", "1_Pooling/*.json"],
        max_workers=3,
    )
    files = {
        str(f.relative_to(a.output)): sha256(f.read_bytes()).hexdigest()
        for f in a.output.rglob("*")
        if f.is_file() and ".cache" not in f.parts and f.name != "cad-model.json"
    }
    record = {"model": MODEL_ID, "revision": revision, "files": files}
    (a.output / "cad-model.json").write_text(json.dumps(record, indent=2))
    print(json.dumps({"model": MODEL_ID, "revision": revision, "files": len(files)}))


if __name__ == "__main__":
    main()
