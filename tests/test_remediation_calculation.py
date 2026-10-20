"""Deterministic calculation routing, missing operands and immutable real sources."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import shutil
import sqlite3
import pytest
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts
from cad_agent.analysis.operations import infer_operations, planned_operations, receipts
from cad_agent.analysis.calculation_workflow import required_operations
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'artifacts/designer-facts-latest-20261019'


def simple(question='计算厚度差。'):
    return SimpleNamespace(evidence={'a':{'quote':'厚度约96mm（以现场复核为准）'},'b':{'quote':'厚度70mm'}},
        observations={},request=SimpleNamespace(question=question))


def q(eid,value,unit='mm',**kw):
    return dict(origin='source',evidence_id=eid,value=value,unit=unit,**kw)


def invoke(s,args):
    return json.loads(s.invoke_tool('calculate_facts',args))


def test_explicit_quotient_and_formula_requests_are_planned():
    for text in ['计算各分区面积/车位数及全库加权指标','求两种做法砂浆总厚度','按ceil(总面积/4000)计算下界及(总面积/4000)×2理论指标与实际数量的差异']:
        assert 'arithmetic' in infer_operations(text)
    assert required_operations('每区面积/车位数及全库加权指标，不能把比值简单平均')==['divide']
    assert set(required_operations('ceil(总面积/4000)与(总面积/4000)×2，实际数量的差异'))=={'ceil','divide','multiply','subtract'}


def test_annotation_reading_does_not_claim_requested_geometry():
    assert receipts('read_dimensions',{'items':[{'object_id':'unrelated'}]})==[
        {'operation':'dimensions','fulfilled':True,'tool':'read_dimensions','reference':None}]
    assert planned_operations('读取原生尺寸标注的净宽',['geometry'])==['dimensions']
    assert 'geometry' in planned_operations('读取原生尺寸并测量坡道净宽',[])


def test_approximate_inputs_and_full_source_qualifiers_survive():
    s=simple();r=calculate_facts(s,CalculateFacts(operation='subtract',quantities=[q('a','96',source_quote='96mm'),q('b','70')]))
    assert r['value']=='26.000' and r['approximate']
    assert r['inputs'][0]['quote']==s.evidence['a']['quote'] and '显示精度' in r['note']
    s.observations['o']=dict(tool='calculate_facts',data=r)
    r2=calculate_facts(s,CalculateFacts(operation='sum',quantities=[dict(origin='calculation',observation_id='o',value='26.000',unit='mm')]))
    assert r2['approximate']


def test_unit_cannot_be_borrowed_from_another_number():
    s=simple();s.evidence['a']['quote']='面积18700㎡，数量430辆'
    with pytest.raises(ValueError,match='直接绑定'):
        calculate_facts(s,CalculateFacts(operation='sum',quantities=[q('a','430','m2')]))
    with pytest.raises(ValueError,match='source_quote'):
        calculate_facts(s,CalculateFacts(operation='sum',quantities=[q('a','18700','m2',source_quote='面积18700mm')]))


def test_missing_operands_records_gap_without_arithmetic_or_guess():
    s=simple();args=CalculateFacts(operation='subtract',calculation_key='width',target='目标门净宽差',
        quantities=[q('b','70',label='用户要求比较的原文厚度')],
        missing_operands=[dict(label='目标门实测净宽',unit='mm',reason='尚未读取该门的测量值',required_source='同一门的测量观测')])
    r=calculate_facts(s,args)
    assert r['status']=='missing_operands' and 'value' not in r
    assert not any(x['fulfilled'] for x in r['operation_receipts'])
    assert len(r['inputs'])==1 and r['missing_operands'][0]['label']=='目标门实测净宽'


def test_missing_plan_then_real_tool_execution_closes_only_same_formula(workspace,demo_bytes):
    d=workspace.ingest('calc.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='计算厚度差。',mode='agent'))
    s.evidence.update(simple().evidence)
    args=dict(operation='subtract',target='同一墙体厚度差',calculation_key='wall',quantities=[q('a','96')],
        missing_operands=[dict(label='参考厚度',reason='尚未绑定参考来源',unit='mm')])
    r=invoke(s,args); assert r['status']=='missing_operands'
    assert any(p['operation']=='calculation_operands' for p in s.task.pending_work())
    assert 'arithmetic' in s.task.missing_operations(s.task.data['subtasks'][0])
    bad=invoke(s,dict(args,target='其他墙体厚度差'))
    assert '已绑定另一目标' in bad['error']['message']
    r=invoke(s,dict(args,quantities=[q('a','96'),q('b','70')],missing_operands=[]))
    assert r['value']=='26.000'
    assert not s.task.missing_operations(s.task.data['subtasks'][0])
    assert not [p for p in s.task.pending_work() if p['operation'].startswith('calculation_')]
    assert s.task.data['subtasks'][0]['calls']==3 # no automatic budget extension


def test_executing_wrong_arithmetic_does_not_discharge_requested_difference(workspace,demo_bytes):
    d=workspace.ingest('calc.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='计算厚度差。'))
    s.evidence.update(simple().evidence)
    invoke(s,dict(operation='sum',quantities=[q('a','96'),q('b','70')]))
    assert s.task.missing_operations(s.task.data['subtasks'][0])==['arithmetic']
    assert any(p.get('required_operations')==['subtract'] for p in s.task.pending_work())


def replay_real_calculation(workspace,case):
    source=FROZEN/(case+'-task.json')
    if not source.exists():pytest.skip('Frozen source dataset not installed')
    before=hashlib.sha256(source.read_bytes()).hexdigest();old=json.loads(source.read_text());sid=old['snapshot_id']
    with sqlite3.connect(f'file:{FROZEN}/data/index.sqlite3?mode=ro',uri=True) as conn:
        drawing=json.loads(conn.execute("select data from records where kind='drawing' and id=?",(sid,)).fetchone()[0])
    workspace.store.put('drawing',sid,drawing)
    for name in ('regions.json','scene.json','entities.json'):
        dest=workspace.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(FROZEN/'data/drawings'/sid/name,dest)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=sid,question=old['question'],mode='agent'))
    s.task.auto_plan()
    # Rebind exact frozen source spans through the current evidence registration,
    # retaining source IDs, snapshots and hashes; no inferred operands.
    for e in old['evidence'].values():
        region=s.index.by_id.get(e['region_id'])
        assert region and region.text[slice(*e['text_span'])]==e['quote']
    s.evidence.update(deepcopy(old['evidence']))
    s.task.data['evidence'].update(deepcopy(old['evidence']))
    step=next(x for x in s.task.data['subtasks'] if 'arithmetic' in x['operations'])
    results=[]
    if case=='Q12-A':
        area=q('ev_fe2a255bb211964d6e6de000','18700','m2',label='全地下室建筑面积约数')
        cars=q('ev_0089cdb992dd92120e4ec35c','430','count',label='全地下室机动车总数')
        disabled=q('ev_0089cdb992dd92120e4ec35c','8','count',label='全地下室无障碍车辆数')
        for key,op,qs,expected in [('whole_ratio','divide',[area,cars],'43.488372'),('whole_subtract','subtract',[cars,disabled],'422.000000'),('whole_accessible_ratio','divide',[disabled,cars],'0.018605')]:
            r=invoke(s,dict(task_ids=[step['id']],calculation_key=key,target='全地下室原文统计口径',coverage_scope='aggregate',operation=op,quantities=qs,decimal_places=6))
            assert r.get('value')==expected,r
            results.append(r)
        assert results[0]['approximate']
        assert 'arithmetic' in s.task.missing_operations(step)
        assert any(p['operation']=='calculation_target_coverage' for p in s.task.pending_work())
    else:
        assert case=='Q06-A'
        # Frozen reads contain 15-thick gypsum mortar and 5-thick mortar from
        # INNER wall assembly. They cannot be used for the requested outer wall.
        r=invoke(s,dict(task_ids=[step['id']],operation='sum',calculation_key='outerwall_mortar',
            target='两种涂料外墙各自砂浆总厚度',quantities=[],missing_operands=[
            dict(label='涂料外墙做法一的全部砂浆层厚度',unit='mm',reason='已读标题未绑定完整外墙层序；内墙砂浆不能借用',required_source='做法一各砂浆层原文及单位依据'),
            dict(label='涂料外墙做法二的全部砂浆层厚度',unit='mm',reason='尚未取得该做法逐层厚度及排除项',required_source='做法二逐层原文、适用部位及单位依据')]))
        assert r['status']=='missing_operands' and 'value' not in r and not r['inputs']
        results.append(r)
        assert 'arithmetic' in s.task.missing_operations(step)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    return dict(case=case,evidence_class='frozen_source_deterministic_tool_chain',model_calls=0,
        frozen_task_id=old['task_id'],snapshot_id=sid,source_sha256=drawing['sha256'],
        frozen_task_sha256=before,source_spans_revalidated=len(old['evidence']),
        requirement=step['requirement'],operations=step['operations'],results=results,
        pending_calculations=[p for p in s.task.pending_work() if p['operation'].startswith('calculation_')],
        missing_operations=s.task.missing_operations(step),tool_calls=step['calls'],
        limitations=['未完成分区/外墙做法覆盖；不证明模型会主动选取正确操作数。','Q12全地下室约面积不能替代机动车库各分区面积。','Q06仅记录已读范围的操作数缺口，不证明原图无数据。'])


@pytest.mark.parametrize('case',['Q12-A','Q06-A'])
def test_real_frozen_calculation_chain(workspace,case):
    replay_real_calculation(workspace,case)


def test_ratio_output_can_be_chained_for_difference_and_ranking():
    s=simple('用户参考35㎡/车')
    s.evidence={'a':{'quote':'面积约18700㎡'},'n':{'quote':'430辆'}}
    r=calculate_facts(s,CalculateFacts(operation='divide',quantities=[q('a','18700','m2'),q('n','430','count')]))
    s.observations['ratio']=dict(tool='calculate_facts',data=r)
    operand=dict(origin='calculation',observation_id='ratio',value=r['value'],unit='m2/count')
    reference=dict(origin='user_input',question_quote='用户参考35㎡/车',value='35',unit='m2/count')
    diff=calculate_facts(s,CalculateFacts(operation='subtract',quantities=[operand,reference]))
    assert diff['value']=='8.488' and diff['conditional'] and diff['approximate']
    ranked=calculate_facts(s,CalculateFacts(operation='rank',quantities=[reference,operand]))
    assert [x['input_index'] for x in ranked['ordered']]==[1,0]


def test_unresolved_native_dimensions_have_no_fulfilled_measurement_receipts(workspace):
    from test_components import ingest
    from cad_agent.components.dimension_chain import check_chain,DimensionChainQuery
    def build(doc):
        m=doc.modelspace()
        m.add_linear_dim(base=(0,-500),p1=(0,0),p2=(2000,0)).render()
    _,drawing,lid=ingest(workspace,build)
    result=check_chain(workspace,drawing['id'],DimensionChainQuery(layout_id=lid))
    assert result['status']=='unresolved' and result['candidate_dimensions']
    assert not any(r['fulfilled'] for r in result['operation_receipts'])
    assert all(x['object_id'] and x['definition_points'] for x in result['candidate_dimensions'])


def test_unresolved_dimension_candidates_are_scope_filtered(monkeypatch):
    from cad_agent.analysis import scope_workflow
    scope=dict(scope_id='s',snapshot_id='snap',source_sha256='sha',layout_id='l',frame_id='f',bbox=[0,0,10,10])
    monkeypatch.setattr(scope_workflow,'selected_for_call',lambda *a:scope)
    s=SimpleNamespace(trace=[{'task_ids':['q1']}])
    inside=dict(object_id='in',layout_id='l',bbox=[1,1,2,2],definition_points=[[1,1],[2,2]])
    outside=dict(object_id='out',layout_id='l',bbox=[1,1,2,2],definition_points=[[1,1],[20,20]])
    result=dict(status='unresolved',candidate_dimensions=[inside,outside],operation_receipts=[])
    scope_workflow.record_scope_call(s,'check_dimension_chain',{},result)
    assert result['candidate_dimensions']==[inside]
    assert result['excluded_scope_dimensions'][0]['object_id']=='out'
    assert not result['operation_receipts']


def test_source_only_delivery_and_legacy_generic_receipt_do_not_complete_arithmetic(workspace,demo_bytes):
    d=workspace.ingest('calc.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='计算厚度差。'))
    s.task.auto_plan();step=s.task.data['subtasks'][0]
    step['operation_receipts']=[dict(operation='arithmetic',fulfilled=True)]
    assert s.task.missing_operations(step)==['arithmetic']
    result=s.task.finalise(dict(claims=[dict(text='已读两段厚度原文，尚未执行差值计算。',state='derived',evidence_ids=['a','b'],question_part_ids=['q1'])]))
    assert result['subtasks'][0]['status']=='partial'


def test_concrete_missing_operands_can_be_delivered_as_partial(workspace,demo_bytes):
    d=workspace.ingest('calc.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='计算厚度差。'))
    r=invoke(s,dict(operation='subtract',target='目标墙体差值',quantities=[],missing_operands=[
        dict(label='原图墙厚',reason='目标墙体原图厚度尚未读取'),
        dict(label='参照墙厚',reason='未提供参照厚度')]))
    content=dict(claims=[dict(text='尚缺目标墙体原图厚度与参照厚度，不能执行厚度差计算。',state='unknown',
        question_part_ids=['q1'],evidence_basis='tool_observation',observation_ids=[r['observation_id']])],requirement_outcomes=[
        dict(requirement_id='q1',status='unresolved',gap_kind='missing_input',reason='缺少目标墙体厚度及参照厚度。')])
    result=s.finish(json.dumps(content,ensure_ascii=False))
    assert result['task']['subtasks'][0]['status']=='partial'


@pytest.mark.parametrize('mode',['one','both','aggregate','combined'])
def test_each_explicit_target_requires_its_own_executed_formula(workspace,demo_bytes,mode):
    d=workspace.ingest('targets.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='逐区计算东库和西库的面积/车位数。'))
    s.evidence.update({'east':{'quote':'东库面积100㎡，停车数5辆'},'west':{'quote':'西库面积180㎡，停车数6辆'}})
    args=dict(operation='divide',target='东库',quantities=[q('east','100','m2'),q('east','5','count')])
    if mode=='aggregate':args.update(target='全库',coverage_scope='aggregate')
    if mode=='combined':args.update(target='东库和西库')
    r=invoke(s,args);assert r['value']=='20.000'
    if mode=='both':
        r=invoke(s,dict(operation='divide',target='西库',quantities=[q('west','180','m2'),q('west','6','count')]))
        assert r['value']=='30.000'
    step=s.task.data['subtasks'][0]
    pending=[p for p in s.task.pending_work() if p['operation']=='calculation_target_coverage']
    if mode=='both':
        assert not pending and 'arithmetic' not in s.task.missing_operations(step)
    else:
        assert 'arithmetic' in s.task.missing_operations(step)
        assert pending[0]['required_targets']==['东库','西库']
        missing=[r['target'] for r in pending[0]['missing_target_operations']]
        assert missing==(['西库'] if mode=='one' else ['东库','西库'])


def test_unknown_target_universe_is_not_completed_by_one_named_calculation(workspace,demo_bytes):
    d=workspace.ingest('targets.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='计算每个机动车库防火分区的面积/车位数。'))
    s.evidence['e']={'quote':'已读区域面积100㎡，车位5辆'}
    r=invoke(s,dict(operation='divide',target='已读区域',quantities=[q('e','100','m2'),q('e','5','count')]))
    assert r['value']=='20.000'
    pending=[p for p in s.task.pending_work() if p['operation']=='calculation_target_coverage']
    assert pending[0]['target_set_status']=='unconfirmed'
    assert 'arithmetic' in s.task.missing_operations(s.task.data['subtasks'][0])
