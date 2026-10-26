from cad_agent.cad.numbers import unsupported_values, format_claim_text, readings, VERSION as NUMBER_VERSION
"""Source, numerical and per-requirement answer validation."""
import json
import re
from cad_agent.analysis.boundary_context import numeric_evidence
from cad_agent.analysis.grounding import numbers, contains_number, contains_numeric_phrase, verify_calculations, without_claim_list_markers
from cad_agent.analysis.fact_tools import user_values
from cad_agent.analysis.assembly_delivery import ownership_issues
from cad_agent.analysis.spatial_delivery import is_spatial_fact, spatial_claim_issues, spatial_numeric_evidence, spatial_measurement_issues, spatial_numeric_claim_text
from cad_agent.analysis.property_evidence import collect_property_evidence, property_claim_issues

from cad_agent.analysis.tool_registry import FinalAnswer
from cad_agent.analysis.answer_protocol import inspect_answer, explicit_user_conditions, repair_targets, delivery_consistency_issues


class AnswerValidationError(ValueError):
    """Rejected delivery with independently checked clauses for bounded repair."""

    def __init__(self, issues, validated_claims, attempted_claims, *, field_issues=(), validated_user_inputs=(), targets=()):
        super().__init__("\n".join(issues))
        self.validated_claims = validated_claims
        self.attempted_claims = attempted_claims
        self.field_issues = list(field_issues)
        self.repair_targets = list(targets)
        self.validated_user_inputs = list(validated_user_inputs)


