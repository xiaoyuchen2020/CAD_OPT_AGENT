"""Stage 2/3 contracts, routing, recovery and answer admission.

Model/execution mocks are explicit. CAD_TEST_TASK_SANDBOX=1 runs the SAME
packages through real Docker, with no model request or external data transfer.
"""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

from cad_agent.analysis import task_programs as tp
from cad_agent.analysis.fact_tools import ReadAssembly
from cad_agent.analysis.tool_registry import tool_definitions, Claim
from cad_agent.extensions.programs import kernels, regression, validation, package, sdk
from cad_agent.extensions.programs.contracts import *
from cad_agent.runtimes.token_budget import charged_tokens
from cad_agent.execution import ExecutionCancelled
from test_table_recovery import recovery_case
from test_task_methods import session_for, baseline

PROGRAM='''import cad_sdk as sdk
from helper import apply

def run(bundle):
    nodes={}
    for n in bundle['contract']['nodes']:
        nodes[n['id']]=apply(bundle,n,nodes)
    return {'input_id':bundle['input_id'],'nodes':nodes,'output':bundle['contract']['output']}
'''
HELPER='''import cad_sdk as sdk

def apply(bundle,n,nodes):
    op=n['op'];inputs=[nodes[i] for i in n['inputs']]
    if op=='source':return sdk.records(bundle,n['source'])
    if op=='select':return sdk.select(inputs[0],n['keywords'])
    if op=='field':return sdk.field(inputs[0],n['prefix'],n['suffix'])
    if op=='number':return sdk.number(inputs[0],n['unit'])
    if op=='sum':return sdk.sum_values(inputs[0])
    return getattr(sdk,op)(*inputs)
'''
PKG={'files':{'main':PROGRAM,'helper':HELPER},'entry':'main'}


def revision_code():
    return kernels.source().replace("round(max(t['origin'][1] for t in source)/max(height*.3,.01))", "max(t['origin'][1] for t in source)")


def strict_spans(bundle):
    rows=kernels.assembly_kernel.run(bundle)
    regions={r['region_id']:r for r in bundle['regions']};texts={t['entity_id']:t for t in bundle['texts']}
    def order(row):
        rid,a,b,col=row
        points=[texts[p['entity_id']]['origin'] for p in regions[rid]['parts'] if p['text_span'][0]<b and p['text_span'][1]>a]
        return col,-max(p[1] for p in points),min(p[0] for p in points),rid,a
    return sorted(rows,key=order)


def mocks(monkeypatch,fail=False):
    calls=[];executions=[]
    monkeypatch.setattr(tp,'runtime',lambda *a:dict(image_id='mock-only'))
    def propose(s,kind,instruction,bundle,feedback,*a,**kw):
        calls.append((kind,copy.deepcopy(feedback)))
        pkg={'files':{'main':revision_code()},'entry':'main'} if kind=='revision' else PKG
        return Proposal(strategy='code',explanation='mock model',package=Package.model_validate(pkg)),dict(usage={'total_tokens':20})
    def execute(data,bundle,code,**kw):
        executions.append(copy.deepcopy(bundle))
        if fail:return {'output':{}}
        if 'cases' in bundle:return {'output':[strict_spans(b) for b in bundle['cases']]}
        if 'column_edges' in bundle:return {'output':strict_spans(bundle)}
        return {'output':validation.expected(bundle,bundle['contract'])}
    monkeypatch.setattr(tp,'propose',propose);monkeypatch.setattr(tp,'execute',execute)
    return calls,executions


def plan(*nodes,output=None):
    return Pipeline(nodes=[Node(**n) for n in nodes],output=output or nodes[-1]['id'])


def count_plan():
    return plan(dict(id='source',op='source'),dict(id='selected',op='select',inputs=['source'],keywords=['900','2100']),
                dict(id='count',op='count',inputs=['selected']))


def develop(s,oid,contract=None):
    return json.loads(s.invoke_tool('develop_program',dict(baseline_observation_ids=[oid],instruction='按明确契约计算局部结果并保留逐项来源。',contract=(contract or count_plan()).model_dump())))


