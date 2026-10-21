"""Unfinished source navigation and text arithmetic must remain executable."""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from cad_agent.analysis.calculation_workflow import refresh_calculation_inputs, calculation_input_context
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts, calculation_tokens
from cad_agent.analysis.scheduling import execution_state, schedule
from cad_agent.analysis.recall import pending_calls
from test_text_reading_repair import calculation_session, frozen_session, quantity


def test_unit_inventory_exposes_validated_quantities_without_execution():
    session = calculation_session()
    data = dict(question=session.request.question, evidence=session.evidence, observations={}, subtasks=[
        dict(id='q1', requirement=session.request.question, operations=['arithmetic'], evidence_ids=['a','b'], observation_ids=[])])
    inventory = refresh_calculation_inputs(data)['q1']
    rows = [r for r in calculation_input_context(inventory)['operands'] if r['reference'] in ('a','b')]
    assert {r['value'] for r in rows} == {'5','10'}
    assert all(r['unit']=='mm' and r['status']=='available_numeric' and r['quantity']['unit_evidence_id']=='units' for r in rows)
    assert all(r['unit_binding']['evidence_id']=='units' for r in rows)
    assert not data.get('calculation_workflows') and not data['observations']
    session.evidence['units']['snapshot_id']='another'
    rows=refresh_calculation_inputs(data)['q1']['operands']
    assert all(r['status']=='unit_unconfirmed' for r in rows if r['reference'] in ('a','b'))


def test_validated_text_units_have_receipt_but_missing_input_does_not():
    session=calculation_session()
    result=calculate_facts(session,CalculateFacts(operation='sum',quantities=[quantity('a','5'),quantity('b','10')]))
    assert any(r['operation']=='units' and r['fulfilled'] for r in result['operation_receipts'])
    result=calculate_facts(session,CalculateFacts(operation='sum',quantities=[quantity('a','5')],
        missing_operands=[dict(label='未知保温层',reason='未读到该层厚度')]))
    assert result['status']=='missing_operands' and not any(r['fulfilled'] for r in result['operation_receipts'])
    assert next(calculation_tokens('1、5厚砂浆'))['status']=='identifier'


def test_progress_reserve_bounded_and_repeated_read_does_not_stop_calculation(workspace):
    s=frozen_session(workspace,'snap_ef95ba940715f9f93396f465','计算原文厚度减用户给定20mm的差值。')
    step=s.task.data['subtasks'][0]
    step.update(calls=12,progress_call=12,action_progress_tracking=True,stalled=0,status='pending',operations=['arithmetic','units'])
    assert execution_state(s.task.data,'q1')['executable']
    step.update(calls=16,progress_call=16)
    assert not execution_state(s.task.data,'q1')['executable']
    step.update(calls=4,progress_call=2,stalled=2,repeated_actions={'old':2})
    with pytest.raises(ValueError,match='REPEATED_SOURCE_ACTION'):s.task.start(['q1'],action_key='old')
    s.task.start(['q1'],action_key='calculate')
    unit_work=next(w for w in schedule(s) if w['operation']=='units')
    assert 'calculate_facts' in unit_work['next_action']['tools']
    assert 'resolve_room_units' not in unit_work['next_action']['tools']


def test_real_q08_returned_stair_candidate_prioritized_and_unit_inventory_visible():
    path=Path(__file__).resolve().parents[1]/'artifacts/text-repair-full12-20261021/attempts/Q08-B-sol-low/0003/task.json'
    if not path.exists():pytest.skip('optional immutable Q08 snapshot not installed')
    data=json.loads(path.read_text())
    for s in data['subtasks']:
        s.update(status='pending',calls=10,stalled=0,delivered=False,reason=None)
    calls=pending_calls(data)
    target=[c for c in calls if c['arguments'].get('region_id')=='region_d0b266b4c9f01c69f3c8afff']
    assert target and target[0]['priority']==1 and '7A' in target[0]['source_preview']
    assert calls.index(target[0])<4
    inventory=calculation_input_context(refresh_calculation_inputs(data)['q2'])
    row=next(r for r in inventory['operands'] if r['value']=='96' and r['quantity']['origin']=='source')
    assert row['unit']=='mm' and row['quantity']['unit_evidence_id']
    assert row['target_binding_status']=='unconfirmed'


