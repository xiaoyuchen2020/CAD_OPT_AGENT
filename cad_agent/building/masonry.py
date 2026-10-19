"""Masonry facts only: no thresholds, optimization rules or replacement advice."""

import re
from cad_agent.contracts import MasonryPractice, FactValue, Coverage, stable_id
from cad_agent.evidence import bind_evidence, validate_proposal

EXTRACTOR_VERSION = "building.masonry@1"
EXTRACTOR_REVISION = "masonry-v1.1.3"
MATERIAL = re.compile(
    r"蒸压(?:砂)?加气混凝土砌块|蒸压(?:砂)?加气砌块|加气\s*混凝土砌块|蒸压加气混凝土|砂加气砌块|机制实心水泥砖|(?:水泥|混凝土)实心砖|混凝土多孔砖|(?:非[粘黏]土类)?烧结(?:粉煤灰|页岩)?(?:多孔|空心)?砖|页岩多孔砖|混凝土(?:空心)?砌块|多孔砖|粉煤灰砖|砌块"
)
SPECIAL = (
    "卫生间",
    "厨房",
    "有水房间",
    "湿房间",
    "水泵房",
    "设备房",
    "设备机房",
    "管道井",
    "电梯井",
    "楼梯间",
    "分户墙",
    "防火墙",
    "墙脚",
    "墙体底部",
    "内墙底部",
    "女儿墙",
)


def material_matches(text):
    matches = []
    for m in MATERIAL.finditer(text):
        # References to substrate inside finishing specifications are not masonry facts.
        tail = text[m.end() : m.end() + 8]
        if re.match(r"\s*(?:专用)?界面剂", tail):
            continue
        matches.append(m)
    return matches


def elevation_values(text):
    values = []
    # The reference level and direction must belong to the same clause.
    if re.search(
        r"(?<!非)地下(?:室)?|地坪以下|(?:±0\.0+|(?:室外|室内)?地坪)(?:标高)?(?:或建筑室内地坪标高)?以下|负[一二三123]层",
        text,
    ):
        values.append("below_ground")
    if re.search(
        r"地上|(?:±0\.0+|(?:室外|室内)?地坪)(?:标高)?(?:或建筑室内地坪标高)?以上", text
    ):
        values.append("above_ground")
    return values


def is_scope_heading(text):
    text = re.sub(r"[\s|]+", "", text)
    return len(text) < 110 and bool(
        re.search(r"(?:墙体|砌体).*[:：]$|(?:墙体|砌体)(?:工程|材料|做法)$", text)
    )


def local_spans(region):
    text = region.text
    # Soft line breaks never terminate a clause. A semicolon starts a new
    # practice only when the next subject explicitly names a material; otherwise
    # preserve the shared subject and mark role-specific values for review.
    cuts = [0]
    for m in re.finditer(r"[；;]", text):
        tail = text[m.end() :]
        segment = re.split(r"[；;]", tail, maxsplit=1)[0]
        if material_matches(segment) or not re.search(
            r"(?:内墙|外墙).*?(?:B\d{2}|A\d)", segment
        ):
            cuts.append(m.end())
    cuts.append(len(text))
    for a, b in zip(cuts, cuts[1:]):
        snippet = text[a:b]
        if material_matches(snippet):
            left = len(snippet) - len(snippet.lstrip())
            right = len(snippet.rstrip("；; \n"))
            yield a + left, a + right


def value(values, evidence, derivation=None):
    values = list(dict.fromkeys(values))
    if not values:
        return FactValue()
    return FactValue(
        value=values[0] if len(values) == 1 else values,
        state=(
            "conflict" if len(values) > 1 else ("derived" if derivation else "observed")
        ),
        evidence_ids=evidence,
        derivation=derivation,
    )


