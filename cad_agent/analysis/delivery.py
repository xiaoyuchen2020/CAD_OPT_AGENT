"""Source, numerical and per-requirement answer validation."""
import json
import re
from cad_agent.analysis.boundary_context import numeric_evidence
from cad_agent.analysis.grounding import numbers, contains_number, verify_calculations
from cad_agent.analysis.fact_tools import user_values

from cad_agent.analysis.tool_registry import FinalAnswer


def validate_answer(self, content):
    parsed = FinalAnswer.model_validate_json(content)
    if len(self.task.parts)==1:
        for claim in parsed.claims:
            if not claim.question_part_ids:claim.question_part_ids=[self.task.parts[0]['id']]
    # A top-level reference registry is accepted, but each numerical claim
    # must still explicitly identify user conditions and be derived.
    if parsed.user_inputs:
        user_values(parsed.user_inputs,self.request.question,'derived','用户给定条件')
    for claim in parsed.claims:
        if claim.state=='derived' and re.search('用户|题设|给定|假设|参考|条件',claim.text):
            own={(r.quote,r.value) for r in claim.user_inputs}
            claim.user_inputs.extend(r for r in parsed.user_inputs if any(contains_number(claim.text,n) for n in numbers(r.value)) and (r.quote,r.value) not in own)
    claims, citations, positions, issues = [], [], {}, []
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
    for index, claim in enumerate(parsed.claims, 1):
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
            if any(o['tool']=='calculate_facts' and o['data'].get('conditional') for o in obs) and not re.search('用户|题设|给定|假设|参考|条件',claim.text):
                issues.append(f'第 {index} 条包含用户输入的运算须明确条件，不能冒充图纸实测。')
            diagnostic = claim.observation_kind == 'measurement_diagnostic'
            if not diagnostic and any(o['tool'] in ('calculate_facts','analyze_measurements','get_analysis_results','check_dimension_chain','prepare_space_connections','review_space_connections','prepare_discovery_review','review_discovered_spaces') for o in obs) and claim.state not in ('derived','unknown'):
                issues.append(f'第 {index} 条计算/继承集合保留候选口径，必须标 derived/unknown。')
            if any(o['tool'] in ('analyze_room_batch','prepare_room_boundaries','select_room_boundaries','resolve_room_units') for o in obs) and claim.state not in ('derived', 'unknown'):
                issues.append(f"第 {index} 条房间批量结果是名称与范围候选，必须标 derived/unknown。")
            if any(o['tool'] in ('query_annotations', 'locate_by_axes', 'trace_detail', 'read_table_semantics') for o in obs) and claim.state not in ('derived', 'unknown', 'conflict'):
                issues.append(f"第 {index} 条语义组件含候选关系，工具观测结论须标 derived/unknown/conflict；原文事实另引用 source 证据。")
            if any(o["tool"] in ("get_drawing_context", "read_dimensions", "query_building_relations", "get_component_evidence") for o in obs) and claim.state not in ("derived", "unknown", "conflict"):
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
                 if diagnostic else numeric_evidence(o['data']) for o in obs], ensure_ascii=False
            )
            unsupported_numbers=sorted({v for v in re.findall(r"\d+(?:\.\d+)?", re.sub(r'\]\([^)]*\)', ']', claim.text) if diagnostic else claim.text)
                                        if v not in numeric_source and v not in calculated_values})
            if unsupported_numbers:
                issues.append(f"第 {index} 条数值 {', '.join(unsupported_numbers[:12])} 不在该条引用的工具观测中；请引用实际含该数值的观测，或保留工具原单位及精度，不得自行换算或补值。")
            used_observations.update({o["observation_id"]: o for o in obs})
            claims.append(claim.model_dump() | {"citation_numbers": []})
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
        quote = "\n".join(self.evidence[eid]["quote"] for eid in ids)
        # Exact numeric tokens or explicitly source-bound, recomputed values.
        if claim.state != "unknown":
            values = numbers(claim.text)
            missing = sorted({value for value in values if not contains_number(quote, value) and value not in calculated_values})
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
    if issues:
        raise ValueError("\n".join(issues))
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
        + c["text"]
        + "".join(f" [{n}]" for n in c["citation_numbers"])
        for c in claims
    )
    return {
        "answer": answer,
        "claims": claims,
        "citations": citations,
        "tool_observations": list(used_observations.values()),
        "missing_information": parsed.missing_information,
        "requirement_outcomes": [v.model_dump() for v in parsed.requirement_outcomes],
        "mode": "agent",
        "fact_set_id": self.fact_set_id,
        "note": "引用已校验原文与来源；语义理解、适用范围和完整性仍需核查。",
    }

