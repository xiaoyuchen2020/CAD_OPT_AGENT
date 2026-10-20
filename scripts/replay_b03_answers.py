"""Offline protocol replay from frozen 2026-10-19 evidence; never writes source runs."""
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from cad_agent.analysis.delivery import validate_answer, AnswerValidationError
from cad_agent.analysis.delivery_recovery import recover_valid_claims, record_validation_issue
from cad_agent.analysis.tool_registry import Claim

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'artifacts/designer-facts-repaired-20261019'
OUTPUT = ROOT / 'artifacts/bottleneck-repairs-20261019'


class ReplaySession:
    validate_answer = validate_answer
    recover_valid_claims = recover_valid_claims
    record_validation_issue = record_validation_issue

    def __init__(self, run, task):
        self.request = SimpleNamespace(**run['request'])
        self.allowed_snapshots = {run['snapshot_id'], *(d['snapshot_id'] for d in run['request'].get('related_drawings', []))}
        self.evidence = deepcopy(task['evidence'])
        self.observations = deepcopy(task['observations'])
        self.task = SimpleNamespace(data=deepcopy(task), parts=[{'id': s['id']} for s in task['subtasks']])
        self.coverage = SimpleNamespace(parts=self.task.parts)
        self.policy = SimpleNamespace(stop_reason='answer_repair')
        self.fact_set_id = run['request'].get('fact_set_id')
        self.sent_image_ids = set((run.get('result') or {}).get('vision', {}).get('image_ids', []))
        self._validated_delivery_claims = []
        self._validated_delivery_user_inputs = []
        self.progress = lambda **kwargs: None

    def finish(self, content):
        # Deliberately no workspace writes or scheduler execution. Full semantic
        # acceptance and live runtime publication are outside offline replay.
        return self.validate_answer(content) | {'task': {'status': 'partial'}}


def load_case(case):
    folder = FROZEN / 'attempts' / case / '0001'
    return (json.loads((folder / 'run.json').read_text()),
            json.loads((folder / 'task.json').read_text()))


