"""Operand provenance and partial, source-bound arithmetic (no model calls)."""
from types import SimpleNamespace
from copy import deepcopy
import pytest
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts
from cad_agent.analysis.calculation_workflow import outstanding, record_calculation


def session(question='逐区计算东库和西库的面积/车位数。'):
    return SimpleNamespace(request=SimpleNamespace(question=question), observations={},
        evidence={
            'east':dict(quote='东库面积100㎡，停车数5辆',snapshot_id='s',layout_id='l',frame_id='f',object_id='east'),
            'west':dict(quote='西库面积180㎡，停车数6辆',snapshot_id='s',layout_id='l',frame_id='f',object_id='west')})


def q(e,value,unit,**kw):
    return dict(origin='source',evidence_id=e,value=value,unit=unit,**kw)


def run(s,operation,quantities,**kw):
    return calculate_facts(s,CalculateFacts(operation=operation,quantities=quantities,**kw))


def test_source_label_cannot_launder_other_object_and_partial_is_independent():
    s=session()
    r=run(s,'divide',[q('east','100','m2'),q('west','6','count')],target='东库',operand_relation='same_object')
    assert r['status']=='missing_operands' and r['missing_operands'][0]['label']=='同一对象的操作数对应关系'
    ok=run(s,'divide',[q('east','100','m2'),q('east','5','count')],target='东库',operand_relation='same_object')
    assert ok['value']=='20.000' and ok['target_binding_status']=='source_bound'
    assert ok['lineage'][0]['snapshot_id']=='s' and ok['lineage'][0]['object_id']=='east'
    # The same arithmetic is allowed as independently labelled numbers, without
    # claiming correspondence or completing per-zone facts.
    raw=run(s,'divide',[q('east','100','m2'),q('west','6','count')],target='东库')
    assert raw['value']=='16.667' and raw['target_binding_status']=='unconfirmed'
    data={};record_calculation(data,['q1'],'calculate_facts',raw)
    pending=outstanding(data,dict(id='q1',operations=['arithmetic'],requirement=s.request.question,operation_receipts=[]))
    assert any(p['operation']=='calculation_target_coverage' for p in pending)


def test_source_single_subject_is_reachable_without_object_registry():
    s=session()
    for e in s.evidence.values():e.pop('object_id')
    r=run(s,'divide',[q('east','100','m2'),q('east','5','count')],target='东库',operand_relation='same_object')
    assert r['status']=='calculated' and r['relation_status']=='source_bound'
    s.evidence['east']['quote']='东库面积100㎡，西库停车数5辆'
    r=run(s,'divide',[q('east','100','m2'),q('east','5','count')],target='东库',operand_relation='same_object')
    assert r['status']=='missing_operands'


@pytest.mark.parametrize('field,value',[('object_id','fabricated'),('scope_id','invented')])
def test_model_constraints_must_match_tool_source(field,value):
    with pytest.raises(ValueError,match='未与来源记录绑定'):
        run(session(),'sum',[q('east','100','m2',**{field:value})])


def test_scope_and_snapshot_conflicts_not_hidden_by_repeated_name_or_id():
    s=session();s.evidence['copy']=dict(s.evidence['east'],frame_id='other',snapshot_id='other_drawing')
    qs=[q('east','100','m2'),q('copy','100','m2')]
    for relation in ('same_scope','same_object'):
        r=run(s,'subtract',qs,target='东库',operand_relation=relation)
        assert r['status']=='missing_operands' and r['target_binding_status']=='unconfirmed'
    assert run(s,'subtract',qs)['value']=='0.000'


def test_user_condition_and_unit_conversion_survive_entire_chain():
    s=session('用户给定参考1500mm。')
    s.evidence['x']=dict(quote='墙体长度约2m',object_id='wall',snapshot_id='s')
    first=run(s,'subtract',[q('x','2','m'),dict(origin='user_input',question_quote='用户给定参考1500mm',value='1500',unit='mm')])
    assert first['value']=='0.500' and first['unit']=='m'
    s.observations['first']=dict(tool='calculate_facts',data=first)
    second=run(s,'sum',[dict(origin='calculation',observation_id='first',value='0.500',unit='m')])
    assert second['conditional'] and second['approximate'] and len(second['lineage'])==2
    assert second['lineage'][1]['origin']=='user_input' and second['lineage'][1]['quote']=='用户给定参考1500mm'
    assert second['inputs'][0]['upstream_unit']=='m'
    with pytest.raises(ValueError,match='值或单位不符'):
        run(s,'sum',[dict(origin='calculation',observation_id='first',value='0.500',unit='mm')])


def test_intermediate_rounding_does_not_accumulate_into_false_difference():
    s=session();s.evidence['x']=dict(quote='1㎡，3辆')
    first=run(s,'divide',[q('x','1','m2'),q('x','3','count')],decimal_places=2)
    s.observations['a']=dict(tool='calculate_facts',data=first)
    second=run(s,'sum',[dict(origin='calculation',observation_id='a',value='0.33',unit='m2/count')]*3,decimal_places=6)
    assert second['value']=='1.000000' and first['unrounded_value'].startswith('0.333333')


@pytest.mark.parametrize('quote,value,unit',[
    ('面积10mm2','10','mm'),('指标35㎡/车','35','m2'),('长度5m','5',''),
    ('编号3，面积100㎡','3',''),('分区3#-1/2','3',''),('编号3，系数0.5','3','ratio'),
])
def test_ids_unit_prefix_and_dropped_units_are_not_operands(quote,value,unit):
    s=session();s.evidence['bad']={'quote':quote}
    with pytest.raises(ValueError):run(s,'sum',[q('bad',value,unit)])


