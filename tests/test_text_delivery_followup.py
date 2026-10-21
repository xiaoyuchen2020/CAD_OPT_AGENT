"""Regressions for conditional delivery, chained calculation and clause coverage."""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from cad_agent.analysis.calculation_workflow import execution_reviews, refresh_calculation_inputs
from cad_agent.analysis.delivery_recovery import same_calculation_delivery
from test_text_reading_repair import frozen_session, calculation_session, quantity

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'artifacts/text-execution-verified-20261021/attempts'


def test_missing_subtract_requeues_after_sum_but_not_unchanged_reads(workspace):
    s=frozen_session(workspace,'snap_ef95ba940715f9f93396f465','用户给定20mm，请计算厚度差值。')
    step=s.task.data['subtasks'][0]
    step.update(operations=['arithmetic'], evidence_ids=['a','b'])
    s.evidence.update({key: value | dict(evidence_id=key, snapshot_id=s.request.snapshot_id) for key, value in calculation_session().evidence.items()});s.task.data['evidence'].update(s.evidence)
    refresh_calculation_inputs(s.task.data)
    missing=dict(operation='subtract',calculation_key='difference',task_ids=['q1'],
        missing_operands=[dict(label='完整合计',reason='有未知层')])
    assert execution_reviews(s.task.data)
    assert json.loads(s.invoke_tool('calculate_facts',missing))['status']=='missing_operands'
    assert not execution_reviews(s.task.data)
    # Newly executed subtotal is a new eligible operand, with its own lineage.
    summed=json.loads(s.invoke_tool('calculate_facts',dict(operation='sum',task_ids=['q1'],
        quantities=[quantity('a','5'),quantity('b','10')])))
    assert summed['status']=='calculated'
    reviews=execution_reviews(s.task.data)
    assert reviews and reviews[0]['required_operations']==['subtract']
    difference=json.loads(s.invoke_tool('calculate_facts',dict(operation='subtract',task_ids=['q1'],
        calculation_key='conditional_subtotal_difference', quantities=[
            dict(origin='calculation',observation_id=summed['observation_id'],value='15',unit='mm'),
            dict(origin='user_input',question_quote='用户给定20mm',value='20',unit='mm')])))
    assert difference['value']=='-5.000' and not execution_reviews(s.task.data)
    assert s.task.data['calculation_workflows']['q1:difference']['status']=='missing_operands'
    # Malformed mixed-protocol prose must not erase the separately executed receipt.
    result=s.recover_valid_claims(json.dumps(dict(claims=[dict(text='完整总厚度未知',state='unknown',question_part_ids=['q1'])])))
    assert any(c['evidence_basis']=='tool_observation' and '-5.000mm' in c['text'] for c in result['claims'])
    assert result['task']['status']=='partial'


def test_explicit_user_comparison_recovered_without_promoting_facts(workspace):
    path=BASE/'Q08-B-sol-low/0001/task.json'
    if not path.exists():pytest.skip('optional real snapshot')
    task=json.loads(path.read_text())
    s=frozen_session(workspace,task['snapshot_id'],task['question'])
    s.task.data=deepcopy(task);s.task.data['execution_token']=None
    s.evidence.update(task['evidence']);s.observations.update(task['observations'])
    s.trace.append(dict(tool='read_assembly',status='completed'))
    claim=dict(text='汽车库D3原文96厚C25细石混凝土，内配Φ6@200双向钢筋网片；用户提及的旧版C30不代替原图C25。',
        state='observed',evidence_basis='source',evidence_ids=['ev_23dde70c29190a31e9ec4008','ev_5bc5be7744cf20101ac6e84c'],
        question_part_ids=['q1','q5'],user_inputs=[dict(quote='不用用户提及的旧版C30代替',value='C30')])
    result=s.recover_valid_claims(json.dumps(dict(claims=[claim])))
    assert any(c['text']==claim['text'] and c['state']=='derived' for c in result['claims'])
    bad=claim|dict(text=claim['text']+'总厚987654mm。')
    s._validated_delivery_claims=[]
    result=s.recover_valid_claims(json.dumps(dict(claims=[bad])))
    assert not result or not any(c['text']==bad['text'] for c in result['claims'])


def test_calculation_duplicate_requires_same_receipts_and_complete_results():
    obs={key:dict(tool='calculate_facts',data=dict(status='calculated',value=value)) for key,value in [('a','26.000'),('b','30.000')]}
    base=dict(state='derived',evidence_basis='tool_observation',observation_ids=['a','b'],question_part_ids=['q1'])
    left=base|dict(text='单层差26mm，两层小计差30mm，完整厚度未知。')
    assert same_calculation_delivery(left,base|dict(text='两种条件口径：30mm及26mm；均不表示完整厚度。'),obs)
    assert not same_calculation_delivery(left,base|dict(text='仅单层差26mm。'),obs)
    assert not same_calculation_delivery(left,base|dict(text=left['text'],question_part_ids=['q2']),obs)
    assert not same_calculation_delivery(left,base|dict(text=left['text'],state='unknown'),obs)


