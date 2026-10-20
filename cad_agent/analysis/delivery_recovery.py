"""Bounded partial delivery, independent of a malformed sibling or repair response."""
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
    candidates = current + [c for c in self._validated_delivery_claims
                            if not any(supersedes_claim(new, c) for new in current)]
    for data in candidates:
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
        checked.append({k: v for k, v in validated['claims'][0].items() if k != 'citation_numbers'})
    facts = [c for c in checked if c['state'] != 'unknown']
    unknowns = [c for c in checked if c['state'] == 'unknown']
    if not facts and not unknowns and not valid_refs:
        return None
    kept = (facts + unknowns)[:11]
    omissions = []
    for step, outcome in zip(steps, outcomes):
        rid = step['id']
        gaps = [c['text'] for c in unknowns if rid in c['question_part_ids']]
        original = original_outcomes.get(rid, {}) if rid not in rejected_requirements else {}
        if original.get('status') in ('partial', 'unresolved') and original.get('reason'):
            gaps.insert(0, original['reason'])
        if not gaps:
            gaps = ['尚未形成可交付的完整结论，待核对的验收内容：' + (step.get('acceptance') or step['requirement'])]
        if hasattr(self.task, 'coverage_summary'):
            coverage = next((r for r in self.task.coverage_summary().get('requirements', [])
                             if r['requirement_id'] == rid), {})
            fields = [f"{item['object']}：{item['field']}" for item in coverage.get('items', [])
                      if item['state'] != 'addressed']
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
    result['protocol_normalizations'] = parts.normalizations
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
    cache_validated_parts(self, getattr(error, 'attempted_claims', []),
                          getattr(error, 'validated_claims', []),
                          getattr(error, 'validated_user_inputs', []))
    self.answer_field_issues = getattr(error, 'field_issues', [])
    self.answer_repair_targets = getattr(error, 'repair_targets', [])
    self.validation_issue = str(error)[:4000]
    self.progress(stage="答案引用校验未通过", validation_issue=self.validation_issue)
