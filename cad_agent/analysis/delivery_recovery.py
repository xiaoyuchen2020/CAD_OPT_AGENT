"""Bounded partial delivery, independent of a malformed sibling or repair response."""
import re
from cad_agent.analysis.answer_protocol import inspect_answer
from cad_agent.analysis.fact_tools import UserInputRef, user_values
from cad_agent.analysis.efficiency import dumps


def recover_valid_claims(self, content):
    """Revalidate this run's independent facts and preserve requirement gaps.

    Rejected prose is never rewritten into a fact. Unknown clauses remain
    gaps, and exact source excerpts are delivered separately by finish().
    """
    try:
        parts = inspect_answer(content, self.request.question)
    except ValueError:
        # A malformed new document must not erase previously checked clauses.
        parts = inspect_answer('{"claims":[]}', self.request.question)
    parsed = parts.answer
    valid_refs, dropped_refs, ref_seen = [], parts.rejected_user_inputs, set()
    for data in parts.validated_user_inputs + getattr(self, '_validated_delivery_user_inputs', []):
        try:
            ref = UserInputRef.model_validate(data)
            user_values([ref], self.request.question, 'derived', '用户给定条件')
        except ValueError:
            dropped_refs += 1
            continue
        key = (ref.quote, ref.value)
        if key not in ref_seen:
            valid_refs.append(ref); ref_seen.add(key)
    parsed.user_inputs = valid_refs[:20]
    steps = self.task.data['subtasks']
    ids = [step['id'] for step in steps]
    original_outcomes = {o.requirement_id: o.model_dump() for o in parsed.requirement_outcomes}
    outcomes = [dict(requirement_id=i, status='unresolved', gap_kind='validation_failed',
                     reason='答案整理修复已用尽；该要求尚无通过校验的独立结论。') for i in ids]

    def packet(claims):
        covered = {i for c in claims for i in c.get('question_part_ids', [])}
        missing = [i for i in ids if i not in covered]
        filler = [dict(text='尚未形成通过校验的独立结论，具体要求及缺口见逐项交付记录。',
                       state='unknown', question_part_ids=missing)] if missing else []
        return dict(claims=claims + filler,
                    user_inputs=[r.model_dump() for r in parsed.user_inputs],
                    requirement_outcomes=outcomes, missing_information=[])

    checked, rejected, seen = [], len({e['path'].split('.')[1] for e in parts.field_issues if e['path'].startswith('claims.') and len(e['path'].split('.')) > 1}), set()
    # Prefer the current attempt, then recover independently valid clauses
    # from earlier failed attempts in this same session. Recheck every ID,
    # number, calculation, user reference and self-containment constraint.
    from cad_agent.analysis.delivery import supersedes_claim
    current = [c.model_dump() for c in parsed.claims]
    if len(ids) == 1:
        for claim in current:
            if not claim['question_part_ids']:
                claim['question_part_ids'] = ids[:]
    rejected_requirements = set()
    raw_claims = parts.raw.get('claims', [])
    for issue in parts.field_issues:
        path = issue['path'].split('.')
        if len(path) > 1 and path[0] == 'claims' and path[1].isdigit() and isinstance(raw_claims, list):
            position = int(path[1])
            raw = raw_claims[position] if position < len(raw_claims) else None
            if isinstance(raw, dict):
                owners = raw.get('question_part_ids', [])
                if isinstance(owners, list):
                    rejected_requirements.update(i for i in owners if isinstance(i, str) and i in ids)
    candidates = current + [c for c in getattr(self, '_recoverable_delivery_claims', [])
                            if not any(supersedes_claim(new, c) for new in current)] + [c for c in self._validated_delivery_claims
                            if not any(supersedes_claim(new, c) for new in current)]
    normalizations = []
    for data in candidates:
        data, normalization = conditional_source_claim(data, self.request.question)
        if normalization:
            normalizations.append(normalization)
        key = dumps(data)
        if key in seen:
            continue
        seen.add(key)
        try:
            validated = self.validate_answer(dumps(packet([data])))
        except ValueError:
            rejected += 1
            rejected_requirements.update(data.get('question_part_ids', []))
            continue
        value = {k: v for k, v in validated['claims'][0].items() if k != 'citation_numbers'}
        if any(same_calculation_delivery(c, value, self.observations) for c in checked):
            normalizations.append(dict(action='deduplicate_calculation_receipts', text=value['text']))
            continue
        checked.append(value)
    # Preserve executed arithmetic even if the model mixed source and tool
    # protocols in its prose. This is a conditional numerical receipt, not a
    # reconstructed engineering conclusion or completion of a missing total.
    for data in undelivered_calculation_receipts(self, checked):
        try:
            validated = self.validate_answer(dumps(packet([data])))
        except ValueError:
            continue
        checked.append({k: v for k, v in validated['claims'][0].items() if k != 'citation_numbers'})
        normalizations.append(dict(action='retain_executed_calculation', observation_ids=data['observation_ids']))
    facts = [c for c in checked if c['state'] != 'unknown']
    unknowns = [c for c in checked if c['state'] == 'unknown']
    if not facts and not unknowns and not valid_refs:
        return None
    kept = (facts + unknowns)[:11]
    omissions = []
    for step, outcome in zip(steps, outcomes):
        rid = step['id']
        owned = [c for c in kept if rid in c['question_part_ids']]
        gaps = [c['text'] for c in owned if c['state'] == 'unknown']
        original = original_outcomes.get(rid, {}) if rid not in rejected_requirements else {}
        def delivered_cell(cell):
            prop = re.sub(r'(?:原文|做法|情况|分别|是什么|有哪些|多少|[？?])', '', cell['field']).strip()
            return cell['state'] == 'addressed' or any(
                c['state'] not in ('unknown', 'conflict')
                and cell['object'] in c['text'] and (not prop or prop in c['text'])
                and set(c.get('evidence_ids', [])) & set(cell.get('evidence_ids', []))
                for c in owned)
        # Preserve a declared, independently validated completion only when none
        # of its content was rejected/truncated and every operation/field is met.
        # A malformed sibling must not invalidate an unrelated delivered result.
        if (original.get('status') == 'fulfilled' and owned and not gaps
                and not any(c['state'] == 'conflict' for c in owned)
                and hasattr(self.task, 'missing_operations')
                and not self.task.missing_operations(step)
                and all(delivered_cell(c) for c in step.get('coverage', []))
                and all(c in kept for c in checked if rid in c['question_part_ids'])):
            outcome.update(original)
            continue
        if original.get('status') in ('partial', 'unresolved') and original.get('reason'):
            gaps.insert(0, original['reason'])
        if not gaps:
            gaps = ['尚未形成可交付的完整结论，待核对的验收内容：' + (step.get('acceptance') or step['requirement'])]
        if hasattr(self.task, 'coverage_summary'):
            coverage = next((r for r in self.task.coverage_summary().get('requirements', [])
                             if r['requirement_id'] == rid), {})
            fields = [f"{item['object']}：{item['field']}" for item in coverage.get('items', [])
                      if not delivered_cell(item)]
            if fields:
                gaps.append('尚待逐项核查：' + '、'.join(fields))
        reason = '；'.join(dict.fromkeys(gaps))
        outcome['reason'] = reason[:300]
        outcome['status'] = 'partial' if any(c['state'] != 'unknown' and rid in c['question_part_ids'] for c in kept) else 'unresolved'
        gap_kind = original.get('gap_kind') if original.get('status') != 'fulfilled' else None
        if rid in rejected_requirements:
            gap_kind = 'validation_failed'
            outcome['reason'] = ('答案校验或整理未完成；该分项包含未通过校验的结论，已读内容尚待重新整理；' + reason)[:300]
        if gap_kind in (None, 'none'):
            gap_kind = step.get('gap_kind')
        if gap_kind in (None, 'none'):
            stopped_for_budget = any(token in (self.policy.stop_reason or '')
                                     for token in ('budget', 'time_limit', 'round_limit', 'tool_limit'))
            gap_kind = 'budget_exhausted' if stopped_for_budget else 'validation_failed'
            if gap_kind == 'validation_failed':
                outcome['reason'] = ('答案校验或整理未完成；' + reason)[:300]
        outcome['gap_kind'] = gap_kind
        omissions.append(step['requirement'] + '：' + reason)
    final = packet(kept)
    # Keep every original field-level gap in user-visible delivery metadata;
    # never replace them with a single generic unsupported-capability label.
    final['missing_information'] = omissions + parsed.missing_information[:max(0, 11-len(omissions))]
    final['missing_information'].append('部分结论未通过校验或整理未完成；已读原文摘录不代表主体分析已完成。')
    previous_stop = self.policy.stop_reason
    self.policy.stop_reason = 'validated_partial_recovery'
    try:
        result = self.finish(dumps(final))
    except ValueError:
        self.policy.stop_reason = previous_stop
        return None
    result['protocol_normalizations'] = parts.normalizations + normalizations
    result['recovery_field_issues'] = parts.field_issues
    raw_missing = parts.raw.get('missing_information', [])
    result['recovery_rejected_fields'] = [
        {'path': f'missing_information.{i}', 'value': value}
        for i, value in enumerate(raw_missing if isinstance(raw_missing, list) else [])
        if not isinstance(value, str)
        and not (isinstance(value, dict) and set(value) == {'text'} and isinstance(value['text'], str))]
    if not isinstance(raw_missing, list):
        result['recovery_rejected_fields'].append({'path': 'missing_information', 'value': raw_missing})
    return result | {'delivery_kind': 'validated_partial_recovery', 'recovery': {
        'kept_claims': sum(c['state'] != 'unknown' for c in kept), 'rejected_claims': rejected,
        'retained_unknowns': sum(c['state'] == 'unknown' for c in kept),
        'rejected_user_inputs': dropped_refs, 'retained_user_inputs': len(result.get('user_inputs', [])), 'complete': False,
        'note': '仅保留逐条重新通过校验的结论和具体资料缺口；原文摘录不升级为分析结论。'}}