def test_original_q04_first_pages_include_cross_clause_conditions(workspace):
    path=BASE/'Q04-B-sol-low/0001/task.json'
    if not path.exists():pytest.skip('optional real snapshot')
    task=json.loads(path.read_text());s=frozen_session(workspace,task['snapshot_id'],task['question'])
    s.task.data=task
    for owner,target in [('q1','region_d406598772881eebb930a4bc'),('q6','region_8a3191a12750b2e9d7a4afbf')]:
        result=s.execute('read_requirement_sources',dict(requirement_id=owner,limit=6))
        assert target==result['items'][0]['region_id']
        assert all(i['attribution_state']=='unverified' for i in result['items'])
    from cad_agent.analysis.requirement_sources import required_reviews
    for step in task['subtasks']:step.update(status='pending',calls=1,stalled=0,delivered=False)
    required=required_reviews(task)
    assert any(c['task_ids']==['q6'] for c in required)
    task.setdefault('requirement_source_reads',{})['q6']=[dict(offset=0,has_more=True)]
    assert not any(c['task_ids']==['q6'] for c in required_reviews(task))


def test_missing_review_revision_cap_survives_new_read_values():
    from cad_agent.analysis.calculation_workflow import record_calculation
    from test_text_reading_repair import source
    s=calculation_session()
    data=dict(question='用户给定20mm，请计算厚度差值。',evidence=s.evidence,observations={},subtasks=[
        dict(id='q1',requirement='用户给定20mm，请计算厚度差值。',status='pending',operations=['arithmetic'],evidence_ids=['a','b'],observation_ids=[])])
    refresh_calculation_inputs(data)
    for attempt in range(3):
        assert execution_reviews(data)
        record_calculation(data,['q1'],'calculate_facts',dict(operation='subtract',calculation_key='missing',status='missing_operands'))
        assert not execution_reviews(data)
        eid='more'+str(attempt)
        data['evidence'][eid]=source(f'{attempt+21}mm厚砂浆')
        data['subtasks'][0]['evidence_ids'].append(eid)
        refresh_calculation_inputs(data)
    assert not execution_reviews(data)


def test_each_read_assembly_and_new_subtotal_has_own_review(workspace):
    from cad_agent.analysis.calculation_workflow import grouped_execution_reviews
    path=ROOT/'artifacts/text-delivery-followup-20261021/attempts/Q06-B-sol-low/0001/task.json'
    if not path.exists():pytest.skip('optional development snapshot')
    task=json.loads(path.read_text())
    for step in task['subtasks']:step.update(status='pending',calls=1,stalled=0,delivered=False)
    refresh_calculation_inputs(task)
    reviews=grouped_execution_reviews(task)
    second=[r for r in reviews if r['candidate_title'].startswith('2\n')]
    assert second and second[0]['required_operations']==['sum']
    assert not any(r['candidate_title'].startswith('3\n') for r in reviews)
    s=frozen_session(workspace,task['snapshot_id'],task['question'])
    s.task.data=task;s.task.data['execution_token']=None
    s.evidence.update(task['evidence']);s.observations.update(task['observations'])
    review=second[0]
    # A group-local missing decision unlocks reads without claiming arithmetic.
    missing=json.loads(s.invoke_tool('calculate_facts',dict(operation='sum',task_ids=review['task_ids'],
        review_group_id=review['review_group_id'],calculation_key='record2-review',
        missing_operands=[dict(label='材料选择',reason='记录归属还需核查')])) )
    assert missing['status']=='missing_operands'
    assert not any(r['review_group_id']==review['review_group_id'] for r in grouped_execution_reviews(s.task.data))
    # Subtraction for another row cannot acknowledge this candidate group.
    from cad_agent.analysis.fact_tools import CalculateFacts,calculate_facts
    wrong=next(o for o in s.observations.values() if o['tool']=='calculate_facts' and o['data'].get('operation')=='sum' and o['data'].get('status')=='calculated')
    with pytest.raises(ValueError,match='不属于该 review_group_id'):
        calculate_facts(s,CalculateFacts(operation='subtract',review_group_id=review['review_group_id'],
            quantities=[dict(origin='calculation',observation_id=wrong['observation_id'],value='15',unit='mm'),
                        dict(origin='user_input',question_quote='用户给定的算术参照为5厚抗裂砂浆+5厚聚合物防水砂浆+10厚找平砂浆=20mm',value='20',unit='mm')]))


def test_completed_single_layer_difference_does_not_hide_new_subtotal(workspace):
    from cad_agent.analysis.calculation_workflow import grouped_execution_reviews
    path=ROOT/'artifacts/text-delivery-followup-20261021/attempts/Q08-B-sol-low/0001/task.json'
    if not path.exists():pytest.skip('optional development snapshot')
    task=json.loads(path.read_text())
    for step in task['subtasks']:step.update(status='pending',calls=1,stalled=0,delivered=False)
    refresh_calculation_inputs(task)
    reviews=grouped_execution_reviews(task)
    assert len(reviews)==1 and reviews[0]['required_operations']==['subtract']
    assert reviews[0]['group_kind']=='calculated_subtotal'
    assert any(r['quantity'].get('origin')=='calculation' and float(r['value'])==100 for r in reviews[0]['input_context']['operands'])