def assembly_baseline(s):
    # Existing synthetic table has real closed bands; no semantic binding is fabricated.
    for r in s.index.regions:
        if '900' in r.text:
            result=json.loads(s.invoke_tool('read_assembly',dict(region_id=r.region_id)))
            if result.get('row_structure'):return result['observation_id'],r.region_id
    raise AssertionError('Synthetic native row not found')


def test_program_pipeline_and_current_answer(workspace,recovery_case,monkeypatch):
    calls,executions=mocks(monkeypatch);s=session_for(workspace,recovery_case)
    result=develop(s,baseline(s,recovery_case))
    assert result.get('admitted'),result
    assert len(calls)==1 and len(executions)==1 and charged_tokens(s)==32000
    assert result['items'][0]['value']=='2'
    template=dict(result['claim_templates'][0],observation_ids=[result['observation_id']])
    answer=s.validate_answer(json.dumps(dict(claims=[template],missing_information=[]),ensure_ascii=False))
    assert answer['claims'][0]['text']==template['text']
    bad=Claim(text='全楼共有2扇门',state='derived',evidence_basis='tool_observation',observation_ids=[result['observation_id']])
    assert tp.claim_issues(s,bad)
    assert develop(s,baseline(s,recovery_case))['result_id']==result['result_id'] and len(calls)==1


def test_revision_installs_native_dispatch_and_versioned_cache(workspace,recovery_case,monkeypatch):
    calls,executions=mocks(monkeypatch);s=session_for(workspace,recovery_case)
    oid,rid=assembly_baseline(s)
    initial=json.loads(s.invoke_tool('inspect_tool_implementation',{}))
    assert initial['source']==kernels.source()
    args=dict(baseline_observation_id=oid,base_sha256=initial['base_sha256'],gap='原工具采用基线量化而非精确位置排序。',instruction='将原内核改为精确纵坐标排序，保留全部来源片段。')
    result=json.loads(s.invoke_tool('revise_tool',args))
    assert result.get('status')=='installed_for_task',result
    after=json.loads(s.invoke_tool('read_assembly',{'region_id':rid}))
    assert after['tool_version']['version_id']==result['version_id']
    assert after['tool_version']['semantic_acceptance'] is False and len(calls)==1
    assert tp._service(s).artifact('program_diff',result['diff_artifact_id'])['diff'].startswith('--- base')
    # Tamper with a saved execution: no revised page may be returned from cache.
    route=next(iter(s.task.data['tool_version_routes'].values()))
    key=next(iter(route['executions'].values()))['execution_artifact_id']
    corrupted=s.w.store.get('adaptation_program_execution',key);corrupted['output']=[]
    s.w.store.put('adaptation_program_execution',key,corrupted)
    fallback=json.loads(s.invoke_tool('read_assembly',{'region_id':rid}))
    assert 'tool_version_fallback' in fallback and 'tool_version' not in fallback
    assert kernels.ACTIVE.get() is None


@pytest.mark.parametrize('attack',['drop','duplicate','column','truncate','foreign','order'])
def test_kernel_independent_validator(attack):
    b=regression.cases(dict(texts=[],regions=[],column_edges=[0,1],bbox=[0,0,1,1]))[-1]
    rows=strict_spans(b)
    if attack=='drop':rows.pop()
    if attack=='duplicate':rows.append(rows[0])
    if attack=='column':rows[0][3]=99
    if attack=='truncate':rows[0][2]=0
    if attack=='foreign':rows[0][0]='forged'
    if attack=='order':rows.reverse()
    with pytest.raises(ValueError):kernels.validate(b,rows,strict_order=True)


def test_revision_canary_proves_change_without_requiring_membership_change():
    b=regression.cases(dict(texts=[],regions=[],column_edges=[0,1],bbox=[0,0,1,1]))[-1]
    base=kernels.assembly_kernel.run(b)
    kernels.validate(b,base)
    with pytest.raises(ValueError,match='ORDER'):kernels.validate(b,base,strict_order=True)
    kernels.validate(b,strict_spans(b),strict_order=True)


