"""Explicit live smoke check: synthetic text only, no customer drawings."""

import json
from pathlib import Path
from cad_agent.config import Settings
from cad_agent.contracts import SourceRegion
from cad_agent.adapters.model import ModelGateway, ModelError


def main():
    settings = Settings.load()
    source = SourceRegion(
        region_id="synthetic-model-check",
        layout_id="demo",
        frame_id="demo",
        frame_name="合成连通性测试",
        kind="text",
        text="地上内墙采用B06级A3.5蒸压加气混凝土砌块。",
        entity_ids=["demo"],
        bbox=[0, 0, 100, 10],
    )
    result = {
        "model": settings.model,
        "protocol": settings.protocol,
        "synthetic_only": True,
    }
    try:
        proposals, usage = ModelGateway(settings).propose([source])
        from cad_agent.evidence import validate_proposal

        if not proposals:
            raise ModelError("MODEL_NO_PROPOSALS", "合成候选未返回来源引用。")
        for p in proposals:
            validate_proposal(p, {source.region_id: source})
        result.update(status="success", grounded_items=len(proposals), usage=usage)
    except ModelError as e:
        result.update(status="failed", code=e.code, message=e.message)
    except ValueError:
        result.update(
            status="failed", code="UNGROUNDED_QUOTE", message="模型引用未匹配合成原文。"
        )
    output = Path("artifacts/model-check.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
