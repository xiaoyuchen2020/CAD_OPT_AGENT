"""B03 field isolation, input provenance and strict negative controls; offline only."""
import json
from copy import deepcopy
import pytest
from cad_agent.analysis.answer_protocol import inspect_answer, explicit_user_conditions
from cad_agent.analysis.delivery import AnswerValidationError
from test_remediation_delivery import read_fact


def test_illegal_enum_is_isolated_never_guessed_and_good_sibling_cached(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    bad = fact | {'text': '非法来源条目', 'evidence_basis': 'user_input'}
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(json.dumps({'claims': [bad, fact]}))
    assert e.value.validated_claims[0]['text'] == fact['text']
    assert e.value.field_issues[0]['path'] == 'claims.0.evidence_basis'
    assert e.value.repair_targets[0]['item_path'] == 'claims.0'
    assert e.value.repair_targets[0]['value']['evidence_basis'] == 'user_input'
    s.record_validation_issue(e.value)
    result = s.recover_valid_claims(json.dumps({'claims': [bad]}))
    assert result['recovery']['kept_claims'] == 1
    assert '非法来源条目' not in result['answer']
    assert result['task']['status'] == 'partial'


def test_nested_gap_object_requires_repair_and_is_preserved_in_diagnostics(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    gap = {'requirement_id': 'q1', 'reason': '还缺范围', 'details': {'source': ['未读页', '单位'], 'value': None}}
    original = deepcopy(gap)
    payload = json.dumps({'claims': [fact], 'missing_information': [gap]})
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(payload)
    assert e.value.field_issues[0]['path'] == 'missing_information.0'
    result = s.recover_valid_claims(payload)
    assert result['recovery_rejected_fields'][0]['value'] == original and gap == original
    assert result['recovery']['kept_claims'] == 1
    assert 'requirement_id' not in result['answer'] and 'details' not in result['answer']


def test_single_text_gap_container_is_unambiguous(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    result = s.validate_answer(json.dumps({'claims': [fact], 'missing_information': [{'text': '尚未核查范围。'}]}))
    assert result['missing_information'] == ['尚未核查范围。']
    assert result['protocol_normalizations'][0]['action'] == 'unwrap_text_only_gap'


def test_bad_reference_does_not_delete_valid_reference_or_source(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes, '用户给定70mm；请读取图纸原文。')
    good = {'quote': '用户给定70mm', 'value': '70mm'}
    bad = {'quote': '用户给定70mm', 'value': '700mm'}
    payload = {'claims': [fact, fact | {'text': '用户参考700mm', 'state': 'derived', 'user_inputs': [bad]}],
               'user_inputs': [good, bad]}
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(json.dumps(payload))
    assert e.value.validated_user_inputs == [good]
    s.record_validation_issue(e.value)
    result = s.recover_valid_claims('{"claims": [{"evidence_basis":"bogus"}]}')
    assert result['user_inputs'] == [good]
    assert fact['text'] in result['answer'] and '700mm' not in result['answer']
    assert result['user_conditions'][0]['origin'] == 'user_question'
    assert all(c['user_inputs'] == [] for c in result['claims'])


@pytest.mark.parametrize('fault', ['numeric', 'unread', 'foreign_snapshot'])
def test_recovery_rechecks_numbers_ids_and_selected_snapshot(workspace, demo_bytes, fault):
    s, fact = read_fact(workspace, demo_bytes)
    bad = dict(fact)
    if fault == 'numeric': bad['text'] = '图纸厚度987654mm'
    elif fault == 'unread': bad['evidence_ids'] = ['ev_never_read']
    else: s.evidence[fact['evidence_ids'][0]]['snapshot_id'] = 'snap_unauthorized'
    result = s.recover_valid_claims(json.dumps({'claims': [bad]}))
    assert result is None


def test_missing_information_error_does_not_erase_independent_facts(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    payload = {'claims': [fact], 'missing_information': [None]}
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(json.dumps(payload))
    assert e.value.validated_claims[0]['text'] == fact['text']
    result = s.recover_valid_claims(json.dumps(payload))
    assert result['recovery']['kept_claims'] == 1
    assert result['recovery_field_issues'][0]['path'] == 'missing_information.0'


def test_unsourced_gap_on_same_requirement_does_not_retract_fact(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(json.dumps({'claims': [fact, fact | {'text': '厚度987654mm'}]}))
    s.record_validation_issue(e.value)
    gap = {'text': '尚未读取其他材料范围。', 'state': 'unknown', 'question_part_ids': ['q1']}
    result = s.recover_valid_claims(json.dumps({'claims': [gap]}))
    assert result['recovery']['kept_claims'] == 1
    assert fact['text'] in result['answer'] and gap['text'] in result['answer']


def test_explicit_user_conditions_survive_absent_model_refs_without_numeric_inference():
    q = '核查地库。内部筛选参考人防区层高3.7m、非人防3.6m、绿化覆土1.5m，使用净高目标2.2m，但净高必须扣除梁和设备占用。用户给定值与原图值分别标明。'
    conditions = explicit_user_conditions(q)
    assert len(conditions) == 1
    c = conditions[0]
    assert q[slice(*c['question_span'])] == c['quote']
    assert '但净高必须扣除梁和设备占用' in c['quote']
    assert 'value' not in c and 'evidence_ids' not in c


def test_semantic_error_paths_keep_original_index_after_malformed_sibling(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(json.dumps({'claims': [fact | {'evidence_basis': 'bad'}, fact | {'text': '厚度987654mm'}]}))
    paths = {v['path'] for v in e.value.field_issues}
    assert 'claims.0.evidence_basis' in paths and 'claims.1.evidence_ids' in paths


def test_condition_clause_keeps_reference_before_marker_and_nonnumeric_scope_rule():
    q = '内部规则优先采用节能计算书中的保温厚度，其他层次按设计说明；热工和开启面积指标另按与限值相差不大于10%的内部参考比较。'
    c = explicit_user_conditions(q)
    assert len(c) == 2 and ''.join(i['quote'] for i in c) == q


def test_bounded_repair_context_preserves_schema_and_reports_cached_clause_omission():
    from cad_agent.analysis.delivery_context import bounded_packet
    from cad_agent.analysis.answer_protocol import ANSWER_CONTRACT
    packet = {'question': '检验条件', 'task': {}, 'evidence': [], 'observations': [],
              'validated_claims': [{'text': '保留原文'*1000} for _ in range(12)],
              'answer_contract': ANSWER_CONTRACT, 'field_issues': [], 'instruction': '仅修复错误字段'}
    bounded = bounded_packet(packet, 5000)
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(',', ':'))) <= 5000
    assert bounded['answer_contract']['evidence_basis'] == ['source', 'tool_observation']
    assert bounded['context_omissions']['validated_claims_count'] > 0
    assert len(packet['validated_claims']) == 12


def test_frozen_real_published_clauses_and_error_path_injections_replay_without_mutation():
    from scripts.replay_b03_answers import run_replay, FROZEN
    if not FROZEN.exists():
        pytest.skip('frozen real drawing experiment is not installed')
    report, deliveries = run_replay()
    assert report['historical_inputs_unchanged'] and report['real_model_calls'] == 0
    assert len(report['cases']) == 10
    for case in ['Q03-A', 'Q03-B', 'Q12-A', 'Q12-B', 'Q16-A', 'Q16-B', 'Q19-A', 'Q19-B']:
        assert deliveries[case]['user_conditions']
        assert '用户条件原句' in deliveries[case]['answer']
        for condition in deliveries[case]['user_conditions']:
            assert condition['quote'] in deliveries[case]['answer']


def test_pending_geometry_caches_fact_but_cannot_mark_requirement_complete(workspace, demo_bytes, monkeypatch):
    s, fact = read_fact(workspace, demo_bytes)
    monkeypatch.setattr(s.task, 'pending_work', lambda: [{'operation': 'measure_geometry', 'task_ids': ['q1'], 'blocking': True}])
    with pytest.raises(ValueError, match='TASK_WORK_PENDING'):
        s.finish(json.dumps({'claims': [fact]}))
    assert s._validated_delivery_claims[0]['text'] == fact['text']
    result = s.recover_valid_claims('{"claims":[{"text":"待测量","state":"unknown","question_part_ids":["q1"]}]}')
    assert result and result['task']['status'] != 'completed'
    assert all(o['status'] != 'fulfilled' for o in result['requirement_outcomes'])
    assert s.task.pending_work()[0]['operation'] == 'measure_geometry'


def test_unknown_unit_gate_cannot_be_bypassed_by_cached_numeric_fact(workspace, demo_bytes, monkeypatch):
    s, fact = read_fact(workspace, demo_bytes)
    eid = fact['evidence_ids'][0]
    s.evidence[eid]['quote'] = '面积20m²'
    fact['text'] = '面积20m²'
    s.room_batches = {'batch': {'status': 'needs_unit_confirmation'}}
    s.task.data['subtasks'][0].update(kind='measure', status='waiting_input')
    monkeypatch.setattr(s, 'recover_scope_before_delivery', lambda: False)
    with pytest.raises(ValueError, match='单位尚未确认'):
        s.finish(json.dumps({'claims': [fact]}))
    assert s._validated_delivery_claims[0]['text'] == fact['text']
    assert s.recover_valid_claims(json.dumps({'claims': [fact]})) is None


def test_foreign_snapshot_tool_observation_is_rejected_in_recovery(workspace, demo_bytes):
    s, _ = read_fact(workspace, demo_bytes)
    s.observations['obs_wrong'] = {'observation_id': 'obs_wrong', 'snapshot_id': 'snap_unauthorized',
                                   'tool': 'query_entities', 'data': {'total': 20}}
    claim = {'text': '工具返回20个候选。', 'state': 'derived', 'evidence_basis': 'tool_observation',
             'observation_ids': ['obs_wrong'], 'question_part_ids': ['q1']}
    assert s.recover_valid_claims(json.dumps({'claims': [claim]})) is None


@pytest.mark.parametrize('content', [None, 'invalid JSON private-marker'])
def test_unparseable_response_can_still_recover_earlier_verified_claims(workspace, demo_bytes, content):
    s, fact = read_fact(workspace, demo_bytes)
    with pytest.raises(AnswerValidationError) as e:
        s.validate_answer(json.dumps({'claims': [fact, fact | {'text': '987654mm'}]}))
    s.record_validation_issue(e.value)
    result = s.recover_valid_claims(content)
    assert result['recovery']['kept_claims'] == 1
    assert fact['text'] in result['answer'] and 'private-marker' not in result['answer']


def test_root_gap_object_is_retained_only_as_rejected_diagnostic(workspace, demo_bytes):
    s, fact = read_fact(workspace, demo_bytes)
    gap = {'reason': '缺少范围', 'source': ['待读表']}
    result = s.recover_valid_claims(json.dumps({'claims': [fact], 'missing_information': gap}))
    assert result['recovery_rejected_fields'] == [{'path': 'missing_information', 'value': gap}]
    assert result['recovery']['kept_claims'] == 1
    assert '待读表' not in result['answer']


@pytest.mark.parametrize('reply', ['not JSON private-marker', '{"message":"private-marker"}'])
def test_runtime_never_persists_free_reply_even_after_source_read(workspace, demo_bytes, monkeypatch, reply):
    from test_siwc_stopping import make_stopping_session, answer
    from test_agent import final
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    s, rid = make_stopping_session(workspace, demo_bytes, monkeypatch)
    s.invoke_tool('read_source_context', {'region_id': rid})
    calls = []
    def request(self, session, payload):
        calls.append(payload)
        return answer(reply if len(calls) == 1 else final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    assert s.run()['citations']
    assert len(calls) == 2
    rejected = s.runtime_diagnostics['rejected_answers'][0]
    assert set(rejected) == {'round', 'issue'}
    assert 'private-marker' not in json.dumps(s.runtime_diagnostics)


@pytest.mark.parametrize('oversized', [False, True])
def test_runtime_retains_only_bounded_structured_field_failure_after_read(workspace, demo_bytes, monkeypatch, oversized):
    from test_siwc_stopping import make_stopping_session, answer
    from test_agent import final
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    s, rid = make_stopping_session(workspace, demo_bytes, monkeypatch)
    s.invoke_tool('read_source_context', {'region_id': rid})
    evidence = next(iter(s.evidence.values()))
    bad = {'claims': [{'text': '字' * 65000 if oversized else evidence['quote'], 'state': 'observed',
                       'evidence_ids': [evidence['evidence_id']], 'evidence_basis': 'invalid_enum'}]}
    raw = json.dumps(bad, ensure_ascii=False)
    calls = []
    def request(self, session, payload):
        calls.append(payload)
        return answer(raw if len(calls) == 1 else final(evidence['evidence_id']))
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    assert s.run()['citations']
    rejected = s.runtime_diagnostics['rejected_answers'][0]
    assert rejected['answer'] == raw[:60000]
    assert rejected['answer_truncated'] == oversized
    assert len(rejected['answer']) <= 60000
    assert 'evidence_basis' in rejected['issue']


def test_runtime_structured_failure_without_read_evidence_keeps_only_issue(workspace, demo_bytes, monkeypatch):
    from test_siwc_stopping import make_stopping_session, answer, tool
    from test_agent import final
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    s, rid = make_stopping_session(workspace, demo_bytes, monkeypatch)
    calls = []
    raw = json.dumps({'claims': [{'text': 'private-marker', 'state': 'observed', 'evidence_basis': 'invalid_enum'}]})
    def request(self, session, payload):
        calls.append(payload)
        if len(calls) == 1:
            return tool('search_sources', {'query': 'impossible-fixture-marker'}, 'search')
        if len(calls) == 2:
            assert s.observations and not s.evidence
            return answer(raw)
        if len(calls) == 3:
            return tool('read_source_context', {'region_id': rid}, 'read')
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    assert s.run()['citations']
    rejected = s.runtime_diagnostics['rejected_answers'][0]
    assert set(rejected) == {'round', 'issue'}
    assert 'private-marker' not in json.dumps(s.runtime_diagnostics)
