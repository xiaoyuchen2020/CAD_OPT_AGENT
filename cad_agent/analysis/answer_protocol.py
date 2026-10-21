"""Field-local answer parsing. Normalize containers, never infer evidence semantics."""
import json
from dataclasses import dataclass
from pydantic import ValidationError
from cad_agent.analysis.tool_registry import FinalAnswer, Claim, RequirementOutcome
from cad_agent.analysis.fact_tools import UserInputRef, user_values


ANSWER_CONTRACT = {
    'evidence_basis': ['source', 'tool_observation'],
    'state': ['observed', 'derived', 'unknown', 'conflict'],
    'missing_information': '字符串数组；不要返回对象。具体原因和对象写在完整字符串内。',
    'user_inputs': '独立条件登记数组 [{quote:本轮问题的逐字片段,value:该片段内的数值或完整数字短语}]；不是原图事实，不授予任何要求完成状态。',
    'source_delivery': 'read_assembly的source_bound确认来源内标题/表行与层次归属，按已读完整原文逐字段交付；object_instance_state或engineering_applicability_state未确认只限制工程实例适用性。已读但未整理须标validation_failed，不能称unread_source；原文摘录和工具完成均不算主体答案。',
    'structured_fields': '交付包的delivery_fields提供来源内已绑定字段及claim_template，可按实际问题选择并填写question_part_ids；candidate_requirement_ids不是完成证明。模板不替代分页、单位、实例适用性、数值与最终逐项校验；不要把全部已读source机械标fulfilled。',
    'calculation_delivery': 'calculate_facts 返回 calculated 后，正文单列 evidence_basis=tool_observation、observation_ids=[计算观测ID]、state=derived，并保留用户条件及小计/完整合计边界；不要用 calculations 重复声明工具运算，不混入 source evidence_ids。每个算式口径只交付一次。原文层次摘要另列，比较用户标签须 derived。',
    'repair': '只修正 field_issues 指定字段；保留 validated_claims 和 validated_user_inputs。未知枚举不得猜成 source；缺证据或数值不匹配须回查，不能只换标签。',
}


@dataclass
class AnswerParts:
    answer: FinalAnswer
    field_issues: list
    validated_user_inputs: list
    normalizations: list
    raw: dict
    rejected_user_inputs: int = 0
    claim_indexes: tuple = ()


def inspect_answer(content, question):
    """Parse independent fields so one malformed entry cannot erase valid siblings.

    Full-answer errors remain blocking for normal publication. Recovery receives
    only individually schema-checked claims, and must re-run provenance checks.
    """
    try:
        raw = json.loads(content)
    except (ValueError, TypeError):
        raise ValueError('答案必须为有效 JSON 对象；仅修复 JSON 结构，不重写已校验结论。') from None
    if not isinstance(raw, dict):
        raise ValueError('答案必须为 JSON 对象。')
    normalized = dict(raw)
    normalizations = []
    missing = raw.get('missing_information', [])
    if isinstance(missing, list):
        normalized['missing_information'] = []
        for index, item in enumerate(missing):
            if isinstance(item, dict) and set(item) == {'text'} and isinstance(item['text'], str):
                # A single text container is unambiguous. Objects carrying scope,
                # status or other semantics must be repaired, never flattened.
                item = item['text']
                normalizations.append({'path': f'missing_information.{index}',
                                       'action': 'unwrap_text_only_gap'})
            normalized['missing_information'].append(item)
    issues = []
    try:
        FinalAnswer.model_validate(normalized)
    except ValidationError as error:
        issues = [{'path': '.'.join(map(str, e['loc'])), 'code': e['type'],
                   'message': e['msg']} for e in error.errors(include_input=False)]

    def items(key, model):
        result = []
        values = normalized.get(key, [])
        if not isinstance(values, list):
            return result
        for item in values:
            try:
                result.append(model.model_validate(item))
            except (ValueError, TypeError):
                pass  # Exact field issue above remains visible and blocks publication.
        return result

    claims = items('claims', Claim)
    claim_indexes = []
    if isinstance(raw.get('claims'), list):
        for i, item in enumerate(raw['claims']):
            try:
                Claim.model_validate(item)
                claim_indexes.append(i)
            except (ValueError, TypeError):
                pass
    refs, seen = [], set()
    # Conditions survive a rejected claim but are never upgraded to drawing facts.
    ref_groups = [('user_inputs', raw.get('user_inputs', []))]
    if isinstance(raw.get('claims'), list):
        ref_groups.extend((f'claims.{i}.user_inputs', c.get('user_inputs', []))
                          for i, c in enumerate(raw['claims']) if isinstance(c, dict))
    valid_top = []
    rejected_refs = 0
    for path, values in ref_groups:
        if not isinstance(values, list):
            continue
        for index, data in enumerate(values):
            try:
                ref = UserInputRef.model_validate(data)
                user_values([ref], question, 'derived', '用户给定条件')
            except (ValueError, TypeError) as error:
                rejected_refs += 1
                # Claim-specific checks are repeated by delivery; top-level errors
                # cannot prevent independently valid source claims being checked.
                if path == 'user_inputs':
                    issues.append({'path': f'{path}.{index}', 'code': 'invalid_user_input',
                                   'message': str(error)})
                continue
            if path == 'user_inputs':
                valid_top.append(ref)
            key = (ref.quote, ref.value)
            if key not in seen:
                refs.append(ref.model_dump()); seen.add(key)
    # model_construct is internal only: each child above is validated. Aggregate
    # violations (missing claims, count, extra keys) remain in field_issues.
    parsed = FinalAnswer.model_construct(
        claims=claims, user_inputs=valid_top,
        requirement_outcomes=items('requirement_outcomes', RequirementOutcome),
        missing_information=[v for v in normalized.get('missing_information', []) if isinstance(v, str)]
        if isinstance(normalized.get('missing_information', []), list) else [])
    return AnswerParts(parsed, issues, refs, normalizations, raw, rejected_refs, tuple(claim_indexes))