@pytest.mark.parametrize('attack',['value','source','drop','extra','node','input'])
def test_pipeline_rejects_forged_outputs(recovery_case,attack):
    bundle=dict(sources=[recovery_case[3]],input_id='fixture')
    contract=count_plan().model_dump();result=validation.expected(bundle,contract)
    if attack=='value':result['nodes']['count'][0]['value']='999'
    if attack=='source':result['nodes']['count'][0]['refs'][0]['entity_id']='fake'
    if attack=='drop':result['nodes']['selected'].pop()
    if attack=='extra':result['semantic_acceptance']=True
    if attack=='node':result['output']='source'
    if attack=='input':result['input_id']='fake'
    with pytest.raises(ValueError,match='CONTRACT'):validation.validate(bundle,contract,result)


def test_field_normalization_units_join_and_arithmetic():
    b=dict(input_id='fixture',sources=[dict(texts=[dict(entity_id='a',text='厚度：12mm；'),dict(entity_id='b',text='厚度：8mm；')])])
    p=plan(dict(id='s',op='source'),dict(id='f',op='field',inputs=['s'],prefix='厚度：',suffix='；'),
           dict(id='n',op='number',inputs=['f'],unit='mm'),dict(id='total',op='sum',inputs=['n']))
    host=validation.expected(b,p.model_dump())
    assert host['nodes']['total'][0]['value']=='20' and host['nodes']['total'][0]['unit']=='mm'
    # SDK and validator are separate implementations, compared on real operations.
    actual=sdk.sum_values(sdk.number(sdk.field(sdk.records(b),'厚度：','；'),'mm'))
    assert host['nodes']['total']==actual
    assert actual[0]['refs'][0]['start']==3 and actual[0]['refs'][0]['end']==7
    p=plan(dict(id='s',op='source'),dict(id='n',op='normalize',inputs=['s']),
           dict(id='d',op='distinct',inputs=['n']),dict(id='j',op='join',inputs=['d','d']),
           dict(id='sorted',op='sort',inputs=['j']),dict(id='eq',op='compare',inputs=['sorted','sorted']))
    assert validation.expected(b,p.model_dump())['nodes']['eq'][0]['value']=='相同'


@pytest.mark.parametrize('value',['12','NaNmm','Infinitymm','1e20mm','12m','1,000mm'])
def test_missing_or_ambiguous_units_and_numbers_refused(value):
    b=dict(input_id='fixture',sources=[dict(texts=[dict(entity_id='a',text=value)])])
    p=plan(dict(id='s',op='source'),dict(id='n',op='number',inputs=['s'],unit='mm'))
    with pytest.raises(ValueError):validation.expected(b,p.model_dump())


@pytest.mark.parametrize('files,entry',[
    ({'../evil':'def run(b):return b'},'../evil'),
    ({'main':'import os\ndef run(b):return b'},'main'),
    ({'main':'import helper\ndef run(b):return b','helper':'import main'},'main'),
    ({'main':'def nope(b):return b'},'main'),
    ({'main':'#'+'x'*15000},'main'),
])
def test_package_rejects_unavailable_dependencies_and_invalid_modules(files,entry):
    with pytest.raises(ValueError):package.payload(dict(files=files,entry=entry))


def test_program_disabled_and_wrong_baseline(workspace,recovery_case,monkeypatch):
    calls,_=mocks(monkeypatch);s=session_for(workspace,recovery_case,mode='off')
    assert not TOOLS & {v['function']['name'] for v in tool_definitions(session=s)}
    assert 'DISABLED' in json.dumps(develop(s,'fake')) and not calls
    s=session_for(workspace,recovery_case)
    assert 'BASELINE_REQUIRED' in json.dumps(develop(s,'fake')) and not calls


def test_failed_program_never_grants_evidence(workspace,recovery_case,monkeypatch):
    calls,_=mocks(monkeypatch,fail=True);s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    old=set(s.evidence);result=develop(s,oid)
    assert not result.get('admitted') and set(s.evidence)==old and len(calls)==3
    assert charged_tokens(s)==96000