def build_practice(snapshot_id, source, start, end, region_map, unit):
    quote = source.text[start:end]
    materials = [re.sub(r"\s+", "", m.group()) for m in material_matches(quote)]
    if "砌块" in materials and any(m != "砌块" for m in materials):
        materials = [m for m in materials if m != "砌块"]
    if not materials:
        return None, []
    ev = bind_evidence(snapshot_id, source, start, end, unit)
    evidence = [ev]
    eid = [ev.evidence_id]
    # Region separators are presentation-only. Numeric parsing uses a normalized
    # view while evidence keeps the original joined text and native entity IDs.
    compact = re.sub(r"\s+", "", quote).replace("|", " ")
    # Keep column boundaries between grade and thickness (A3.5 | 200), while
    # joining split thickness expressions such as 200 | 或 | 240 | 厚.
    compact = re.sub(r"\s*(或|、|/|厚)\s*", r"\1", compact)
    roles = []
    if re.search(r"内墙|内隔墙|内分隔墙|分户墙", compact):
        roles.append("inner")
    if re.search(r"外墙|外围护", compact):
        roles.append("outer")
    subject = value(roles, eid)
    density = value(re.findall(r"(?<![A-Za-z0-9])B\d{2}(?![\d.])", compact, re.I), eid)
    strength = value(
        re.findall(
            r"(?<![A-Za-z])(?:A\d+(?:\.\d+)?|MU\d+(?:\.\d+)?)(?![\d.])", compact, re.I
        ),
        eid,
    )
    # Finishing layers are a separate construction phase, never wall thickness
    # or masonry mortar. The complete source quote remains available.
    masonry_text = re.split(r"粉刷|抹灰|[+＋]|在?两侧|饰面|下设", compact, maxsplit=1)[
        0
    ]
    mortar = value(
        re.findall(
            r"(?<![A-Za-z0-9-])(?:DM[-—]?)?(M(?:a|b)?\d+(?:\.\d+)?)(?![\d.])",
            masonry_text,
            re.I,
        ),
        eid,
    )
    thickness = []
    for match in re.finditer(
        r"(?<![\d.])(\d+(?:(?:/|或|、)\d+)*)(?:mm|毫米)?厚|厚(?:度)?(?:为|：|:)?(\d+(?:(?:/|或|、)\d+)*)(?:mm|毫米)",
        masonry_text,
        re.I,
    ):
        thickness.extend(
            int(n) for n in re.split(r"/|或|、", match.group(1) or match.group(2))
        )
    elevations = elevation_values(compact)
    elevation = value(elevations, eid)
    uses = [s for s in SPECIAL if s in compact]
    heading_scopes = []
    # Only explicit nearby heading scopes. Building nature does not imply thermal requirements.
    contexts = [
        region_map[x]
        for x in source.context_region_ids
        if x in region_map
        and region_map[x].frame_id == source.frame_id
        and region_map[x].layout_id == source.layout_id
    ]
    for ctx in contexts:
        if ctx.layout_id != source.layout_id or ctx.frame_id != source.frame_id:
            continue
        ct = re.sub(r"[\s|]+", "", ctx.text)
        scopes = elevation_values(ct) if is_scope_heading(ct) else []
        context_scope = scopes[0] if len(scopes) == 1 else None
        if elevation.state == "unknown" and context_scope:
            ce = bind_evidence(snapshot_id, ctx, 0, len(ctx.text), unit)
            heading_scopes.append((context_scope, ce))
    if heading_scopes:
        evidence.extend(e for _, e in heading_scopes)
        elevation = value(
            [scope for scope, _ in heading_scopes],
            [e.evidence_id for _, e in heading_scopes],
            "explicit_nearby_heading.v1",
        )
    application = []
    scope_eids = []
    table_roles = []
    table_uses = []
    table_use_eids = []
    for ctx in contexts:
        ct = re.sub(r"\s+", "", ctx.text)
        if is_scope_heading(ct) and re.search(r"住宅|公建|设备用房|除.*外", ct):
            ref = bind_evidence(snapshot_id, ctx, 0, len(ctx.text), unit)
            evidence.append(ref)
            application.append(ct)
            scope_eids.append(ref.evidence_id)
        if source.table_row_key and ctx.table_row_key == source.table_row_key:
            if re.fullmatch(r"分户墙|内墙|内隔墙", ct):
                table_roles.append("inner")
            elif ct == "外墙":
                table_roles.append("outer")
            else:
                continue
            ref = bind_evidence(snapshot_id, ctx, 0, len(ctx.text), unit)
            evidence.append(ref)
            scope_eids.append(ref.evidence_id)
            if ct in SPECIAL:
                table_uses.append(ct)
                table_use_eids.append(ref.evidence_id)
    if not roles and table_roles:
        subject = value(table_roles, scope_eids, "explicit_table_row_label.v1")
    conflicts = []
    if "其余" in compact:
        # In-region preceding text is stronger than proximity across native entities.
        prefix = source.text[:start]
        remainder_contexts = []
        if re.search(r"内墙.*(?:有水房间|卫生间|厨房)", prefix) and len(prefix) < 600:
            remainder_contexts = [(source, 0, start)]
        else:
            remainder_contexts = [
                (c, 0, len(c.text))
                for c in contexts
                if re.search(r"内墙.*(?:有水房间|卫生间|厨房)", c.text)
            ]
        if len(remainder_contexts) == 1:
            ctx, cs, ce = remainder_contexts[0]
            ref = bind_evidence(snapshot_id, ctx, cs, ce, unit)
            evidence.append(ref)
            subject = FactValue(
                value="inner",
                state="derived",
                evidence_ids=eid + [ref.evidence_id],
                derivation="explicit_remainder_subject.v1",
            )
        else:
            conflicts.append("unresolved_remainder_scope")
    thermal = value(
        (
            ["not_required"]
            if re.search(r"无保温|非保温", compact)
            else (["required"] if "有保温要求" in compact else [])
        ),
        eid,
    )
    statement_kind = "material_mention"
    if re.search(r"(?:不得|禁止|严禁|不应)(?:采用|使用|选用)|不采用", compact):
        statement_kind = "prohibition"
    elif (
        "《" in quote
        or re.search(r"参见|参照|详见|详[图表]", compact)
        and not re.search(r"墙.*采用", compact)
    ):
        statement_kind = "reference"
    elif re.search(
        r"当.*(?:砌块|砖)|(?:砌块|砖)(?:墙体)?时|砌筑各种|灰缝|梁底|墙顶|电钻|过梁|交接处|抹灰|粉刷|网格布|钢丝网|钢板网|技术要求|性能指标",
        compact,
    ):
        statement_kind = "material_mention"
    elif re.search(r"采用|选用|砌筑|材料为", compact) or (
        source.kind == "table_row" and (roles or table_roles)
    ):
        statement_kind = "specification"
    # Finishing words following a complete explicit wall selection do not turn
    # that selection into a mere mention.
    if statement_kind == "material_mention" and re.search(
        r"(?:内墙|外墙|分户墙|墙体|女儿墙)(?:底部|砌体)?(?:均)?采用[^。]*?(?:实心砖|多孔砖|加气混凝土砌块)(?!墙体时)",
        masonry_text,
    ):
        statement_kind = "specification"
    if materials == ["砌块"] and re.search(r"采用C\d+|专用.*粘结剂", compact):
        statement_kind = "material_mention"
    if re.search(r"或(?:PC|现浇|钢筋)|(?:PC|钢筋混凝土).*或", compact):
        conflicts.append("material_alternatives_require_review")
    if len(roles) > 1:
        conflicts.append("role_specific_values_require_review")
    if statement_kind in ("material_mention", "reference"):
        conflicts.append("unconfirmed_material_application")
    if statement_kind in ("reference", "material_mention"):
        # Numeric values in technical/finishing clauses are not selected wall
        # parameters (page A58, mesh dimensions, interface coat thickness, etc.).
        thickness = []
        mortar, strength, density = FactValue(), FactValue(), FactValue()
    # An equipment recess can specify the host wall, box depth and infill.
    # Only the explicit masonry infill thickness belongs to this practice.
    if re.search(r"栓箱|箱体", compact):
        infill = re.search(r"砌筑(\d+)厚(?:加气)?混凝土砌块", compact)
        thickness = [int(infill.group(1))] if infill else []
    role_parameters = {}
    if len(roles) > 1:
        for m in re.finditer(r"(外墙|内墙)(.*?)(?=外墙|内墙|$)", compact):
            role = "outer" if m.group(1) == "外墙" else "inner"
            segment = m.group(2)
            parameters = role_parameters.setdefault(role, {})
            for key, pattern in (
                ("density_grade", r"B\d{2}"),
                ("strength_grade", r"A\d+(?:\.\d+)?"),
            ):
                found = re.findall(pattern, segment)
                if found:
                    parameters[key] = list(
                        dict.fromkeys(parameters.get(key, []) + found)
                    )
            numbers = re.findall(r"(\d+)厚", segment)
            if numbers:
                parameters["thickness_mm"] = sorted(set(map(int, numbers)))
    attrs = {
        "statement_kind": value(
            [statement_kind], eid, "explicit_wording_classifier.v1"
        ),
        "material": value(materials, eid),
        "density_grade": density,
        "strength_grade": strength,
        "wall_role": subject,
        "elevation_scope": elevation,
        "thickness_mm": FactValue(
            value=sorted(set(thickness)) or None,
            state=(
                ("conflict" if len(roles) > 1 else "observed")
                if thickness
                else "unknown"
            ),
            evidence_ids=eid if thickness else [],
        ),
        "mortar_grade": mortar,
        "special_uses": FactValue(
            value=list(dict.fromkeys(uses + table_uses)),
            state="observed" if uses else ("derived" if table_uses else "unknown"),
            evidence_ids=(eid if uses else []) + table_use_eids,
            derivation="explicit_table_row_label.v1" if table_uses else None,
        ),
        "thermal_requirement": thermal,
        "role_parameters": FactValue(
            value=role_parameters or None,
            state="observed" if role_parameters else "unknown",
            evidence_ids=eid if role_parameters else [],
        ),
        "application_scope": value(
            application, scope_eids, "explicit_section_heading.v1"
        ),
        "scope_qualifiers": value(
            re.findall(
                r"（非[^）]+）|（地下室[^）]+）|除[^，。；]+?外",
                compact
                + "".join(
                    re.sub(r"\s+", "", c.text)
                    for c in contexts
                    if is_scope_heading(c.text)
                ),
            ),
            list(dict.fromkeys(e.evidence_id for e in evidence)),
        ),
    }
    if attrs["scope_qualifiers"].state != "unknown":
        attrs["scope_qualifiers"].state = "observed"
    missing = [
        k
        for k in ("wall_role", "elevation_scope", "strength_grade")
        if attrs[k].state == "unknown"
    ]
    if any("加气" in m for m in materials) and density.state == "unknown":
        missing.append("density_grade")
    conflicts.extend(k for k, v in attrs.items() if v.state == "conflict")
    practice = MasonryPractice(
        practice_id=stable_id(
            "practice", snapshot_id, source.region_id, str(start), str(end)
        ),
        snapshot_id=snapshot_id,
        source_occurrence_id=stable_id(
            "occ", snapshot_id, source.region_id, str(start), str(end)
        ),
        source_region_id=source.region_id,
        layout_id=source.layout_id,
        frame_id=source.frame_id,
        frame_name=source.frame_name,
        original_text=quote,
        attributes=attrs,
        evidence_ids=list(dict.fromkeys(e.evidence_id for e in evidence)),
        missing_fields=missing,
        conflicts=conflicts,
        review_status="needs_review" if missing or conflicts else "source_bound",
    )
    return practice, evidence