def cache_validated_parts(self, attempted, validated, refs):
    """Session-local factual cache; this is not a task completion receipt."""
    from cad_agent.analysis.delivery import supersedes_claim
    cached = {dumps(c): c for c in self._validated_delivery_claims
              if not any(supersedes_claim(new, c) for new in attempted)}
    for claim in validated:
        cached[dumps(claim)] = claim
    self._validated_delivery_claims = list(cached.values())[-12:]
    known_refs = {dumps(r): r for r in getattr(self, '_validated_delivery_user_inputs', [])}
    for ref in refs:
        known_refs[dumps(ref)] = ref
    self._validated_delivery_user_inputs = list(known_refs.values())[:20]


def record_validation_issue(self, error):
    # Keep only explicitly labelled conditional source candidates for bounded
    # revalidation. Never cache arbitrary failed factual assertions.
    retained = {dumps(c): c for c in getattr(self, '_recoverable_delivery_claims', [])}
    for claim in getattr(error, 'attempted_claims', []):
        repaired, change = conditional_source_claim(claim, self.request.question)
        if change:
            retained[dumps(repaired)] = repaired
    self._recoverable_delivery_claims = list(retained.values())[-12:]
    cache_validated_parts(self, getattr(error, 'attempted_claims', []),
                          getattr(error, 'validated_claims', []),
                          getattr(error, 'validated_user_inputs', []))
    self.answer_field_issues = getattr(error, 'field_issues', [])
    self.answer_repair_targets = getattr(error, 'repair_targets', [])
    self.validation_issue = str(error)[:4000]
    self.progress(stage="答案引用校验未通过", validation_issue=self.validation_issue)


