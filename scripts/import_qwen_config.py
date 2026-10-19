"""Copy only Qwen endpoint configuration into this repository's private .env."""

import argparse
import json
from pathlib import Path
from dotenv import dotenv_values
from cad_agent.config import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = dotenv_values(args.source)
    small_key = source.get("SMALL_MODEL_API_KEY") or ""
    long_key = source.get("LLM_API_KEY") or source.get("OPENAI_API_KEY") or small_key
    values = {}
    for tier, endpoint, length, model, key in [
        ("LARGE", "MODEL_HOST", "MODEL_CONTEXT_LENGTH", "MODEL_NAME", long_key),
        (
            "MEDIUM",
            "SMALL_MODEL_CHAT_BASE_URL",
            "SMALL_MODEL_CHAT_CONTEXT_LENGTH",
            "SMALL_MODEL_NAME",
            small_key,
        ),
        (
            "SMALL",
            "SMALL_MODEL_EXTRACTION_BASE_URL",
            "SMALL_MODEL_EXTRACTION_CONTEXT_LENGTH",
            "SMALL_MODEL_NAME",
            small_key,
        ),
    ]:
        prefix = "CAD_QWEN_" + tier + "_"
        values.update(
            {
                prefix + "BASE_URL": source.get(endpoint) or "",
                prefix + "MODEL": source.get(model) or "",
                prefix + "API_KEY": key,
                prefix + "CONTEXT_LENGTH": source.get(length) or "262144",
            }
        )
    if not all(values.values()):
        raise SystemExit("Qwen 配置缺失，未写入；请核对来源变量名。")
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    path.write_text("\n".join(lines + [f"{k}={v}" for k, v in values.items()]) + "\n")
    path.chmod(0o600)
    print(
        json.dumps(
            {
                "imported_profiles": ["qwen_large", "qwen_medium", "qwen_small"],
                "destination": str(path),
                "source_unchanged": True,
            }
        )
    )


if __name__ == "__main__":
    main()