def test_calculation_review_requires_explicit_execution_or_missing_inputs(workspace):
    from cad_agent.analysis.calculation_workflow import execution_reviews
    s=frozen_session(workspace,'snap_ef95ba940715f9f93396f465','计算厚度减用户给定20mm的差值。')
    step=s.task.data['subtasks'][0]
    step.update(operations=['arithmetic'],evidence_ids=['a','b'])
    inputs=calculation_session()
    s.evidence.update(inputs.evidence)
    s.task.data['evidence'].update(inputs.evidence)
    refresh_calculation_inputs(s.task.data)
    reviews=execution_reviews(s.task.data)
    assert len(reviews)==1 and reviews[0]['required_operations']==['subtract']
    assert not s.task.data.get('calculation_workflows')
    blocked=json.loads(s.invoke_tool('search_sources',dict(query='其他对象',task_ids=['q1'])))
    assert blocked['error']['code']=='CALCULATION_REVIEW_REQUIRED'
    result=json.loads(s.invoke_tool('calculate_facts',dict(operation='subtract',calculation_key='review',
        missing_operands=[dict(label='完整厚度',reason='仅知部分层，不能确认完整厚度')],task_ids=['q1'])))
    assert not result.get('error') and result['status']=='missing_operands'
    assert not execution_reviews(s.task.data)
    allowed=json.loads(s.invoke_tool('search_sources',dict(query='其他对象',task_ids=['q1'])))
    assert not allowed.get('error')
    assert s.task.missing_operations(step)==['arithmetic']


def test_later_arithmetic_can_review_shared_read_dimensions_without_auto_join():
    from cad_agent.analysis.calculation_workflow import execution_reviews
    s=calculation_session()
    question='先读取墙面做法。用户给定20mm，请计算原图厚度减20mm的差值，并记录缺项。'
    data=dict(question=question,evidence=s.evidence,observations={},subtasks=[
        dict(id='q1',requirement='先读取墙面做法。',operations=[],evidence_ids=['a','b'],observation_ids=[],status='pending'),
        dict(id='q2',requirement='用户给定20mm，请计算原图厚度减20mm的差值',operations=['arithmetic'],evidence_ids=[],observation_ids=[],status='pending')])
    inv=refresh_calculation_inputs(data)['q2']
    rows=[r for r in inv['operands'] if r['quantity']['origin']=='source']
    assert rows and all(r['source_task_ids']==['q1'] and r['reuse_status']=='shared_read_requires_selection' for r in rows)
    assert all(r['target_binding_status']=='unconfirmed' for r in rows)
    assert not inv['next_calls'] and not data.get('calculation_workflows')
    assert execution_reviews(data)[0]['task_ids']==['q2']


def test_real_stair_heading_is_returned_on_first_page_and_read_route_is_explicit(workspace):
    s=frozen_session(workspace,'snap_ef95ba940715f9f93396f465','读取楼梯间面层做法与材料厚度。')
    r=json.loads(s.invoke_tool('search_sources',dict(query='楼梯间',limit=5,task_ids=['q1'])))
    target='region_d0b266b4c9f01c69f3c8afff'
    assert any(i['region_id']==target and i.get('assembly_heading') for i in r['items'])
    calls=pending_calls(s.task.data)
    call=next(c for c in calls if c['arguments'].get('region_id')==target)
    assert call['tool']=='read_assembly'
    read=json.loads(s.invoke_tool(call['tool'],call['arguments']|dict(limit=12,task_ids=['q1'])))
    assert not read.get('error')
    assert any(e.get('region_id')==target for e in s.evidence.values())
    again=json.loads(s.invoke_tool('read_assembly',dict(region_id=target,limit=12,task_ids=['q1'])))
    assert again['error']['code']=='SOURCE_PAGE_ALREADY_READ'
    assert not any(c.get('tool')=='read_assembly' and c['arguments'].get('view','record')=='record' and c['arguments'].get('region_id')==target for c in pending_calls(s.task.data))
