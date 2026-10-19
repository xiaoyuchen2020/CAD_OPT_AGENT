"""Deterministic source-grounded fact queries, without engineering policies."""

from cad_agent.building.masonry import SPECIAL


def as_list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def queried_values(practice, field, roles):
    per_role = practice["attributes"].get("role_parameters", {}).get("value")
    if (
        roles
        and per_role
        and field in ("density_grade", "strength_grade", "thickness_mm")
    ):
        return [v for role in roles for v in as_list(per_role.get(role, {}).get(field))]
    return as_list(practice["attributes"][field]["value"])


def answer_facts(facts, request):
    items = facts["practices"]
    q = request.question
    if request.selected_practice_id:
        items = [p for p in items if p["practice_id"] == request.selected_practice_id]
        if not items:
            raise ValueError("选中的做法不属于当前事实集合。")
    if any(k in q for k in ("优化", "建议替换", "能否替换", "省钱", "合规", "违规")):
        return {
            "answer": "当前工作台只提供图纸事实与原文证据，尚未接入优化或合规策略。可以查询材料、等级、厚度和适用范围。",
            "citations": [],
            "mode": "grounded_query",
        }
    if not request.selected_practice_id:
        if any(
            k in q for k in ("除了", "除外", "不是", "不使用", "大于", "小于", "相比")
        ):
            return {
                "answer": "当前查询尚不支持排除或比较条件。请用明确的材料、墙体类型、部位或等级筛选，再核对原文。",
                "citations": [],
                "mode": "grounded_query",
            }
        roles = [v for k, v in (("内墙", "inner"), ("外墙", "outer")) if k in q]
        if roles:
            items = [
                p
                for p in items
                if any(
                    v in roles for v in as_list(p["attributes"]["wall_role"]["value"])
                )
            ]
        scopes = [
            v for k, v in (("地下", "below_ground"), ("地上", "above_ground")) if k in q
        ]
        if scopes:
            items = [
                p
                for p in items
                if p["attributes"]["elevation_scope"]["value"] in scopes
            ]
        uses = [s for s in SPECIAL if s in q]
        if uses:
            items = [
                p
                for p in items
                if any(s in p["attributes"]["special_uses"]["value"] for s in uses)
            ]
        if any(k in q for k in ("缺失", "待核查", "不完整")):
            items = [p for p in items if p["review_status"] == "needs_review"]
        import re

        for field, pattern in (
            ("density_grade", r"B\d{2}"),
            ("strength_grade", r"A\d+(?:\.\d+)?|MU\d+(?:\.\d+)?"),
            ("mortar_grade", r"(?<![A-Za-z])M(?:a|b)?\d+(?:\.\d+)?"),
            ("material", r"实心砖|多孔砖|加气"),
        ):
            tokens = re.findall(pattern, q, re.I)
            if tokens:
                items = [
                    p
                    for p in items
                    if any(
                        (
                            token.lower() in str(v).lower()
                            if field == "material"
                            else token.lower() == str(v).lower()
                        )
                        for token in tokens
                        for v in queried_values(p, field, roles)
                    )
                ]
        # Preserve exclusions in section headings: "除住宅外" cannot match a
        # residential-only query just because it contains the word 住宅.
        if any(k in q for k in ("住宅", "公建", "设备用房")):

            def matches_application(p):
                scope = str(
                    p["attributes"].get("application_scope", {}).get("value") or ""
                )
                if "公建" in q or "设备用房" in q:
                    return any(k in scope and k in q for k in ("公建", "设备用房"))
                return "住宅" in scope and "除住宅外" not in scope

            items = [p for p in items if matches_application(p)]
        thicknesses = [
            int(a or b)
            for a, b in re.findall(
                r"(\d+)\s*(?:mm|毫米)?\s*厚|厚度\s*(?:为|是|等于|[:：])?\s*(\d+)",
                q,
                re.I,
            )
        ]
        if thicknesses:
            items = [
                p
                for p in items
                if any(
                    n in queried_values(p, "thickness_mm", roles) for n in thicknesses
                )
            ]
        if any(k in q for k in ("禁止", "禁用", "不得采用")):
            items = [
                p
                for p in items
                if p["attributes"]["statement_kind"]["value"] == "prohibition"
            ]
    supported = any(
        k in q
        for k in (
            "材料",
            "砌体",
            "做法",
            "内墙",
            "外墙",
            "地上",
            "地下",
            "证据",
            "来源",
            "原文",
            "这条",
            "等级",
            "厚度",
            "厚",
            "缺失",
            "待核查",
            "不完整",
            "B0",
            "总结",
            "列出",
        )
    )
    if not supported and not request.selected_practice_id:
        return {
            "answer": "当前支持材料、等级、厚度、适用范围和来源查询。请指定墙体类型或材料，例如“列出地上内墙材料”。",
            "citations": [],
            "mode": "grounded_query",
        }
    if not items:
        return {
            "answer": "当前事实集合中没有匹配记录。这不代表原图不存在相关做法；请结合覆盖诊断或搜索原文核查。",
            "citations": [],
            "mode": "grounded_query",
        }
    shown = items[:12]
    citations = [
        {
            "practice_id": p["practice_id"],
            "evidence_id": p["evidence_ids"][0],
            "label": p["frame_name"],
            "quote": p["original_text"],
        }
        for p in shown
    ]
    answer = f"在当前事实集合中找到 {len(items)} 条匹配做法。以下内容均绑定原图来源："
    return {
        "answer": answer,
        "citations": citations,
        "mode": "grounded_query",
        "partial": len(items) > 12,
        "note": "这是一组设计做法记录，不是物理墙体数量。未标明的范围与等级不做推断。",
    }