def conditional_source_claim(claim, question):
    """Downgrade an explicitly labelled, valid user comparison; never add facts."""
    if (claim.get('state') != 'observed' or claim.get('evidence_basis', 'source') != 'source'
            or not claim.get('evidence_ids') or not claim.get('user_inputs')
            or not re.search(r'用户|题设|给定|假设|参考', claim['text'])):
        return claim, None
    try:
        refs = [UserInputRef.model_validate(r) for r in claim['user_inputs']]
        user_values(refs, question, 'derived', claim['text'])
    except ValueError:
        return claim, None
    return claim | {'state': 'derived'}, dict(action='retain_explicit_user_comparison_as_derived', text=claim['text'])


def same_calculation_delivery(left, right, observations):
    """Same executed results and owners, not merely similar prose or numbers."""
    from cad_agent.analysis.grounding import contains_number
    if any(c.get('state') != 'derived' or c.get('evidence_basis') != 'tool_observation'
           or c.get('evidence_ids') or c.get('property_assertions') for c in (left, right)):
        return False
    ids = set(left.get('observation_ids', []))
    if not ids or ids != set(right.get('observation_ids', [])) or set(left['question_part_ids']) != set(right['question_part_ids']):
        return False
    records = [observations.get(oid, {}) for oid in ids]
    if any(o.get('tool') != 'calculate_facts' or o.get('data', {}).get('status') != 'calculated' for o in records):
        return False
    # Both paragraphs must actually report every result. Complementary clauses
    # about distinct results remain separate even if they cite the same set.
    return all(contains_number(c['text'], str(o['data']['value'])) for c in (left, right) for o in records)


def undelivered_calculation_receipts(session, checked):
    cited = {oid for c in checked for oid in c.get('observation_ids', [])}
    seen = set()
    for plan in session.task.data.get('calculation_workflows', {}).values():
        oid = plan.get('observation_id')
        if not oid or oid in cited or oid in seen or plan.get('status') != 'calculated':
            continue
        seen.add(oid)
        obs = session.observations.get(oid, {})
        d = obs.get('data', {})
        if obs.get('tool') != 'calculate_facts' or d.get('status') != 'calculated' or d.get('operation') != 'subtract':
            continue
        inputs = d.get('inputs', [])
        if len(inputs) != 2 or not any(i.get('origin') == 'user_input' for i in inputs):
            continue
        owners = sorted({rid for p in session.task.data['calculation_workflows'].values()
                         if p.get('observation_id') == oid for rid in p.get('task_ids', [])})
        operands = ' − '.join(str(i['value']) + i.get('unit', '') for i in inputs)
        yield dict(text=f"条件算术记录（用户参照）：{operands} = {d['value']}{d.get('unit', '')}。"
                   + '本记录仅交付已执行的数值差；所选操作数的来源、单位及层次见工具记录。'
                   + '对象对应与完整层次仍需核查；已知层小计不能代表含未知层的整套总厚度。',
                   state='derived', evidence_basis='tool_observation', observation_ids=[oid],
                   question_part_ids=owners)