def explicit_user_conditions(question):
    """Retain explicit question conditions verbatim, without interpreting operands.

    Sentence bounds and a marker are provenance, not a semantic extractor. Values
    used in calculations still require independently checked UserInputRef records.
    """
    import re
    marker = re.compile(r'内部(?:参考|比较值|比较|筛选参考|规则|对比)|用户(?:给定|提供)')
    result = []
    for match in re.finditer(r'[^。；;\n]+[。；;]?', question):
        quote = match.group(0)
        found = marker.search(quote)
        if found and (found.group(0).startswith('内部') or re.search(r'\d', quote)
                      or re.search(r'(?:标签|条件|参考)[^。；]{0,12}(?:为|：|:)', quote[found.start():])):
            result.append({'origin': 'user_question', 'quote': quote,
                           'question_span': [match.start(), match.end()],
                           'interpretation': 'verbatim_condition_not_drawing_fact'})
    return result


def repair_targets(raw, issues):
    """Keep the exact rejected item beside its field paths for a bounded repair."""
    targets = {}
    for issue in issues:
        path = issue['path'].split('.')
        root = path[0]
        value = raw.get(root)
        target = root
        if len(path) > 1 and path[1].isdigit() and isinstance(value, list):
            index = int(path[1])
            if index >= len(value):
                continue
            value = value[index]
            target += '.' + path[1]
        if target not in targets:
            # Preserve whole items; oversized raw data stays out of the prompt.
            if len(json.dumps(value, ensure_ascii=False)) > 6000:
                value = {'omitted': '条目过大，按字段反馈从原始已读证据重新核查；未自动修改。'}
            targets[target] = {'item_path': target, 'value': value, 'field_issues': []}
        targets[target]['field_issues'].append(issue)
    return list(targets.values())


def delivery_consistency_issues(claim, evidence, observations):
    """Reject a narrow factual denial contradicted by this run's read provenance.

    This checks whether source text/binding exists, not engineering applicability
    or the semantic correctness of a proposed answer. It never synthesizes facts,
    silently promotes unknowns, or counts a tool call as delivered analysis.
    """
    import re
    text = claim.text
    ids = set(claim.evidence_ids)
    if not ids:
        return []
    if any(text in evidence.get(eid, {}).get('quote', '') for eid in ids):
        return []  # Literal reporting of a source is not an authored absence claim.
    issues = []
    # A continuous containing span disproves a claim that only its tiny component
    # cells were read. Same snapshot, region and exact character containment are
    # required; similar words from another construction are never sufficient.
    no_continuous = re.search(r'(?:缺少|缺乏|未取得|未读到|没有|未能恢复).{0,12}(?:连续.{0,4}(?:完整|原文|句)|完整(?:原文|句))', text)
    cited = [evidence[eid] for eid in ids if eid in evidence]
    if no_continuous:
        for whole in evidence.values():
            span = whole.get('text_span')
            if not span or len(whole.get('quote', '')) < 20:
                continue
            contained = [e for e in cited if e.get('text_span')
                         and e.get('snapshot_id') == whole.get('snapshot_id')
                         and e.get('region_id') == whole.get('region_id')
                         and span[0] <= e['text_span'][0] and span[1] >= e['text_span'][1]
                         and len(whole['quote']) > len(e.get('quote', ''))]
            if len(contained) >= 2 and re.search(r'[。；;\n]', whole['quote']):
                issues.append('已读完整来源 ' + whole['evidence_id'] +
                    ' 包含本条引用的多个片段；须消费该连续原文并逐字段回答。若归属或符号仍不明，只说明该具体缺口，不能称缺少连续原文。')
                break
    # Restrict this guard to source ownership assertions. A source-bound table
    # may still require another drawing to identify an actual project instance.
    ownership_denial = re.search(
        r'(?:归属|所属做法|跨行对应关系|跨行归属).{0,12}(?:未确认|未确定|不明|未核实|待核对|尚未核实|未建立)'
        r'|(?:不足以确认|不能确认).{0,30}(?:跨行对应关系|跨行归属)'
        r'|未建立.{0,40}(?:对应关系|归属)'
        r'|归属于.{0,20}(?:未确认|未核实)', text)
    if ownership_denial:
        sentence = re.split(r'[。；;\n]', text[:ownership_denial.end()])[-1]
        # An explicitly named, unbound sibling can remain unknown beside a bound
        # record. Citation membership alone must not transfer source ownership.
        if not re.search(r'该|其|已读|已提供|表格跨行', sentence):
            return issues
        from cad_agent.analysis.delivery_context import _reference_ids
        for observation in observations.values():
            if observation.get('tool') != 'read_assembly':
                continue
            data = observation['data']
            binding = data.get('source_binding') or {}
            if binding.get('state') != 'source_bound' or data.get('view') != 'record':
                continue
            # Boundary evidence belongs to the next construction, not this one.
            members = _reference_ids(data.get('items', [])) | _reference_ids(data.get('title_evidence', {}))
            if not ids.intersection(members):
                continue
            if re.search(r'(?:工程实例|具体实例|实际(?:构件|对象)|图中对象).{0,20}(?:归属|适用|对应)', text):
                continue
            issues.append('已读构造记录 ' + data['record_id'] +
                ' 已建立来源内标题/表行与层次的绑定；须依据其已读层次逐项交付。工程实例适用性仍未确认，但不能以此否认来源内归属。')
            break
    return issues