def test_cancel_reservation_and_no_regeneration(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    def cancel(*a,**k):raise ExecutionCancelled()
    monkeypatch.setattr(tp,'propose',cancel)
    with pytest.raises(ExecutionCancelled):develop(s,oid)
    assert charged_tokens(s)==32000
    assert not develop(s,oid)['admitted']


def test_task_ownership_resume_integrity_and_budget(workspace,recovery_case,monkeypatch):
    calls,_=mocks(monkeypatch);s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    other=session_for(workspace,recovery_case)
    with pytest.raises(ValueError,match='NOT_OWNED'):tp.read(other,ReadResult(result_id=r['result_id']))
    other.task.data=copy.deepcopy(s.task.data)
    assert tp.read(other,ReadResult(result_id=r['result_id']))['admitted'] and len(calls)==1
    assert charged_tokens(other)==32000
    path=workspace.store.artifact(s.request.snapshot_id,'source.dxf');path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='SOURCE_INTEGRITY'):tp.read(s,ReadResult(result_id=r['result_id']))


def test_native_source_spans_do_not_gain_semantic_claims(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case)
    p=plan(dict(id='s',op='source'))
    r=develop(s,baseline(s,recovery_case),p);e=next(e for e in r['evidence'] if e['evidence_id'] in s.task.data['program_unbound_evidence'])
    assert tp.claim_issues(s,Claim(text='外墙厚度为900毫米',state='observed',evidence_ids=[e['evidence_id']]))
    assert not tp.claim_issues(s,Claim(text=e['quote'],state='observed',evidence_ids=[e['evidence_id']]))


