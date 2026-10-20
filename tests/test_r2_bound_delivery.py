"""Offline source-consumption regressions; original failed deliveries stay immutable."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from cad_agent.analysis.answer_protocol import delivery_consistency_issues
from cad_agent.analysis.boundary_context import synthesis_observations
from cad_agent.analysis.delivery import AnswerValidationError
from cad_agent.analysis.delivery_context import bounded_packet, _reference_ids
from cad_agent.analysis.efficiency import compact, dumps
from cad_agent.analysis.tool_registry import Claim
from cad_agent.analysis.assembly import read
from cad_agent.analysis.fact_tools import ReadAssembly
from scripts.replay_b03_answers import ReplaySession
from test_b02_assembly_binding import FrozenIndex, TABLE_SNAPSHOT, SECTION_SNAPSHOT

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'artifacts/designer-facts-b-final-20261019/attempts'


def load(case):
    folder = FROZEN / case / '0001'
    if not folder.exists():
        pytest.skip('optional immutable real drawing artifacts unavailable')
    return (json.loads((folder / 'run.json').read_text()),
            json.loads((folder / 'task.json').read_text()))


def synthesis_packet(task):
    # A reproducible lower-bound final context, not the unavailable historical
    # model prompt: runtime source_contexts/repair attempts were not persisted.
    return dict(question=task['question'], evidence=compact(list(task['evidence'].values())),
                observations=synthesis_observations(task['observations'], final=True),
                task={'subtasks': [{k: s[k] for k in ('id', 'requirement', 'status', 'reason', 'operations')}
                                   for s in task['subtasks']]}, source_contexts=[])


@pytest.mark.parametrize('case', ['Q02-B', 'Q05-B', 'Q08-B'])
def test_frozen_context_keeps_whole_read_records_with_all_citation_dependencies(case):
    _, task = load(case)
    packet = synthesis_packet(task)
    original = deepcopy(packet)
    out = bounded_packet(packet, 24000, task['subtasks'])
    assert packet == original and len(dumps(out)) <= 24000
    evidence = {e['evidence_id']: e for e in out['evidence']}
    records = [o for o in out['observations'] if o['tool'] == 'read_assembly']
    assert records
    for observation in records:
        assert _reference_ids(observation) <= evidence.keys()
        for eid in _reference_ids(observation):
            assert evidence[eid]['quote'] == task['evidence'][eid]['quote']
        binding = observation['data'].get('source_binding') or {}
        if binding.get('state') == 'source_bound':
            assert observation['data']['engineering_applicability_state'] == 'unverified'
            assert 'ownership' not in observation['data']['checks']
    if case == 'Q02-B':
        assert any('70厚C20细石保护层' in e['quote'] and '缝宽20mm。' in e['quote'] for e in evidence.values())
    else:
        assert any((o['data'].get('source_binding') or {}).get('state') == 'source_bound' for o in records)


@pytest.mark.parametrize('case,indices', [('Q02-B', [4]), ('Q05-B', [0]), ('Q08-B', [0, 1, 2])])
def test_actual_failed_clauses_cannot_deny_read_binding_or_continuous_source(case, indices):
    run, task = load(case)
    session = ReplaySession(run, task)
    for index in indices:
        claim = Claim.model_validate({k: v for k, v in run['result']['claims'][index].items() if k in Claim.model_fields})
        assert delivery_consistency_issues(claim, task['evidence'], task['observations'])
        payload = dict(claims=[claim.model_dump()], requirement_outcomes=run['result']['requirement_outcomes'])
        with pytest.raises(AnswerValidationError) as error:
            session.validate_answer(dumps(payload))
        assert not any(c['text'] == claim.text for c in error.value.validated_claims)


def test_complete_source_guard_never_uses_another_region_or_snapshot():
    run, task = load('Q02-B')
    claim = Claim.model_validate({k: v for k, v in run['result']['claims'][4].items() if k in Claim.model_fields})
    cited = {eid: task['evidence'][eid] for eid in claim.evidence_ids}
    whole = next(e for e in task['evidence'].values() if '70厚C20细石保护层' in e['quote'])
    for change in ({'region_id': 'other'}, {'snapshot_id': 'other'}, {'text_span': [100, 172]}):
        sources = cited | {whole['evidence_id']: whole | change}
        assert not delivery_consistency_issues(claim, sources, {})


def test_candidate_or_unread_sibling_does_not_gain_source_binding():
    run, task = load('Q08-B')
    claim = Claim.model_validate({k: v for k, v in run['result']['claims'][2].items() if k in Claim.model_fields})
    observations = deepcopy(task['observations'])
    for observation in observations.values():
        if observation['tool'] == 'read_assembly':
            observation['data']['source_binding']['state'] = 'candidate'
    assert not delivery_consistency_issues(claim, task['evidence'], observations)
    claim.text = '图中对象的工程实例适用性未确认，仍须与对应平面核对。'
    assert not delivery_consistency_issues(claim, task['evidence'], task['observations'])


@pytest.mark.parametrize('snapshot,rid', [
    (TABLE_SNAPSHOT, 'region_2ce04d7bc949ee948f56cba9'),
    (SECTION_SNAPSHOT, 'region_593e279bd02789f8e87337f6')])
def test_frozen_reparse_separates_source_ownership_from_instance_scope(snapshot, rid):
    index = FrozenIndex(snapshot)
    result = read(index, ReadAssembly(region_id=rid, limit=12))
    assert result['source_binding']['state'] == 'source_bound'
    assert result['checks']['source_ownership_state'] == 'source_bound'
    assert result['checks']['layer_order'] == result['source_binding']['layer_order']
    assert result['checks']['engineering_applicability_state'] == 'unverified'
    assert all(i['source_ownership_state'] == 'source_bound' and i['object_instance_state'] == 'unverified'
               for i in result['items'])


def test_atomic_record_not_published_when_citations_cannot_fit():
    _, task = load('Q08-B')
    packet = synthesis_packet(task)
    packet['task'] = {}; packet['question'] = '检查构造'
    out = bounded_packet(packet, 1800, [])
    assert len(dumps(out)) <= 1800
    assert not any(o['tool'] == 'read_assembly' for o in out['observations'])
    assert out['context_omissions']['observation_count'] > 0


def test_corrected_field_delivery_is_valid_without_promoting_missing_sibling_or_unit():
    run, task = load('Q08-B')
    session = ReplaySession(run, task)
    title = next(e for e in task['evidence'].values() if e['quote'] == '楼地面D3（汽车库）（燃烧性能等级A）')
    layer = next(e for e in task['evidence'].values() if e['quote'].startswith('2、96厚C25') and '缝深10mm' in e['quote'])
    adhesive = next(e for e in task['evidence'].values() if e['quote'].startswith('3、素水泥浆'))
    facts = [dict(text='楼地面D3（汽车库）的来源构造层：' + e['quote'],
                  state='derived', evidence_ids=[title['evidence_id'], e['evidence_id']],
                  question_part_ids=['q1']) for e in (layer, adhesive)]
    facts.append(dict(text='楼梯间完整做法尚未取得；96厚未明确厚度单位，尚不能与用户给定毫米值相减。',
                      state='unknown', question_part_ids=[s['id'] for s in task['subtasks']]))
    outcomes = [dict(requirement_id=s['id'], status='partial' if s['id']=='q1' else 'unresolved',
                     gap_kind='unread_source', reason='仍缺楼梯间做法或厚度单位依据。') for s in task['subtasks']]
    result = session.validate_answer(dumps(dict(claims=facts, requirement_outcomes=outcomes)))
    assert '96厚C25' in result['answer'] and '素水泥浆一道' in result['answer']
    assert result['claims'][0]['state'] == 'derived'
    assert all(o['status'] != 'fulfilled' for o in result['requirement_outcomes'])
    assert '26mm' not in result['answer']


def test_unbound_sibling_can_remain_unknown_next_to_bound_source_fact():
    run, task = load('Q08-B')
    claim = Claim.model_validate({k: v for k, v in run['result']['claims'][2].items() if k in Claim.model_fields})
    claim.text = '汽车库做法已建立来源内绑定；楼梯间做法归属未确认。'
    assert not delivery_consistency_issues(claim, task['evidence'], task['observations'])


def test_recovery_does_not_preserve_rejected_absence_as_requirement_reason():
    run, task = load('Q08-B')
    session = ReplaySession(run, task)
    original = run['result']
    payload = dict(claims=[{k: v for k, v in c.items() if k in Claim.model_fields} for c in original['claims']],
                   requirement_outcomes=original['requirement_outcomes'])
    result = session.recover_valid_claims(dumps(payload))
    assert result is not None
    affected = next(o for o in result['requirement_outcomes'] if o['requirement_id'] == 'q1')
    assert affected['gap_kind'] == 'validation_failed'
    assert '汽车库归属未确认' not in affected['reason']
    assert not any(o['status'] == 'fulfilled' for o in result['requirement_outcomes'])