def test_measurement_candidate_stays_candidate_and_unknown_measurement_cannot_be_promoted():
    s=session();s.observations['m']=dict(tool='measure_room_area',data=dict(
        status='needs_review',candidate_area_m2=12,measurement_basis='candidate_polygon',
        snapshot_id='s',frame_id='f',object_id='polygon_1'))
    r=run(s,'sum',[dict(origin='measurement',observation_id='m',field='candidate_area_m2',value='12',unit='m2')])
    assert r['value']=='12.000' and r['candidate_inputs']
    assert r['lineage'][0]['object_id']=='polygon_1' and r['lineage'][0]['field']=='candidate_area_m2'
    s.observations['m']['data'].update(status='unresolved',area_m2=12)
    with pytest.raises(ValueError,match='状态未确认'):
        run(s,'sum',[dict(origin='measurement',observation_id='m',field='area_m2',value='12',unit='m2')])


def test_same_key_cannot_replace_a_known_operand_with_another_object():
    s=session();s.task=SimpleNamespace(data={});s.trace=[dict(task_ids=['q1'])]
    r=run(s,'subtract',[q('east','100','m2')],target='差值',calculation_key='difference',
        missing_operands=[dict(label='另一面积',reason='尚未读取')])
    record_calculation(s.task.data,['q1'],'calculate_facts',r)
    with pytest.raises(ValueError,match='已绑定操作数来源'):
        run(s,'subtract',[q('west','180','m2'),q('east','100','m2')],target='差值',calculation_key='difference')
    r=run(s,'subtract',[q('east','100','m2'),q('west','180','m2')],target='差值',calculation_key='difference')
    assert r['value']=='-80.000'


def test_same_position_requirement_cannot_complete_from_unbound_numeric_difference():
    s=session('同一部位逐项计算标高差值。');s.evidence={'a':dict(quote='-0.100'),'b':dict(quote='-1.600')}
    r=run(s,'subtract',[q('a','-0.100',''),q('b','-1.600','')])
    assert r['value']=='1.500' and r['unit']==''
    data={};record_calculation(data,['q1'],'calculate_facts',r)
    step=dict(id='q1',operations=['arithmetic'],requirement=s.request.question,operation_receipts=r['operation_receipts'])
    assert any(p['operation']=='calculation_identity' for p in outstanding(data,step))


def test_source_and_measurement_share_only_verified_object_identity():
    s=session();s.observations['m']=dict(tool='measure_geometry',data=dict(
        status='measured',value=102,unit='m2',object_id='east',snapshot_id='s',layout_id='l',frame_id='f'))
    measure=dict(origin='measurement',observation_id='m',value='102',unit='m2',object_id='east')
    r=run(s,'subtract',[measure,q('east','100','m2')],operand_relation='same_object',target='east')
    assert r['value']=='2.000' and r['relation_status']=='source_bound'
    r=run(s,'subtract',[measure,q('west','180','m2')],operand_relation='same_object',target='east')
    assert r['status']=='missing_operands'


def test_same_object_with_unknown_units_does_not_gain_engineering_status():
    s=session();s.evidence={k:dict(quote=value,object_id='known',snapshot_id='s')
        for k,value in [('a','-0.100'),('b','-1.600')]}
    r=run(s,'subtract',[q('a','-0.100',''),q('b','-1.600','')],operand_relation='same_object')
    assert r['status']=='missing_operands' and r['relation_status']=='unresolved'
    assert [m['label'] for m in r['missing_operands']]==['操作数1的单位','操作数2的单位']


def test_cross_scope_arithmetic_does_not_supply_per_target_coverage():
    s=session();s.evidence['east']['frame_id']='first';s.evidence['copy']=dict(s.evidence['east'],frame_id='second')
    r=run(s,'divide',[q('east','100','m2'),q('copy','5','count')],target='东库')
    assert r['value']=='20.000' and r['target_binding_status']=='unconfirmed'


@pytest.mark.parametrize('case',['Q06-A','Q12-B','Q16-B'])
def test_repaired_frozen_real_source_calculations(case):
    from scripts.replay_b07_calculations import replay
    result=replay(case)
    assert result['inputs_unchanged'] and result['live_model_calls']==0
    assert result['source_spans_revalidated']>0 and result['input_sha256']
    if case=='Q16-B':
        assert len([r for r in result['results'] if r['result']['status']=='calculated'])==3
    else:
        assert all(r['result']['status']=='missing_operands' for r in result['results'])


def test_missing_unknown_binding_can_be_completed_under_same_formula_key():
    s=session();s.task=SimpleNamespace(data={});s.trace=[dict(task_ids=['q1'])]
    s.evidence['unbound']=dict(quote='100㎡')
    r=run(s,'divide',[q('unbound','100','m2')],target='东库',calculation_key='east_ratio',
        operand_relation='same_object',missing_operands=[dict(label='同区停车数',reason='尚未读取')])
    record_calculation(s.task.data,['q1'],'calculate_facts',r)
    r=run(s,'divide',[q('east','100','m2'),q('east','5','count')],target='东库',calculation_key='east_ratio',operand_relation='same_object')
    record_calculation(s.task.data,['q1'],'calculate_facts',r)
    assert r['value']=='20.000' and r['target_binding_status']=='source_bound'
    assert len(s.task.data['calculation_workflows'])==1