def run_replay():
    cases = ['Q03-A', 'Q03-B', 'Q12-A', 'Q12-B', 'Q16-A', 'Q16-B', 'Q19-A', 'Q19-B', 'Q10-B', 'Q13-B']
    source_paths = [FROZEN / 'attempts' / c / '0001' / f for c in cases for f in ('run.json', 'task.json')]
    source_paths += [FROZEN / 'review.json'] + [FROZEN / 'review-inputs' / (c + '.json') for c in cases]
    before = {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in source_paths}
    records, deliveries = [], {}
    for case in cases[:8]:
        run, task = load_case(case)
        s = ReplaySession(run, task)
        published = run['result']
        claims = [{k: v for k, v in c.items() if k in Claim.model_fields} for c in published['claims']]
        payload = {'claims': claims, 'missing_information': published['missing_information'],
                   'requirement_outcomes': published['requirement_outcomes']}
        # Exact model-produced published clauses; no reconstructed lost claims.
        try:
            valid = s.validate_answer(json.dumps(payload, ensure_ascii=False))
            eligible = valid['claims']
            validation_issue = None
        except AnswerValidationError as error:
            s.record_validation_issue(error)
            eligible = error.validated_claims
            validation_issue = str(error)
        result = s.recover_valid_claims(json.dumps(payload, ensure_ascii=False))
        assert result is not None, case
        eligible_text = {c['text'] for c in eligible}
        retained_text = {c['text'] for c in result['claims']}
        assert eligible_text <= retained_text, (case, eligible_text - retained_text)
        for c in result['user_conditions']:
            assert s.request.question[slice(*c['question_span'])] == c['quote']
        assert not any(o['status'] == 'fulfilled' for o in result['requirement_outcomes'])
        records.append({'case': case, 'replay_kind': 'exact_published_claims_with_frozen_read_registry',
                        'published_claims': len(claims), 'independently_validated': len(eligible),
                        'retained': len(result['claims']), 'retained_facts': result['recovery']['kept_claims'],
                        'user_inputs': len(result['user_inputs']), 'verbatim_user_conditions': len(result['user_conditions']),
                        'all_validated_claims_retained': True, 'validation_issue': validation_issue,
                        'historical_lost_response_reconstructed': False})
        deliveries[case] = result
    for case in cases[8:]:
        run, task = load_case(case)
        s = ReplaySession(run, task)
        reads = [e for e in s.evidence.values() if 0 < len(e['quote']) <= 1500]
        facts = [dict(text=e['quote'], state='observed', evidence_ids=[e['evidence_id']],
                      question_part_ids=[task['subtasks'][0]['id']]) for e in reads[:10]]
        assert len(facts) == 10
        payload = {'claims': facts}
        if case == 'Q10-B':
            payload['claims'].append(facts[0] | {'evidence_basis': 'user_input'})
            fault_path = 'claims.10.evidence_basis'
        else:
            # Historical error says object, but its keys were NOT saved. This
            # object is explicitly synthetic, not a purported original response.
            payload['missing_information'] = [{'requirement_id': 'q1', 'reason': '仍需范围依据',
                                               'details': {'unread': ['页码尚未确认']}}]
            fault_path = 'missing_information.0'
        content = json.dumps(payload, ensure_ascii=False)
        try:
            s.validate_answer(content)
            raise AssertionError('fault should be blocked')
        except AnswerValidationError as error:
            assert fault_path in {e['path'] for e in error.field_issues}
            s.record_validation_issue(error)
        result = s.recover_valid_claims(content)
        assert result and result['recovery']['kept_claims'] == 10
        assert not any(o['status'] == 'fulfilled' for o in result['requirement_outcomes'])
        if case == 'Q13-B':
            assert result['recovery_rejected_fields'][0]['value'] == payload['missing_information'][0]
        records.append({'case': case, 'replay_kind': 'synthetic_error_path_injection_on_frozen_read_sources',
                        'historical_failure_path': fault_path, 'original_rejected_response_available': False,
                        'retained_verified_source_excerpts': result['recovery']['kept_claims'],
                        'all_validated_claims_retained': True, 'not_a_historical_repaired_answer': True})
        deliveries[case] = result
    negative_controls = []
    from cad_agent.analysis.grounding import numbers, contains_number
    for fault in ('unsupported_number', 'unread_source', 'foreign_snapshot', 'wrong_read_source', 'forged_user_input'):
        run, task = load_case('Q03-B')
        s = ReplaySession(run, task)
        source = next(iter(s.evidence.values()))
        bad = dict(text=source['quote'][:1400], state='observed', evidence_ids=[source['evidence_id']],
                   question_part_ids=[task['subtasks'][0]['id']])
        if fault == 'unsupported_number':
            bad['text'] = '原图厚度987654321mm。'
        elif fault == 'unread_source':
            bad['evidence_ids'] = ['ev_unread_negative_control']
        elif fault == 'foreign_snapshot':
            s.evidence[source['evidence_id']]['snapshot_id'] = 'snap_unauthorized_negative_control'
        elif fault == 'wrong_read_source':
            pair = next((value, other) for e in s.evidence.values() for value in numbers(e['quote'])
                        for other in s.evidence.values() if not contains_number(other['quote'], value))
            bad['text'] = '原文数值' + pair[0]
            bad['evidence_ids'] = [pair[1]['evidence_id']]
        else:
            bad.update(text='用户给定987654321mm', state='derived',
                       user_inputs=[{'quote': '用户给定987654321mm', 'value': '987654321mm'}])
        assert s.recover_valid_claims(json.dumps({'claims': [bad]}, ensure_ascii=False)) is None, fault
        negative_controls.append({'fault': fault, 'rejected': True, 'source_case': 'Q03-B'})
    after = {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in source_paths}
    assert before == after
    report = {'scope': 'offline protocol/provenance replay, not model or platform acceptance',
              'real_model_calls': 0, 'historical_inputs_unchanged': before == after,
              'input_sha256': before, 'cases': records, 'negative_controls': negative_controls,
              'limitations': ['Q10-B/Q13-B original rejected response bodies were not persisted; only error paths can be replayed by explicit injection.',
                             'Q03/Q12/Q16/Q19 omitted earlier clauses cannot be reconstructed; replay checks published clauses and frozen read registries only.',
                             'Generation adherence, end-to-end completeness and engineering semantics require a later controlled live run.']}
    return report, deliveries


if __name__ == '__main__':
    report, deliveries = run_replay()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'b03-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (OUTPUT / 'b03-replayed-deliveries.json').write_text(json.dumps(deliveries, ensure_ascii=False, indent=2))
    print(json.dumps({'cases': len(report['cases']), 'historical_inputs_unchanged': report['historical_inputs_unchanged'],
                      'retained': [{k: v for k, v in r.items() if k in ('case', 'retained_facts', 'verbatim_user_conditions', 'retained_verified_source_excerpts')} for r in report['cases']]}, ensure_ascii=False, indent=2))
