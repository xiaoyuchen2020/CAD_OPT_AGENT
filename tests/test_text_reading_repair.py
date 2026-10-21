"""Regression of lost text, default-unit provenance and partial delivery."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from test_remediation_delivery import read_fact

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'artifacts/text-sol-low-latest-20261020'
SETUP = ROOT / 'artifacts/text-sol-low-latest-20261020-setup'


def source(quote, frame='detail', snapshot='s'):
    return dict(quote=quote, snapshot_id=snapshot, layout_id='layout', frame_id=frame,
                region_id=quote, text_span=[0, len(quote)])


def calculation_session():
    return SimpleNamespace(evidence={
        'a':source('5厚抗裂砂浆'), 'b':source('10厚水泥砂浆'),
        'units':source('本工程施工图所注尺寸，除总图及标高以米为单位外，其余均以毫米为单位。', 'notes')},
        observations={}, request=SimpleNamespace(question='用户给定参考20mm，求砂浆厚度差。'))


def quantity(eid, value, **extra):
    return dict(origin='source', evidence_id=eid, value=value, unit='mm', unit_evidence_id='units', **extra)


def test_default_unit_chain_preserves_numeric_and_unit_sources():
    s=calculation_session()
    first=calculate_facts(s, CalculateFacts(operation='sum', quantities=[quantity('a','5'),quantity('b','10')]))
    assert first['value']=='15.000' and first['unit']=='mm'
    assert first['inputs'][0]['quote']=='5厚抗裂砂浆'
    assert first['lineage'][0]['unit_basis']['evidence_id']=='units'
    assert first['inputs'][0]['number_evidence']
    s.observations['sum']=dict(tool='calculate_facts',data=first)
    second=calculate_facts(s,CalculateFacts(operation='subtract',quantities=[
        dict(origin='calculation',observation_id='sum',value='15',unit='mm'),
        dict(origin='user_input',question_quote='用户给定参考20mm',value='20',unit='mm')]))
    assert second['value']=='-5.000' and second['conditional']
    assert second['lineage'][0]['unit_basis']['quote']==s.evidence['units']['quote']


@pytest.mark.parametrize('mutate', [
    lambda s:s.evidence['units'].update(snapshot_id='other'),
    lambda s:s.evidence['units'].update(layout_id='other'),
    lambda s:s.evidence['units'].update(quote='本说明中表示厚度等数据，未标明单位的均以毫米为单位。'),
    lambda s:s.evidence['units'].update(quote='总平面图尺寸均以毫米为单位。'),
    lambda s:s.evidence['a'].update(quote='5cm厚抗裂砂浆'),
    lambda s:s.evidence['a'].update(quote='面积5平方米'),
    lambda s:s.evidence['a'].update(quote='标高5'),
    lambda s:s.evidence['a'].update(quote='材料C5'),
    lambda s:s.evidence.update(conflict=source('本工程施工图未标明单位的尺寸均以厘米为单位。','notes')),
])
def test_default_unit_rejects_scope_conflict_and_non_linear_values(mutate):
    s=calculation_session();mutate(s)
    with pytest.raises(ValueError):calculate_facts(s,CalculateFacts(operation='sum',quantities=[quantity('a','5')]))


def test_unknown_user_condition_survives_without_becoming_drawing_fact(workspace,demo_bytes):
    s,_=read_fact(workspace,demo_bytes,'用户给定比较标签10%，未规定分母。')
    text='用户给定比较标签10%，分母尚未规定，无法计算相对差。'
    packet=dict(claims=[dict(text=text,state='unknown',question_part_ids=['q1'],
        user_inputs=[dict(quote='用户给定比较标签10%',value='10%')])])
    result=s.recover_valid_claims(json.dumps(packet))
    assert text in result['answer'] and result['recovery']['rejected_claims']==0
    assert result['claims'][0]['state']=='unknown' and result['task']['status']=='partial'
    packet['claims'][0]['state']='observed'
    with pytest.raises(ValueError):s.validate_answer(json.dumps(packet))


def test_recovery_preserves_independent_fulfilled_requirement(workspace,demo_bytes):
    s,fact=read_fact(workspace,demo_bytes,'请①读取材料；②读取厚度。')
    bad=fact|{'text':'厚度987654mm。','question_part_ids':['q2']}
    result=s.recover_valid_claims(json.dumps(dict(claims=[fact,bad],requirement_outcomes=[
        dict(requirement_id='q1',status='fulfilled',gap_kind='none',reason='材料原文已交付。'),
        dict(requirement_id='q2',status='fulfilled',gap_kind='none',reason='厚度已交付。') ])))
    outcomes={o['requirement_id']:o for o in result['requirement_outcomes']}
    assert outcomes['q1']['status']=='fulfilled' and outcomes['q1']['gap_kind']=='none'
    assert outcomes['q2']['status']=='unresolved' and outcomes['q2']['gap_kind']=='validation_failed'
    assert result['task']['status']=='partial'
    assert not any('读取材料' in x for x in result['missing_information'])


def frozen_session(workspace, sid, question):
    root=FROZEN/'inputs'/sid
    if not root.exists():pytest.skip('optional immutable text evaluation input not installed')
    drawing=json.loads((root/'drawing.json').read_text())
    workspace.store.put('drawing',sid,drawing)
    for name in ('regions.json','scene.json','entities.json'):
        dest=workspace.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/name,dest)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=sid,question=question))
    s.task.auto_plan()
    return s


def test_real_safety_glass_list_is_read_with_its_intro(workspace):
    s=frozen_session(workspace,'snap_387d51baa9bf10359b5cf120','读取安全玻璃适用部位。')
    r=s.execute('read_source_context',dict(region_id='region_2f80b233cb22ac4815934d1a',neighbors=0),bounded=True)
    quotes=[i['evidence']['quote'] for i in r['items']]
    assert any('7层以上的外开窗' in q for q in quotes)
    assert any('1.5平方米' in q for q in quotes)
    assert any('内、外玻璃门' in q for q in quotes)
    assert any(i['relationship']=='numbered_list_continuation' for i in r['items'])
    assert all(i['relation_state']=='candidate' for i in r['items'])


@pytest.mark.parametrize('question, sid, expected', [
    ('摘录图内明确写出的防水等级、基层条件及材料兼容性说明。','snap_ef95ba940715f9f93396f465','region_eb0bf355d4ddc05db47b8c6a'),
    ('摘录卫生间防水层数适用条件。','snap_ef95ba940715f9f93396f465','region_8a3191a12750b2e9d7a4afbf'),
    ('梳理屋面保温材料、厚度和燃烧性能等级。','snap_387d51baa9bf10359b5cf120','region_17b7b0e25bd337e6aa3b4212'),
])
def test_real_cross_clause_read_reaches_missed_conditions(workspace,question,sid,expected):
    s=frozen_session(workspace,sid,question)
    first=s.execute('read_requirement_sources',dict(requirement_id='q1',limit=6))
    second=s.execute('read_requirement_sources',dict(requirement_id='q1',offset=6,limit=6))
    ids={i['region_id'] for r in (first,second) for i in r['items']}
    assert expected in ids
    assert all(i['attribution_state']=='unverified' for i in first['items'])
    for i in first['items']:
        e=i['evidence'];assert s.index.by_id[e['region_id']].text[slice(*e['text_span'])]==e['quote']


def test_real_default_unit_is_discovered_for_assembly_thickness(workspace):
    s=frozen_session(workspace,'snap_387d51baa9bf10359b5cf120','读取砂浆厚度。')
    r=s.execute('read_source_context',dict(region_id='region_636750c020cf30d99f468544',neighbors=0),bounded=True)
    e=next(i['evidence'] for i in r['items'] if i['relationship']=='target')
    clause=next(x['evidence'] for x in r['default_unit_clauses'] if '本工程施工图' in x['evidence']['quote'])
    result=calculate_facts(s,CalculateFacts(operation='sum',quantities=[
        dict(origin='source',evidence_id=e['evidence_id'],value='8',unit='mm',unit_evidence_id=clause['evidence_id'])]))
    assert result['value']=='8.000' and result['inputs'][0]['unit_binding']


def test_original_q19_unknown_condition_is_retained(workspace):
    review=SETUP/'review-inputs/Q19-B-sol-low.json'
    task_path=FROZEN/'attempts/Q19-B-sol-low/0002/task.json'
    if not review.exists() or not task_path.exists():
        pytest.skip('optional immutable failed delivery not installed')
    old=json.loads(review.read_text())
    task=json.loads(task_path.read_text())
    s=frozen_session(workspace,task['snapshot_id'],old['question'])
    s.task.data=deepcopy(task)
    s.evidence.update(task['evidence'])
    s.observations.update(task['observations'])
    s.trace.extend(deepcopy([t for t in old['trace'] if t['tool']!='finish_analysis']))
    draft=next(t['arguments'] for t in old['trace'] if t['tool']=='finish_analysis')
    condition=next(c for c in draft['claims'] if c.get('user_inputs'))
    result=s.recover_valid_claims(json.dumps(draft,ensure_ascii=False))
    assert result is not None
    assert result['recovery']['rejected_claims']==0
    assert condition['text'] in result['answer']
    retained=next(c for c in result['claims'] if c['text']==condition['text'])
    assert retained['state']=='unknown' and retained['user_inputs']==condition['user_inputs']
    assert result['task']['status']=='partial'


def test_malformed_claim_does_not_allow_false_fulfilled_recovery(workspace,demo_bytes):
    s,fact=read_fact(workspace,demo_bytes)
    result=s.recover_valid_claims(json.dumps(dict(claims=[fact,fact|{'state':'invalid'}],
        requirement_outcomes=[dict(requirement_id='q1',status='fulfilled',gap_kind='none',reason='完成')])))
    assert result['requirement_outcomes'][0]['status']!='fulfilled'


def test_local_unit_clause_can_contain_separate_elevation_convention():
    from cad_agent.analysis.default_units import declaration
    assert declaration('本图标高以米为单位。本说明中表示距离、长度、宽度、厚度或高度等数据，未标明单位的，均以毫米（mm）为单位。')=='mm'
    assert declaration('本图标高以毫米为单位。') is None


def test_numbered_continuation_stops_at_gap_and_does_not_infer_ownership():
    from conftest import region
    from cad_agent.analysis.evidence_ledger import numbered_continuations
    rows=[region(text,str(i)).model_copy(update={'bbox':[0,100-i*10,100,105-i*10]})
          for i,text in enumerate(['以下部位采用安全玻璃：','1）外开窗','2）玻璃门','4）不连续清单','其他标题'])]
    assert [r.text for r in numbered_continuations(rows,rows[0])]==['1）外开窗','2）玻璃门']
    assert not numbered_continuations(rows,rows[1])


def test_requirement_read_persists_pagination_without_resetting_budgets(workspace,demo_bytes):
    from cad_agent.analysis.recall import pending_calls
    s,_=read_fact(workspace,demo_bytes,'读取图纸材料说明。')
    before=s.task.data['subtasks'][0]['calls']
    first=json.loads(s.invoke_tool('read_requirement_sources',dict(requirement_id='q1',limit=1,task_ids=['q1'])))
    assert not first.get('error') and first['has_more']
    assert s.task.data['subtasks'][0]['calls']>before
    calls=[c for c in pending_calls(s.task.data) if c['tool']=='read_requirement_sources']
    assert len(calls)==1 and calls[0]['arguments']['offset']==1 and not calls[0]['blocking']
    s.invoke_tool(calls[0]['tool'],calls[0]['arguments']|{'task_ids':['q1']})
    assert not any(c['arguments'].get('offset')==1 for c in pending_calls(s.task.data)
                   if c['tool']=='read_requirement_sources')
    s.task.data['subtasks'][0]['calls']=12
    assert not any(c['tool']=='read_requirement_sources' for c in pending_calls(s.task.data))


def test_sentence_recall_is_scoped_and_returns_unchanged_source():
    from conftest import region
    from cad_agent.analysis.requirement_sources import read, RequirementSources
    long=region('引用标准。'*100+'设备安装时基层含水率不得超过8%。','long')
    other=region('设备安装时基层含水率不得超过7%。','other',frame_id='other')
    index=SimpleNamespace(regions=[long,other],by_id={r.region_id:r for r in (long,other)},snapshot='s')
    index.scope=lambda layout,frame,section:[r for r in index.regions if r.frame_id==frame]
    index.evidence=lambda r,a,b:dict(source(r.text[a:b]),region_id=r.region_id,
                                   evidence_id=r.region_id,text_span=[a,b])
    s=SimpleNamespace(index=index,trace=[],task=SimpleNamespace(data={'subtasks':[
        dict(id='q1',requirement='读取设备安装的基层条件',requirement_types=['SOURCE_FACT'],coverage=[])]}))
    result=read(s,RequirementSources(requirement_id='q1',frame_id='frame1'))
    assert [i['region_id'] for i in result['items']]==['long']
    assert result['items'][0]['evidence']['quote']==long.text
    assert result['items'][0]['attribution_state']=='unverified'
