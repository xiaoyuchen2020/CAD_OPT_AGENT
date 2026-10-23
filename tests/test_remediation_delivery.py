"""Local delivery regressions: no model calls, no historical-result mutation."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from cad_agent.analysis.delivery import AnswerValidationError, append_source_excerpts
from test_factual_workflows import make


def read_fact(workspace, demo_bytes, question='读取图纸原文。'):
    session = make(workspace, demo_bytes, question)
    session.task.auto_plan()
    session.invoke_tool('read_source_context', {
        'region_id': session.index.regions[0].region_id,
        'task_ids': [s['id'] for s in session.task.data['subtasks']],
    })
    evidence = next(iter(session.evidence.values()))
    claim = dict(text=evidence['quote'], state='observed',
                 evidence_ids=[evidence['evidence_id']], question_part_ids=['q1'])
    return session, claim


def test_failed_attempt_caches_only_valid_independent_claims(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes)
    bad = fact | {'text': '前述厚度为987654mm。', 'state': 'derived'}
    with pytest.raises(AnswerValidationError) as error:
        session.validate_answer(json.dumps({'claims': [fact, bad]}))
    session.record_validation_issue(error.value)
    assert len(session._validated_delivery_claims) == 1
    assert session._validated_delivery_claims[0]['text'] == fact['text']
    # A repair that accidentally drops the valid source fact does not erase it.
    result = session.recover_valid_claims(json.dumps({'claims': [bad | {'evidence_ids': ['unread']}]}))
    assert result['recovery']['kept_claims'] == 1
    assert fact['text'] in result['answer'] and '987654' not in result['answer']
    assert result['citations'] and result['task']['status'] == 'partial'


def test_cache_is_session_local_and_revalidated_after_source_removal(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes)
    with pytest.raises(AnswerValidationError) as error:
        session.validate_answer(json.dumps({'claims': [fact, fact | {'text': '厚度987654mm'}]}))
    session.record_validation_issue(error.value)
    session.evidence.clear()
    assert session.recover_valid_claims(json.dumps({'claims': [fact]})) is None
    fresh = make(workspace, demo_bytes)
    assert fresh._validated_delivery_claims == []


def test_recovery_retains_specific_unknowns_and_true_gap_types(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes, '请①读取材料；②读取厚度；③确定适用范围。')
    gaps = [dict(text='尚未读取墙体厚度来源。', state='unknown', question_part_ids=['q2']),
            dict(text='构造与适用楼层之间缺少对应关系。', state='unknown', question_part_ids=['q3'])]
    answer = {'claims': [fact, *gaps, fact | {'text': '材料厚度987654mm'}],
              'missing_information': ['仍缺厚度表头与该单元格的对应依据。'],
              'requirement_outcomes': [
                  dict(requirement_id='q1', status='fulfilled', gap_kind='none', reason='已读原文。'),
                  dict(requirement_id='q2', status='unresolved', gap_kind='unread_source', reason='厚度未读。'),
                  dict(requirement_id='q3', status='partial', gap_kind='ambiguous_scope', reason='适用楼层未确定。')]}
    result = session.recover_valid_claims(json.dumps(answer))
    assert result is not None
    for gap in gaps:
        assert gap['text'] in result['answer']
    outcomes = {o['requirement_id']: o for o in result['requirement_outcomes']}
    assert outcomes['q2']['gap_kind'] == 'unread_source'
    assert outcomes['q3']['gap_kind'] == 'ambiguous_scope'
    assert not any(o['status'] == 'fulfilled' for o in outcomes.values())
    assert not any(o['gap_kind'] == 'unsupported_capability' for o in outcomes.values())
    assert '仍缺厚度表头与该单元格的对应依据。' in result['missing_information']


def test_unknown_only_recovery_retains_gaps_without_inventing_facts(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes)
    result = session.recover_valid_claims(json.dumps({'claims': [
        dict(text='缺少材料与楼层的对应依据。', state='unknown', question_part_ids=['q1']),
        fact | {'text': '上述厚度为987654mm。'}]}))
    assert result['recovery']['kept_claims'] == 0
    assert all(c['state'] == 'unknown' for c in result['claims'])
    assert result['task']['status'] == 'partial'
    assert '缺少材料与楼层的对应依据。' in result['answer']


def test_recovery_does_not_rewrite_dependent_claims_or_publish_proposals(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes)
    answer = {'claims': [fact,
        fact | {'text': '上述构造可以将地下室边界拉直。', 'state': 'derived'},
        fact | {'text': '前述原图材料可作为结论。', 'state': 'derived'}]}
    result = session.recover_valid_claims(json.dumps(answer))
    assert result['recovery']['rejected_claims'] == 2
    assert '上述' not in result['answer'] and '前述' not in result['answer']
    assert '拉直' not in result['answer']


@pytest.mark.parametrize('case, quote', [('Q12-A', '430辆'), ('Q14-A', '集水坑C')])
def test_frozen_read_sources_are_visible_but_never_count_as_analysis(case, quote):
    root = Path(__file__).resolve().parents[1] / 'artifacts/designer-facts-latest-20261019'
    if not (root / f'{case}-run.json').exists():
        pytest.skip('optional immutable experiment artifacts are not installed')
    run = json.loads((root / f'{case}-run.json').read_text())
    task = json.loads((root / f'{case}-task.json').read_text())
    original = deepcopy(run['result'])
    session = SimpleNamespace(task=SimpleNamespace(data=task), evidence=task['evidence'])
    result = append_source_excerpts(session, deepcopy(original))
    # Legacy read material remains in the archived task, not auto-published.
    assert result['answer'] == original['answer']
    assert result['task'] == original['task']
    assert result['requirement_outcomes'] == original['requirement_outcomes']
    selected = {eid for c in result['claims'] for eid in c.get('evidence_ids', [])}
    assert {e['evidence_id'] for e in result['citations']} == selected
    assert any(quote in e.get('quote', '') for e in task['evidence'].values())


def test_excerpt_does_not_publish_unowned_or_bare_numeric_source():
    evidence = {'name': {'evidence_id': 'name', 'quote': '集水坑C'},
                'number': {'evidence_id': 'number', 'quote': '2000'},
                'unowned': {'evidence_id': 'unowned', 'quote': '不相关图纸材料'}}
    session = SimpleNamespace(evidence=evidence, task=SimpleNamespace(data={
        'subtasks': [{'id': 'q1', 'evidence_ids': ['name', 'number']}]}))
    result = append_source_excerpts(session, {'answer': '范围未定', 'claims': [],
                                             'citations': [], 'task': {'status': 'partial'}})
    assert not result.get('source_excerpts')
    assert result['citations'] == []
    assert result['delivery_manifest']['selected_evidence_ids'] == []
    assert '2000' not in result['answer'] and '不相关' not in result['answer']


@pytest.mark.parametrize('replacement', [
    {'text': '该原文的对象归属尚未确定。', 'state': 'unknown'},
    {'text': '该来源存在适用范围冲突。', 'state': 'conflict'},
    {'text': '修正为未绑定的987654mm。', 'state': 'observed'},
])
def test_later_retraction_or_same_source_revision_does_not_revive_cached_fact(workspace, demo_bytes, replacement):
    session, fact = read_fact(workspace, demo_bytes)
    with pytest.raises(AnswerValidationError) as error:
        session.validate_answer(json.dumps({'claims': [fact, fact | {'text': '厚度987654mm'}]}))
    session.record_validation_issue(error.value)
    revised = fact | replacement
    result = session.recover_valid_claims(json.dumps({'claims': [revised]}))
    if result:
        assert fact['text'] not in [c['text'] for c in result['claims']]
    else:
        assert replacement['state'] == 'observed'


def test_validation_failure_is_not_reported_as_capability_or_budget_failure(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes)
    result = session.recover_valid_claims(json.dumps({'claims': [fact, fact | {'text': '厚度987654mm'}]}))
    assert result['requirement_outcomes'][0]['gap_kind'] == 'validation_failed'
    assert '校验或整理未完成' in result['requirement_outcomes'][0]['reason']


def test_truncated_claims_do_not_count_as_retained_facts(workspace, demo_bytes):
    session, fact = read_fact(workspace, demo_bytes, '请①读取材料；②读取厚度。')
    claims = [fact | {'text': f"原文摘录{' ' * n}：" + fact['text']} for n in range(11)]
    claims.append(fact | {'question_part_ids': ['q2']})
    result = session.recover_valid_claims(json.dumps({'claims': claims}))
    outcome = next(o for o in result['requirement_outcomes'] if o['requirement_id'] == 'q2')
    assert outcome['status'] == 'unresolved'
    assert not any(c['state'] != 'unknown' and 'q2' in c['question_part_ids'] for c in result['claims'])


def test_ui_displays_source_excerpts_separately_using_text_nodes():
    script = (Path(__file__).resolve().parents[1] / 'web/app.js').read_text()
    block = script.split('if (result.source_excerpts?.length)', 1)[1].split('if(result.task)', 1)[0]
    assert 'excerpt.quote' in block and 'excerpt.citation_number' in block
    assert '不计为完成' in block
    assert 'node("p", null,' in block
    assert 'innerHTML' not in block


def test_explicit_block_does_not_hide_other_runnable_requirements(workspace, demo_bytes, monkeypatch):
    session, _ = read_fact(workspace, demo_bytes, '请①读取材料；②读取厚度。')
    monkeypatch.setattr(session.task, 'pending_work', lambda: [
        {'operation': 'read_requested_fact', 'task_ids': ['q1']},
        {'operation': 'read_requested_fact', 'task_ids': ['q2']}])
    content = {'claims': [
        {'text': '尚缺用户指定的关联材料图纸。', 'state': 'unknown', 'question_part_ids': ['q1']},
        {'text': '厚度条文尚未读取。', 'state': 'unknown', 'question_part_ids': ['q2']}],
        'requirement_outcomes': [
            {'requirement_id': 'q1', 'status': 'unresolved', 'gap_kind': 'missing_input', 'reason': '缺少用户指定的关联图纸。'},
            {'requirement_id': 'q2', 'status': 'unresolved', 'gap_kind': 'unread_source', 'reason': '厚度条文尚未读取。'}]}
    with pytest.raises(ValueError, match='TASK_WORK_PENDING') as error:
        session.finish(json.dumps(content))
    assert 'q2' in str(error.value) and 'q1' not in str(error.value)
    content['requirement_outcomes'][1].update(gap_kind='missing_input', reason='缺少厚度所在的关联图纸。')
    content['claims'][1]['text'] = '缺少厚度所在的关联图纸。'
    result = session.finish(json.dumps(content))
    assert result['task']['status'] == 'partial'