def validate_answer(self, content):
    from cad_agent.analysis.source_record_answer import permitted
    parts = inspect_answer(content, self.request.question, record_delivery=permitted(self, content))
    parsed = parts.answer
    if len(self.task.parts)==1:
        for claim in parsed.claims:
            if not claim.question_part_ids:claim.question_part_ids=[self.task.parts[0]['id']]
    # A top-level reference registry is accepted, but each numerical claim
    # must still explicitly identify user conditions and be derived.
    if parsed.user_inputs:
        user_values(parsed.user_inputs,self.request.question,'derived','用户给定条件')
    for claim in parsed.claims:
        if claim.state in ('derived', 'unknown') and re.search('用户|题设|给定|假设|参考|条件',claim.text):
            own={(r.quote,r.value) for r in claim.user_inputs}
            claim.user_inputs.extend(r for r in parsed.user_inputs if contains_numeric_phrase(claim.text, r.value) and (r.quote,r.value) not in own)
    claims, citations, positions = [], [], {}
    issues = [f"{e['path']} [{e['code']}]：{e['message']}" for e in parts.field_issues]
    from cad_agent.analysis.source_candidates import delivery_issues
    issues.extend(delivery_issues(self, parsed))
    validated_claims = []
    outcomes={v.requirement_id:v for v in parsed.requirement_outcomes}
    if len(outcomes)!=len(parsed.requirement_outcomes) or not set(outcomes)<={s["id"] for s in self.task.data["subtasks"]}:
        issues.append("requirement_outcomes 含重复或不存在的要求。")
    if self.task.data.get("plan_origin")=="model" and any(s.get("operations") for s in self.task.data["subtasks"]):
        if set(outcomes)!={s["id"] for s in self.task.data["subtasks"]}:issues.append("复杂计划须用requirement_outcomes逐项声明fulfilled/partial/unresolved；范围未确认或部分操作未完成不能标fulfilled。")
    required = {p["id"] for p in self.coverage.parts}
    addressed = {p for c in parsed.claims for p in c.question_part_ids}
    delivered={s['id'] for s in self.task.data['subtasks'] if s['delivered'] and (s['batch_ids'] or self.request.resume_task_id)}
    if required - addressed - delivered:
        issues.append("未覆盖问题分项：" + ",".join(sorted(required - addressed)) + "；补充回答或明确 unknown，并设置 question_part_ids。")
    if addressed - required:
        issues.append("question_part_ids 包含不存在的分项。")
    used_observations = {}
    property_fields, property_diagnostics = collect_property_evidence(
        self.evidence, self.observations, self.allowed_snapshots)
    for parsed_index, claim in enumerate(parsed.claims):
        index = parts.claim_indexes[parsed_index] + 1
        issue_count = len(issues)
        from cad_agent.analysis.source_candidates import claim_issues
        issues.extend(claim_issues(self, claim))
        issues.extend(f"第 {index} 条{message}" for message in delivery_consistency_issues(claim, self.evidence, self.observations))
        issues.extend(f"第 {index} 条{message}" for message in ownership_issues(claim, self.evidence, self.observations))
        issues.extend(f"第 {index} 条{message}" for message in property_claim_issues(claim, property_fields))
        ids = list(dict.fromkeys(claim.evidence_ids))
        # A source can itself contain design instructions or "同上". Keep
        # literal source reporting, but do not turn derived prose into a
        # design proposal or a clause dependent on an omitted predecessor.
        literal_source = (claim.evidence_basis == 'source' and bool(ids)
            and any(claim.text in self.evidence.get(eid, {}).get('quote', '') for eid in ids))
        if claim.state != 'unknown' and not literal_source:
            if re.search(r'上述|前述|前文|同上|如前所述|见上文', claim.text):
                issues.append(f'第 {index} 条依赖其他结论：请完整重述对象、数值和依据，不能用上述/前述/同上；部分发布时前提可能被删除。')
            proposals = re.finditer(r'(?:建议|拟|可考虑|应当|可以)(?:将[^。；\n]{0,30})?(?:取消|减薄|替换|缩小|拉直|拆除)', claim.text)
            if any(not re.search(r'(?:不|不能|不得|无法|未|不应)$', claim.text[max(0,m.start()-4):m.start()]) for m in proposals):
                issues.append(f'第 {index} 条超出图纸事实分析定位：删除拟取消、替换、减薄等设计方案；只保留原构造、可核验算术及资料缺口。供人工复核不构成例外。')
        try:
            calculated_values = verify_calculations(claim, self.evidence, self.observations) | user_values(claim.user_inputs,self.request.question,claim.state,claim.text)
        except ValueError as error:
            issues.append(f"第 {index} 条：{error}")
            calculated_values = set()
        if any(
            eid not in ids
            for group in claim.evidence_roles.values()
            for eid in group
        ):
            issues.append(f"第 {index} 条证据角色包含未列入 evidence_ids 的来源。")
        if claim.evidence_basis == "tool_observation":
            if not claim.observation_ids or any(
                oid not in self.observations for oid in claim.observation_ids
            ):
                issues.append(
                    f"第 {index} 条缺少已读取的工具观测；只能使用这些ID及对应工具："
                    + json.dumps(
                        {o: self.observations[o]["tool"] for o in self.observations}
                    )
                )
                continue
            if ids:
                issues.append(
                    f"第 {index} 条工具观测不能混用原文引用，请拆分结论。"
                )
            obs = [self.observations[oid] for oid in claim.observation_ids]
            for observation in obs:
                issues.extend(f"第 {index} 条{message}" for message in spatial_claim_issues(claim, observation['data']))
            spatial_data = [o['data'] for o in obs if is_spatial_fact(o['data'])]
            if spatial_data:
                issues.extend(f"第 {index} 条{message}" for message in spatial_measurement_issues(claim, spatial_data))
            if any(o.get('snapshot_id') is not None and o['snapshot_id'] not in self.allowed_snapshots for o in obs):
                issues.append(f"第 {index} 条工具观测引用了未授权图纸快照。")
                continue
            if any(o['tool']=='calculate_facts' and o['data'].get('conditional') for o in obs) and not re.search('用户|题设|给定|假设|参考|条件',claim.text):
                issues.append(f'第 {index} 条包含用户输入的运算须明确条件，不能冒充图纸实测。')
            diagnostic = claim.observation_kind == 'measurement_diagnostic'
            if not diagnostic and any(o['tool'] in ('calculate_facts','analyze_measurements','get_analysis_results','check_dimension_chain','prepare_space_connections','review_space_connections','prepare_discovery_review','review_discovered_spaces') for o in obs) and claim.state not in ('derived','unknown'):
                issues.append(f'第 {index} 条计算/继承集合保留候选口径，必须标 derived/unknown。')
            if any(o['tool'] in ('analyze_room_batch','prepare_room_boundaries','select_room_boundaries','resolve_room_units') for o in obs) and claim.state not in ('derived', 'unknown'):
                issues.append(f"第 {index} 条房间批量结果是名称与范围候选，必须标 derived/unknown。")
            if any(o['tool'] in ('query_annotations', 'locate_by_axes', 'trace_detail', 'read_table_semantics',
                                 'read_door_schedule_table', 'trace_door_schedule', 'search_similar_objects')
                   for o in obs) and claim.state not in ('derived', 'unknown', 'conflict'):
                issues.append(f"第 {index} 条语义组件含候选关系，工具观测结论须标 derived/unknown/conflict；原文事实另引用 source 证据。")
            if any(o["tool"] in ("inspect_scope_objects", "get_drawing_context", "read_dimensions", "query_building_relations", "get_component_evidence") for o in obs) and claim.state not in ("derived", "unknown", "conflict"):
                issues.append(f"第 {index} 条公共组件包含候选对象和推导关系，必须标 derived/unknown/conflict，不能升级为已确认设计事实。")
            measurements = [o for o in obs if o["tool"] in ("measure_geometry", "measure_room", "get_measurement_evidence", "measure_room_area")]
            diagnostic_sources = measurements + [o for o in obs if o['tool'] in ('discover_rooms','prepare_space_connections','review_space_connections','prepare_discovery_review','review_discovered_spaces','analyze_measurements','get_analysis_results','check_dimension_chain')]
            if diagnostic and (not diagnostic_sources or claim.state != 'observed'):
                issues.append(f"第 {index} 条诊断观测需引用测量工具，并标 observed。")
            if claim.observation_kind == 'geometry_observation' and measurements:
                issues.append(f"第 {index} 条不能用 geometry_observation 绕过测量状态；测量结论使用 measurement_result，诊断使用 measurement_diagnostic。")
            if claim.observation_kind == 'visual_interpretation':
                bindings = [o for o in obs if o['tool'] in ('bind_visual_objects','select_room_boundaries','review_space_connections','review_discovered_spaces')]
                if not bindings or any(not set(o['data'].get('image_ids',[o['data'].get('image_id')]))<=self.sent_image_ids for o in bindings) or claim.state not in ('derived', 'unknown'):
                    issues.append(f"第 {index} 条视觉解释必须引用已发送图像的绑定观测，标 derived/unknown。")
            if any(o['tool'] == 'bind_visual_objects' for o in obs) and claim.state == 'observed':
                issues.append(f"第 {index} 条视觉绑定不确认对象身份，应标 derived/unknown。")
            if measurements and not diagnostic:
                incomplete = any(o["data"].get("status") != "measured" for o in measurements)
                if (incomplete and claim.state != "unknown") or (not incomplete and claim.state not in ("derived", "unknown")):
                    issues.append(f"第 {index} 条测量为程序推导；待复核测量必须标 unknown，完整测量标 derived。")
            numeric_source = json.dumps(
                [{k:v for k,v in o['data'].items() if k in ('status','diagnostics','unit_basis','applied_unit')}
                 if diagnostic else spatial_numeric_evidence(o['data']) if is_spatial_fact(o['data'])
                 else numeric_evidence(o['data']) for o in obs], ensure_ascii=False
            )
            numeric_claim_text = spatial_numeric_claim_text(claim.text) if spatial_data else claim.text
            numeric_claim_text = without_claim_list_markers(numeric_claim_text)
            if diagnostic:
                numeric_claim_text = re.sub(r'\]\([^)]*\)', ']', numeric_claim_text)
            unsupported_numbers=sorted({t['value'] for t in readings(numeric_claim_text)
                if t['kind'] not in ('ordinal','ordinal_reference')
                and not contains_number(numeric_source,t['value']) and t['value'] not in calculated_values})
            if unsupported_numbers:
                issues.append(f"第 {index} 条数值 {', '.join(unsupported_numbers[:12])} 不在该条引用的工具观测中；请引用实际含该数值的观测，或保留工具原单位及精度，不得自行换算或补值。")
            used_observations.update({o["observation_id"]: o for o in obs})
            claims.append(claim.model_dump() | {"citation_numbers": []})
            if len(issues) == issue_count and set(claim.question_part_ids) <= required:
                validated_claims.append(claim.model_dump())
            continue
        if claim.observation_ids:
            issues.append(f"第 {index} 条原文结论不能以工具观测代替来源。")
        if claim.state != "unknown" and not ids:
            issues.append(f"第 {index} 条缺少证据引用。")
        if any(eid not in self.evidence for eid in ids):
            issues.append(
                f"第 {index} 条引用了未读取或不存在的证据，请先调用证据工具。"
            )
            continue
        if any(self.evidence[eid].get("snapshot_id") is not None
               and self.evidence[eid]["snapshot_id"] not in self.allowed_snapshots for eid in ids):
            issues.append(f"第 {index} 条引用了未授权图纸快照。")
            continue
        quote = "\n".join(self.evidence[eid]["quote"] for eid in ids)
        # Exact numeric tokens or explicitly source-bound, recomputed values.
        if claim.state != "unknown":
            missing = unsupported_values(claim.text, [self.evidence[eid] for eid in ids], calculated_values)
            if missing:
                candidates = {
                    v: [
                        e["evidence_id"]
                        for e in sorted(
                            self.evidence.values(), key=lambda e: len(e["quote"])
                        )
                        if contains_number(e["quote"], v)
                    ][:3]
                    for v in missing
                }
                issues.append(
                    f"第 {index} 条数值 {', '.join(missing)} 未出现在该条引用原文；补充正确来源，或保留原文数值，或用 derived + calculations 声明可复算的来源操作数；不得虚构数值。已读取的可核对来源：{json.dumps(candidates)}。工具位置/图层/数量应单独作为 evidence_basis=tool_observation 的结论并引用 observation_ids。"
                )
        for eid in ids:
            if eid not in positions:
                positions[eid] = len(citations) + 1
                e = self.evidence[eid]
                citations.append(e | {"label": "原文证据", "evidence_url": None})
        claims.append(
            claim.model_dump()
            | {
                "evidence_ids": ids,
                "citation_numbers": [positions[eid] for eid in ids],
            }
        )
        if len(issues) == issue_count and set(claim.question_part_ids) <= required:
            validated_claims.append(claim.model_dump())
    if issues:
        fields = list(parts.field_issues)
        for issue in issues:
            match = re.match(r'第 (\d+) 条', issue)
            if match:
                index = int(match[1]) - 1
                field = ('user_inputs' if 'user_inputs[' in issue else
                         'evidence_ids' if any(t in issue for t in ('数值', '证据', '引用', '来源', '快照')) else
                         'text')
                fields.append({'path': f'claims.{index}.{field}', 'code': 'grounding_validation', 'message': issue})
        raise AnswerValidationError(issues, validated_claims, [c.model_dump() for c in parsed.claims],
            field_issues=fields, validated_user_inputs=parts.validated_user_inputs,
            targets=repair_targets(parts.raw, fields))
    from cad_agent.analysis.delivery_recovery import same_calculation_delivery
    unique_claims = []
    for claim in claims:
        if any(same_calculation_delivery(old, claim, self.observations) for old in unique_claims):
            parts.normalizations.append(dict(action='deduplicate_calculation_receipts', text=claim['text']))
        else:
            unique_claims.append(claim)
    claims = unique_claims
    for claim in claims:
        claim['display_text'] = format_claim_text(claim['text'], [self.evidence[eid] for eid in claim.get('evidence_ids',[]) if eid in self.evidence])
        claim['number_format_version'] = NUMBER_VERSION
    prefixes = {
        "observed": "",
        "derived": "【模型推断，待核查】",
        "unknown": "【无法确定】",
        "conflict": "【存在冲突，待核查】",
    }
    answer = "\n\n".join(
        (
            ("【待确认测量】" if c['state'] == 'unknown' else
             "【视觉候选，待核查】" if c.get('observation_kind') == 'visual_interpretation' else
             "【诊断观测】" if c.get('observation_kind') == 'measurement_diagnostic' else "【工具观测，非原文结论】")
            if c.get("evidence_basis") == "tool_observation"
            else prefixes[c["state"]]
        )
        + c["display_text"]
        + "".join(f" [{n}]" for n in c["citation_numbers"])
        for c in claims
    )
    if parts.validated_user_inputs:
        answer += "\n\n用户给定条件（非原图事实）：\n" + "\n".join(
            "“" + r["quote"] + "”（值：" + r["value"] + "）" for r in parts.validated_user_inputs)
    conditions = explicit_user_conditions(self.request.question)
    if conditions:
        answer += "\n\n用户条件原句（仅保留输入，不是原图事实或计算结果）：\n" + "\n".join(c["quote"] for c in conditions)
    return {
        "answer": answer,
        "user_conditions": conditions,
        "user_inputs": parts.validated_user_inputs,
        "protocol_normalizations": parts.normalizations,
        "claims": claims,
        "citations": citations,
        "tool_observations": list(used_observations.values()),
        "property_evidence": [
            field for field in property_fields.values()
            if any((ref["kind"] == "observation" and ref["ref_id"] in used_observations)
                   or (ref["kind"] == "evidence" and ref["ref_id"] in positions)
                   for ref in field["source_refs"])
        ],
        "property_evidence_diagnostics": property_diagnostics,
        "missing_information": parsed.missing_information,
        "requirement_outcomes": [v.model_dump() for v in parsed.requirement_outcomes],
        "mode": "agent",
        "fact_set_id": self.fact_set_id,
        "note": "引用已校验原文与来源；语义理解、适用范围和完整性仍需核查。",
    }