def test_registration_requires_cross_drawing_executions(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    with pytest.raises(ValueError,match='CROSS_DRAWING'):tp.register(s,RegisterProgram(program_id=r['program_id'],name='计数'))


@pytest.mark.skipif(os.environ.get('CAD_TEST_TASK_SANDBOX')!='1',reason='explicit local Docker sandbox test')
def test_real_docker_multimodule_program_and_tool_revision(workspace,recovery_case,monkeypatch):
    import shutil
    root=Path(__file__).resolve().parents[1]
    shutil.copyfile(root/'.data/adaptation-runtime.json',workspace.settings.data_dir/'adaptation-runtime.json')
    def propose(s,kind,*a,**kw):
        p={'files':{'main':revision_code()},'entry':'main'} if kind=='revision' else PKG
        return Proposal(strategy='code',explanation='fixed test code, no model',package=Package.model_validate(p)),dict(usage={})
    monkeypatch.setattr(tp,'propose',propose)
    s=session_for(workspace,recovery_case)
    r=develop(s,baseline(s,recovery_case));assert r.get('admitted'),r
    entry=s.task.data['task_programs'][r['program_id']]
    execution=tp._service(s).artifact('program_execution',entry['attempts'][0]['execution_artifact_id'])
    assert execution['isolation']['network']=='none' and not execution['isolation']['mounts']
    oid,rid=assembly_baseline(s)
    result=tp.revise(s,ReviseTool(baseline_observation_id=oid,base_sha256=kernels.base_hash(),
        gap='量化顺序与精确位置可能不一致。',instruction='改用精确纵坐标排列原始来源片段。'))
    assert result['status']=='installed_for_task',result
    native=json.loads(s.invoke_tool('read_assembly',dict(region_id=rid)))
    assert native['tool_version']['version_id']==result['version_id']


def test_new_input_execution_registration_and_cross_task_reuse(workspace,recovery_case,monkeypatch,tmp_path):
    from conftest import dxf_bytes
    from cad_agent.contracts import RelatedDrawing
    from cad_agent.analysis.session import CadAgentSession
    calls,executions=mocks(monkeypatch)
    def drawing(doc):
        m=doc.modelspace()
        for x in (0,40,80,120):m.add_line((x,0),(x,45))
        for y in (0,15,30,45):m.add_line((0,y),(120,y))
        for y,values in ((38,('编号','宽度(mm)','高度(mm)')),(23,('M3','800','2000')),(8,('M4','','2200'))):
            for x,value in zip((5,45,85),values):
                if value:m.add_text(value,dxfattribs={'insert':(x,y),'height':2})
    second=workspace.ingest('second.dxf',dxf_bytes(tmp_path,drawing))
    base=session_for(workspace,recovery_case)
    s=CadAgentSession(workspace,base.request.model_copy(update={'related_drawings':[RelatedDrawing(snapshot_id=second['id'])]}))
    oid,_=assembly_baseline(s)
    r=develop(s,oid,plan(dict(id='s',op='source'),dict(id='n',op='count',inputs=['s'])))
    assert r.get('admitted'),r
    index=tp._index(s,second['id']);target=next(r for r in index.regions if '800' in r.text)
    b=json.loads(s.invoke_tool('read_assembly',dict(region_id=target.region_id,snapshot_id=second['id'])))
    second_result=tp.execute_program(s,ExecuteProgram(program_id=r['program_id'],baseline_observation_ids=[b['observation_id']]))
    assert second_result['admitted'] and len(calls)==1 and len(executions)==2
    registered=tp.register(s,RegisterProgram(program_id=r['program_id'],name='局部原文计数'))
    assert registered['status']=='experimental'
    other=session_for(workspace,recovery_case)
    listed=tp.list_programs(other,ListPrograms())
    assert listed['items'][0]['program_id']==registered['program_id']
    replay=tp.execute_program(other,ExecuteProgram(program_id=registered['program_id'],baseline_observation_ids=[baseline(other,recovery_case)]))
    assert replay['admitted'] and len(calls)==1 and len(executions)==3 and charged_tokens(other)==0


def test_scope_and_evidence_revalidation_after_validator_change(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    def changed(*a,**k):raise ValueError('VALIDATOR_CHANGED')
    monkeypatch.setattr(validation,'validate',changed)
    with pytest.raises(ValueError,match='VALIDATOR_CHANGED'):
        tp.read(s,ReadResult(result_id=r['result_id']))
    with pytest.raises(ValueError,match='VALIDATOR_CHANGED'):
        tp.claim_issues(s,Claim(text=r['claim_templates'][0]['text'],state='derived',evidence_basis='tool_observation',observation_ids=[r['observation_id']]))


def test_dynamic_enabled_keeps_agent_route(workspace,recovery_case):
    from cad_agent.analysis.source_record_query import route
    s=session_for(workspace,recovery_case)
    s.request=s.request.model_copy(update={'question':'帮我找下外墙做法，允许使用动态代码。'})
    assert route(s)['reason']=='dynamic_authorized_agent_route'


def test_dag_cycle_and_forward_reference_rejected():
    with pytest.raises(ValueError):plan(dict(id='x',op='count',inputs=['y']),dict(id='y',op='source'))
    with pytest.raises(ValueError):plan(dict(id='x',op='source'),dict(id='x',op='count',inputs=['x']))


def test_program_time_refusal_precedes_generation(workspace,recovery_case,monkeypatch):
    calls,_=mocks(monkeypatch);s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    monkeypatch.setattr(s,'_remaining',lambda:31)
    r=develop(s,oid)
    assert not r['admitted'] and not calls and charged_tokens(s)==0


def test_duplicate_sources_do_not_satisfy_cross_drawing_registration(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    r=develop(s,oid)
    assert tp.execute_program(s,ExecuteProgram(program_id=r['program_id'],baseline_observation_ids=[oid,oid]))['admitted']
    with pytest.raises(ValueError,match='CROSS_DRAWING'):
        tp.register(s,RegisterProgram(program_id=r['program_id'],name='重复输入'))


def test_cached_revision_is_invalidated_with_host_validator(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);oid,rid=assembly_baseline(s)
    r=tp.revise(s,ReviseTool(baseline_observation_id=oid,base_sha256=kernels.base_hash(),gap='量化顺序与精确位置可能不一致。',instruction='改用精确纵坐标排列原始来源片段。'))
    assert r['status']=='installed_for_task'
    json.loads(s.invoke_tool('read_assembly',dict(region_id=rid)))
    def invalid(*a,**kw):raise ValueError('NEW_VALIDATOR_REJECTED')
    monkeypatch.setattr(kernels,'validate',invalid)
    result=json.loads(s.invoke_tool('read_assembly',dict(region_id=rid)))
    assert 'tool_version_fallback' in result and not s.task.data['tool_version_routes']


def test_program_result_receipts_are_bound_and_metadata_not_numeric(workspace,recovery_case,monkeypatch):
    from cad_agent.analysis.operations import receipts
    from cad_agent.analysis.boundary_context import numeric_evidence
    mocks(monkeypatch);s=session_for(workspace,recovery_case)
    r=develop(s,baseline(s,recovery_case))
    assert [v['operation'] for v in receipts('develop_program',r)]==['arithmetic']
    assert numeric_evidence(r)==[{'value':'2','unit':'条'}]
