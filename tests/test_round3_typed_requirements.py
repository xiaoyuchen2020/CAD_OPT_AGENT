"""Offline producer/acceptance routing, including frozen real plan evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from cad_agent.analysis.coverage import record_cells
from cad_agent.analysis.recall import pending_calls
from cad_agent.analysis.tasks import TaskLedger, TaskPlan
from cad_agent.analysis.typed_requirements import annotate_cell, annotate_step, requirement_type

FROZEN = json.loads(Path(__file__).with_name('test_round3_typed_requirements_frozen.json').read_text())


def cell(obj, field):
    return dict(object=obj, field=field, state='unread', evidence_ids=[])


@pytest.mark.parametrize('question,obj,field,expected', [
    ('分别标明用户给定值与原图值。', '用户给定值', '分别标明', 'DELIVERY_REQUIREMENT'),
    ('将输入参考值和图面读数分列。', '输入参考值', '分列', 'DELIVERY_REQUIREMENT'),
    ('说明已核查区域及未覆盖部分。', '已核查区域', '说明', 'DELIVERY_REQUIREMENT'),
    ('报告已检查的范围。', '已检查的范围', '已检查的范围', 'DELIVERY_REQUIREMENT'),
    ('定位构件甲的实际边界。', '构件甲', '定位', 'SPATIAL_FACT'),
    ('核查构件甲与构件乙的重叠关系。', '构件甲与构件乙', '重叠关系', 'DERIVED_MEASURE'),
    ('核查构件丙与构件丁的相交。', '构件丙与构件丁', '相交', 'DERIVED_MEASURE'),
    ('列出构件甲与构件乙、构件丙的重叠关系。', '构件甲', '构件乙', 'DERIVED_MEASURE'),
    ('分别记录已确认几何与候选归属。', '对象', '已确认几何', 'DELIVERY_REQUIREMENT'),
    ('分别记录已确认几何与候选归属。', '候选归属', '记录', 'DELIVERY_REQUIREMENT'),
    ('请记录尚缺边界的对象。', '尚缺边界的对象', '记录', 'DELIVERY_REQUIREMENT'),
    ('测量构件甲的面积。', '构件甲', '面积', 'DERIVED_MEASURE'),
    ('计算构件甲相对用户给定值的差值。', '构件甲', '差值', 'DERIVED_CALCULATION'),
    ('求构件甲各层的总厚度。', '构件甲', '总厚度', 'DERIVED_CALCULATION'),
    ('用户提供的参考值是80㎡。', '用户提供的参考值', '80㎡', 'USER_CONDITION'),
    ('读取图纸标明的构件甲的厚度。', '构件甲', '厚度', 'SOURCE_FACT'),
    ('读取图纸分别标明两间房的面积。', '两间房', '分别标明的面积', 'SOURCE_FACT'),
    ('读取构件甲分别标明的厚度。', '构件甲', '分别标明的厚度', 'SOURCE_FACT'),
    ('读取标注中的总厚度。', '构件甲', '总厚度', 'SOURCE_FACT'),
    ('读取构件甲厚度差原文。', '构件甲', '厚度差原文', 'SOURCE_FACT'),
    ('摘录构件甲的定位尺寸。', '构件甲', '定位尺寸', 'SOURCE_FACT'),
    ('读取图纸标注的构件甲净宽。', '构件甲', '净宽', 'SOURCE_FACT'),
    ('定位相关文字。', '相关文字', '定位', 'SOURCE_FACT'),
    ('定位楼地面表。', '楼地面表', '定位', 'SOURCE_FACT'),
    ('核查仅供消防的风机房并单独标明。', '仅供消防的风机房', '单独标明', 'SOURCE_FACT'),
])
def test_clause_types_are_not_drawing_specific(question, obj, field, expected):
    assert requirement_type(cell(obj, field), dict(requirement=question)) == expected


def test_mixed_requirement_preserves_original_source_producer():
    step = dict(id='q1', requirement='读取墙体的标注厚度，并将用户给定值与原图值分别标明。',
                coverage=[cell('墙体', '标注厚度'), cell('用户给定值', '分别标明')])
    annotate_step(step)
    assert step['requirement_types'] == ['DELIVERY_REQUIREMENT', 'SOURCE_FACT']
    data = dict(subtasks=[step | dict(status='pending')])
    calls = pending_calls(data)
    # Object-first discovery preserves recall when property wording differs.
    assert [c['arguments'] for c in calls] == [dict(query='墙体', limit=3)]


def test_declared_acceptance_type_cannot_erase_source_obligation():
    c = cell('墙体', '材料') | dict(declared_requirement_type='DELIVERY_REQUIREMENT')
    assert requirement_type(c, dict(requirement='读取墙体材料。')) == 'SOURCE_FACT'
    c = cell('墙体', '差值') | dict(declared_requirement_type='SOURCE_FACT')
    assert requirement_type(c, dict(requirement='计算墙体差值。')) == 'DERIVED_CALCULATION'


def test_planning_types_preserve_literal_binding_and_do_not_execute():
    question = '读取墙体的标注厚度，计算墙体总厚度，并将用户给定值与原图值分别标明。'
    ledger = object.__new__(TaskLedger)
    ledger.data = dict(question=question, planned=False, subtasks=[dict(id='q1', requirement=question,
        kind='lookup', status='pending', delivered=False, coverage=[], operations=[], operation_receipts=[])])
    ledger.save = lambda: None
    ledger.public = lambda: deepcopy(ledger.data)
    ledger.plan(TaskPlan.model_validate(dict(steps=[dict(requirement_id='q1',kind='lookup',acceptance='逐项读取和计算',
        coverage=[dict(object='墙体',field='标注厚度',requirement_type='SOURCE_FACT'),
                  dict(object='墙体',field='总厚度',requirement_type='DERIVED_CALCULATION'),
                  dict(object='用户给定值',field='分别标明',requirement_type='DELIVERY_REQUIREMENT')])])))
    step = ledger.data['subtasks'][0]
    assert 'arithmetic' in step['operations'] and not step['operation_receipts']
    assert not step['delivered'] and step['status'] == 'pending'
    assert all(c['state'] == 'unread' and not c['evidence_ids'] for c in step['coverage'])
    for c in step['coverage']:
        for bindings in c['question_binding'].values():
            for binding in bindings:
                start, end = binding['requirement_span']
                assert question[start:end] == binding['quote']


def test_prose_match_cannot_produce_computation_or_delivery_evidence():
    cells = [annotate_cell(cell('墙体', '差值')), annotate_cell(cell('墙体', '重叠关系')),
             annotate_cell(cell('用户给定值', '分别标明')), annotate_cell(cell('墙体', '材料'))]
    evidence = {'ev1':dict(quote='墙体差值10，重叠关系确认，材料混凝土。用户给定值分别标明。')}
    record_cells(cells, {}, evidence)
    assert all(c['state'] == 'unread' and c['evidence_ids'] == [] for c in cells[:3])
    assert cells[3]['state'] == 'source_candidate' and cells[3]['evidence_ids'] == ['ev1']


@pytest.mark.parametrize('case', FROZEN, ids=lambda case: case['id'])
def test_frozen_real_plans_route_without_mutating_evidence_or_completing(case):
    task = deepcopy(case)
    for step in task['subtasks']:
        previous = deepcopy(step)
        annotate_step(step)
        for key in ('requirement', 'status', 'delivered', 'evidence_ids', 'operation_receipts'):
            if key in previous:
                assert step[key] == previous[key]
        for old, new in zip(previous.get('coverage', []), step.get('coverage', [])):
            for key in ('state', 'evidence_ids', 'question_binding', 'object', 'field'):
                if key in old:assert new[key] == old[key]
        step.update(status='pending', calls=0, stalled=0)
    calls = pending_calls(task)
    for call in calls:
        if call['reason'] != 'explicit_object_field_gap':continue
        step = next(s for s in task['subtasks'] if s['id'] in call['task_ids'])
        target = next(c for c in step['coverage'] if (c['object'], c['field']) == (call['object'], call['field']))
        assert target['requirement_type'] == 'SOURCE_FACT'
    if case['id'].startswith('Q14'):
        targets = [c for s in task['subtasks'] for c in s.get('coverage', [])]
        assert all(c['requirement_type'] != 'SOURCE_FACT' for c in targets if c['field'] in ('定位','重叠关系','分别标明'))
        assert all('geometry' in s['operations'] for s in task['subtasks']
                   if any(c['requirement_type'] == 'SPATIAL_FACT' for c in s.get('coverage', [])))
        assert not any(c['arguments'].get('keywords') in (['定位'], ['重叠关系'], ['分别标明']) for c in calls)
    else:
        assert any(c['reason'] == 'explicit_object_field_gap' for c in calls)


def test_explicit_source_prerequisite_read_survives_spatial_type():
    """A literal name search can supply identity; do not erase that prerequisite."""
    step = dict(id='q1', requirement='定位构件甲。', status='pending', coverage=[cell('构件甲', '定位')])
    query = dict(query_id='q', task_ids=['q1'], tool='search_sources', arguments=dict(query='构件甲'),
                 first_unread_offset=1, total=2, candidates={'r':dict(source_spans=[dict(region_id='r',text_span=[0,3])],
                     region_ids=['r'], start=0, read_state='unread')})
    data = dict(snapshot_id='s', source_sha256='h', subtasks=[step],
        source_recall=dict(context=dict(snapshot_id='s',source_sha256='h'), queries={'q':query},read_spans={}))
    calls = pending_calls(data)
    assert {c['tool'] for c in calls} == {'read_source_context', 'search_sources'}
    assert not any(c['reason'] == 'explicit_object_field_gap' for c in calls)
    query['arguments']['keywords'] = ['定位']
    assert not pending_calls(data)  # stale synthesized field query is not paginated
    step['coverage'] = [cell('用户给定值', '分别标明')]
    assert not pending_calls(data)