def append_source_excerpts(session, result):
    """Compatibility entry point: never append investigation sources to answers."""
    from cad_agent.analysis.evidence_selection import publish
    return publish(result)


def supersedes_claim(current, cached):
    """A later statement can retract a prior fact, even if repair still fails."""
    if not set(current.get('question_part_ids', [])) & set(cached.get('question_part_ids', [])):
        return False
    current_fields = set(current.get('evidence_roles', {}).get('property', [])) | set(current.get('evidence_roles', {}).get('value', []))
    cached_fields = set(cached.get('evidence_roles', {}).get('property', [])) | set(cached.get('evidence_roles', {}).get('value', []))
    if current_fields and cached_fields and current_fields.isdisjoint(cached_fields):
        return False  # A shared subject/title cannot retract a different field.
    # A gap on the same requirement is not a retraction of every source fact.
    # Only an explicit overlapping source/observation can retract a cached fact.
    if current['state'] in ('unknown', 'conflict'):
        return bool(set(current.get('evidence_ids', [])) & set(cached.get('evidence_ids', []))
                    or set(current.get('observation_ids', [])) & set(cached.get('observation_ids', [])))
    source_key = lambda c: (tuple(sorted(c.get('evidence_ids', []))),
                            tuple(sorted(c.get('observation_ids', []))))
    # Several independent fields can legitimately share a paragraph citation.
    # A new positive fact is not an implicit revision of every sibling field.
    return (source_key(current) == source_key(cached) and any(source_key(current))
            and (current['text'] == cached['text']
                 or bool(re.search(r'修正|更正|撤回|更改|替换原结论', current['text']))))