def extract(snapshot_id, regions, unit, proposals=None):
    region_map = {r.region_id: r for r in regions}
    spans = []
    diagnostics = []
    candidate_ids = set()
    for r in regions:
        found = list(local_spans(r))
        if found:
            candidate_ids.add(r.region_id)
        spans.extend((r, a, b) for a, b in found)
    mode = "local"
    if proposals is not None:
        mode = "model_with_local_coverage"
        accepted = []
        for proposal in proposals:
            try:
                r, a, b = validate_proposal(proposal, region_map)
                if not material_matches(r.text[a:b]):
                    raise ValueError("Not masonry")
                accepted.append((r, a, b))
            except ValueError:
                diagnostics.append(
                    {
                        "code": "MODEL_UNGROUNDED_ITEM",
                        "message": "模型的一条来源引用未通过原文校验，已拒绝。",
                    }
                )
        # Validate coverage at clause granularity. A quote from one paragraph must
        # never suppress its other clauses or discard adjacent scope/grade text.
        covered = {
            (r.region_id, a, b)
            for r, a, b in spans
            if any(
                ar.region_id == r.region_id and aa <= a and ab >= b
                for ar, aa, ab in accepted
            )
        }
        omitted = {(r.region_id, a, b) for r, a, b in spans} - covered
        if omitted:
            diagnostics.append(
                {
                    "code": "MODEL_OMITTED_CANDIDATES",
                    "message": f"模型未完整覆盖 {len(omitted)} 条本地材料候选原文，已保留并待复核。",
                }
            )
        # Native clause boundaries remain canonical; model proposals cannot alter
        # the fact set simply by dropping words from a grounded quote.
    practices, evs, seen = [], {}, set()
    for r, a, b in spans:
        key = (r.region_id, a, b)
        if key in seen:
            continue
        seen.add(key)
        p, evidence = build_practice(snapshot_id, r, a, b, region_map, unit)
        if p:
            practices.append(p)
            evs.update((e.evidence_id, e) for e in evidence)
    unresolved = sorted(
        {p.source_region_id for p in practices if p.review_status == "needs_review"}
    )
    coverage = Coverage(
        scope=sorted({r.frame_id for r in regions}),
        source_units_seen=len(regions),
        source_units_resolved=len({p.source_region_id for p in practices}),
        candidate_units=len(candidate_ids),
        unresolved_region_ids=unresolved,
        skipped_region_ids=[
            r.region_id for r in regions if r.region_id not in candidate_ids
        ],
        completeness="partial" if unresolved or diagnostics else "bounded",
    )
    return practices, list(evs.values()), coverage, diagnostics, mode
