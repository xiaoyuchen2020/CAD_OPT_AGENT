"""Bounded document overview from existing source regions, without masonry parsing."""

import re

from cad_agent.evidence import bind_evidence


TITLE = re.compile(
    r"(?:设计总说明|设计说明|施工说明|平面图|立面图|剖面图|详图|节点图|系统图|"
    r"材料表|做法表|图纸目录)[一二三四五六七八九十\d（）()、.\-\s]*$"
)


def drawing_overview(workspace, drawing, layout_id="", frame_id="", offset=0, limit=12):
    candidates = {}
    for region in workspace.regions(drawing["snapshot_id"]):
        if layout_id and region.layout_id != layout_id:
            continue
        if frame_id and region.frame_id != frame_id:
            continue
        title = region.text.strip()
        # Short standalone titles only; prose and references are not title blocks.
        if (
            not title
            or len(title) > 80
            or "\n" in title
            or any(mark in title for mark in ("《", "》", "。", "；", ";"))
        ):
            continue
        normalized = re.sub(r"\s+", "", title)
        if TITLE.search(normalized):
            candidates.setdefault(normalized, []).append(region)
    ordered = sorted(
        candidates.values(),
        key=lambda rs: ("总说明" not in rs[0].text, len(rs[0].text), rs[0].text),
    )
    items = []
    for occurrences in ordered[offset : offset + limit]:
        # Representative source, not a merged location or a sheet-count inference.
        region = occurrences[0]
        evidence = bind_evidence(
            drawing["snapshot_id"], region, 0, len(region.text), drawing["unit"]
        ).model_dump()
        items.append(
            {
                "region_id": region.region_id,
                "title_candidate": region.text,
                "occurrence_count": len(occurrences),
                "evidence": evidence,
            }
        )
    return {
        "file_metadata": {
            "name": drawing["name"],
            "source_format": drawing["source_format"],
            "layout_names": [layout["name"] for layout in drawing["layouts"][:40]],
            "layout_count": len(drawing["layouts"]),
        },
        "items": items,
        "total": len(ordered),
        "has_more": offset + len(items) < len(ordered),
        "offset": offset,
        "note": "文件名和布局数量是导入元数据，不是原图标题或图纸张数。items 是短标题候选的代表原文位置，可能来自图签、目录或重复绘制；不据此确认独立图纸数量。无候选不代表原图无标题。可直接引用 evidence；需要确认具体位置时读取上下文。",
    }
