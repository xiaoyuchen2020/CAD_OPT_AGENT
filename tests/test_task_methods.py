"""Task-local admission and failure isolation; model/sandbox mocks are explicit."""
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.tool_registry import tool_definitions, Claim
from cad_agent.analysis import task_methods as tm
from cad_agent.contracts import ChatRequest
from cad_agent.extensions.adaptation.contracts import Proposal
from cad_agent.runtimes.token_budget import charged_tokens
from cad_agent.execution import ExecutionCancelled
from test_table_recovery import recovery_case
from test_adaptation_multimethod import candidate


def session_for(workspace, case, mode='task_verified', policy='legacy'):
    return CadAgentSession(workspace, ChatRequest(snapshot_id=case[0]['id'], question='读取局部原文候选并保留来源。',
        mode='agent', runtime='responses_subscription', model_profile='siwc_sol', dynamic_methods=mode,
        agent_policy=policy))


def baseline(s, case):
    q=dict(layout_id=case[3]['layout_id'], bbox=case[3]['bbox'],limit=1)
    result=json.loads(s.invoke_tool('query_entities', q))
    assert not result.get('error'), result
    return result['observation_id']


def mocks(monkeypatch, invalid_first=False, fail=False):
    calls=[]; requests=[]
    monkeypatch.setattr(tm,'runtime',lambda *a: {'image_id':'test-only'})
    def model(settings,method,instruction,bundle,feedback,**kwargs):
        calls.append(copy.deepcopy(feedback)); requests.append((method,bundle))
        return Proposal(strategy='code',explanation='mock model',code='def adapt(bundle):\n return {}'), dict(usage={'total_tokens':20},reserved_tokens=100)
    def execute(*a,**k):
        method,bundle=requests.pop(0)
        return dict(output={} if fail or invalid_first and len(calls)==1 else candidate(method,bundle))
    monkeypatch.setattr(tm,'propose',model);monkeypatch.setattr(tm,'execute',execute)
    return calls


def develop(s, oid, method='text_spans'):
    return json.loads(s.invoke_tool('develop_method',dict(baseline_observation_id=oid,method=method,
        gap='已有读取不能按目标筛选局部来源。',instruction='选取局部来源并严格保留来源ID，不确认工程身份。')))


def test_disabled_schema_and_direct_execution(workspace,recovery_case):
    s=session_for(workspace,recovery_case,'off')
    assert not tm.TOOLS & {t['function']['name'] for t in tool_definitions(session=s)}
    assert 'TASK_METHODS_DISABLED' in json.dumps(develop(s,'fake'))


@pytest.mark.parametrize('method',['text_spans','segment_measurements','segment_distances','closed_contour'])
def test_real_validator_admission_and_repair(workspace,recovery_case,monkeypatch,method):
    calls=mocks(monkeypatch,invalid_first=True)
    s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    # Geometry source scope uses a native spatial read, not a claimed failure.
    if method!='text_spans':
        p=json.loads(s.invoke_tool('query_spatial',dict(layout_id=recovery_case[3]['layout_id'],bbox=recovery_case[3]['bbox'])))
        assert not p.get('error'),p
        oid=p['observation_id']
    result=develop(s,oid,method)
    assert result.get('admitted'),result
    assert len(calls)==2 and calls[1][0]['errors']
    assert result['observation_id'] in s.observations
    assert result['semantic_acceptance'] is False
    assert charged_tokens(s)==64000
    assert sum(u['total_tokens'] for u in s.usage)==40
    if method=='text_spans':
        assert all(e['evidence_id'] in s.evidence for e in result['evidence'])
        e=result['evidence'][0]
        checked=s.validate_answer(json.dumps(dict(claims=[dict(text=e['quote'],state='observed',evidence_ids=[e['evidence_id']])],missing_information=[]),ensure_ascii=False))
        assert checked['claims'][0]['evidence_ids']==[e['evidence_id']]
    before=len(calls)
    assert develop(s,oid,method)['method_id']==result['method_id']
    assert len(calls)==before


def test_failed_code_does_not_register_evidence(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch,fail=True);s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    before=dict(s.evidence);r=develop(s,oid)
    assert r['admitted'] is False and s.evidence==before and len(calls)==3
    assert charged_tokens(s)==96000
    assert tm.claim_issues(s,Claim(text='值为1',state='derived',evidence_basis='tool_observation',observation_ids=[r['observation_id']]))


def test_task_ownership_and_source_change(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    other=session_for(workspace,recovery_case)
    with pytest.raises(ValueError,match='NOT_OWNED'):
        tm.read(other,tm.RunTaskMethod(method_id=r['method_id']))
    path=workspace.store.artifact(s.request.snapshot_id,'source.dxf');path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='SOURCE_INTEGRITY'):
        tm.read(s,tm.RunTaskMethod(method_id=r['method_id']))


def test_wrong_baseline_rejected_before_generation(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch);s=session_for(workspace,recovery_case)
    assert 'BASELINE_REQUIRED' in json.dumps(develop(s,'forged')) and not calls


def test_resume_reuses_validated_code_without_generation(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch);s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    saved=copy.deepcopy(s.task.data)
    other=session_for(workspace,recovery_case);other.task.data=saved
    again=tm.read(other,tm.RunTaskMethod(method_id=r['method_id']))
    assert again['code_sha256']==r['code_sha256'] and len(calls)==1
    assert charged_tokens(other)==32000


def test_unknown_model_failure_reservation_is_retained(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch)
    from cad_agent.adapters.model import ModelError
    def fail(*a,**k):raise ModelError('TRANSPORT_FAILURE','test')
    monkeypatch.setattr(tm,'propose',fail)
    s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    r=develop(s,oid);assert not r['admitted'] and charged_tokens(s)==32000
    assert not s.usage
    assert develop(s,oid)['method_id']==r['method_id'] and charged_tokens(s)==32000


def test_borrowed_subscription_lease_restores_on_error(workspace,recovery_case):
    s=session_for(workspace,recovery_case)
    with pytest.raises(ValueError):
        with tm.borrowed_subscription(s):
            assert s._task_method_subscription_lease
            raise ValueError('fixture')
    assert not s._task_method_subscription_lease


def test_substring_no_unread_tail_credit(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch)
    def execute(data,bundle,code,**kw):
        t=next(t for t in bundle['texts'] if len(t['text'])>3)
        return dict(output=dict(input_id=bundle['input_id'],items=[dict(entity_id=t['entity_id'],start=0,end=2)]))
    monkeypatch.setattr(tm,'execute',execute)
    s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    assert r.get('admitted'),r
    assert all(len(e['quote'])==2 for e in r['evidence'])


def test_cancel_and_interrupted_attempt_no_regeneration(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch)
    def cancel(*a,**k):raise ExecutionCancelled()
    monkeypatch.setattr(tm,'propose',cancel)
    s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    with pytest.raises(ExecutionCancelled):develop(s,oid)
    assert next(iter(s.task.data['task_methods'].values()))['status']=='cancelled'
    assert charged_tokens(s)==32000
    assert not develop(s,oid)['admitted']


def test_table_grid_admission_is_structural(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch)
    from cad_agent.extensions.table_recovery.worker import recover
    monkeypatch.setattr(tm,'execute',lambda data,b,code,**kw:dict(output=recover(b)))
    s=session_for(workspace,recovery_case)
    b=json.loads(s.invoke_tool('read_table',{'table_id':recovery_case[2]['table_id']}))
    before=set(s.evidence)
    r=develop(s,b['observation_id'],'table_grid')
    assert r.get('admitted') and r['has_more'],r
    assert r['evidence']==[] and set(s.evidence)==before
    assert not any('evidence' in row for row in r['items'])
    last=json.loads(s.invoke_tool('run_task_method',r['next_call']['arguments']))
    assert last.get('admitted') and not last['has_more']


def test_scope_cannot_be_injected(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch);s=session_for(workspace,recovery_case)
    oid=baseline(s,recovery_case)
    r=json.loads(s.invoke_tool('develop_method',dict(baseline_observation_id=oid,method='text_spans',
        gap='缺少局部数据的选择结果',instruction='返回原文来源片段以便检查',bbox=[-1e9,-1e9,1e9,1e9])))
    assert r.get('error') and not calls


def test_budget_and_time_refuse_without_model(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch);s=session_for(workspace,recovery_case);oid=baseline(s,recovery_case)
    monkeypatch.setattr(s,'_remaining',lambda:31)
    r=develop(s,oid)
    assert not r['admitted'] and not calls and charged_tokens(s)==0


def test_forged_code_artifact_is_rejected(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    entry=s.task.data['task_methods'][r['method_id']];key=entry['attempts'][-1]['code_artifact_id']
    artifact=s.w.store.get('adaptation_code',key);artifact['code']='forged'
    s.w.store.put('adaptation_code',key,artifact)
    with pytest.raises(ValueError,match='INTEGRITY'):
        tm.read(s,tm.RunTaskMethod(method_id=r['method_id']))


def test_numeric_metadata_not_measurement_credit():
    from cad_agent.analysis.boundary_context import numeric_evidence
    value=dict(kind='task_method_result',total=999,code_sha256='12345',items=[dict(length=12,bbox=[0,999,0,0])])
    assert numeric_evidence(value)==[{'length':12}]


def test_new_span_cannot_be_reassigned_to_assembly(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch)
    def execute(data,bundle,code,**kw):
        t=next(t for t in bundle['texts'] if t['text']=='900')
        return dict(output=dict(input_id=bundle['input_id'],items=[dict(entity_id=t['entity_id'],start=0,end=3)]))
    monkeypatch.setattr(tm,'execute',execute)
    s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    e=r['evidence'][0]
    assert tm.claim_issues(s,Claim(text='外墙厚度为900毫米',state='observed',evidence_ids=[e['evidence_id']]))
    assert not tm.claim_issues(s,Claim(text='900',state='observed',evidence_ids=[e['evidence_id']]))


@pytest.mark.skipif(__import__('os').environ.get('CAD_TEST_TASK_SANDBOX')!='1',reason='explicit local Docker sandbox test')
def test_actual_isolated_execution_into_current_answer(workspace,recovery_case,monkeypatch):
    import shutil
    root=Path(__file__).resolve().parents[1]
    shutil.copyfile(root/'.data/adaptation-runtime.json',workspace.settings.data_dir/'adaptation-runtime.json')
    calls=[]
    code='def adapt(bundle):\n return {"input_id":bundle["input_id"],"items":[{"entity_id":t["entity_id"],"start":0,"end":len(t["text"])} for t in sorted(bundle["texts"],key=lambda t:(t["origin"][0],-t["origin"][1])) if t["text"]]} '
    def model(*a,**k):
        calls.append(1)
        return Proposal(strategy='code',explanation='deterministic sandbox fixture, no model request',code=code),dict(usage={},reserved_tokens=0)
    monkeypatch.setattr(tm,'propose',model)
    s=session_for(workspace,recovery_case);r=develop(s,baseline(s,recovery_case))
    assert r.get('admitted'),r
    entry=s.task.data['task_methods'][r['method_id']]
    svc=tm.AdaptationService(s.w,s.request.snapshot_id)
    execution=svc.artifact('execution',entry['attempts'][0]['execution_artifact_id'])
    assert execution['isolation']['network']=='none' and not execution['isolation']['mounts']
    e=r['evidence'][0]
    answer=s.validate_answer(json.dumps(dict(claims=[dict(text=e['quote'],state='observed',evidence_ids=[e['evidence_id']])],missing_information=[]),ensure_ascii=False))
    assert answer['citations'][0]['quote']==e['quote'] and len(calls)==1


def test_focused_tools_survive_native_read_completion(workspace,recovery_case):
    s=session_for(workspace,recovery_case)
    s.request=s.request.model_copy(update={'agent_policy':'efficient'})
    baseline(s,recovery_case)
    assert tm.TOOLS <= s.focus.names()


def test_efficient_progress_accepts_dynamic_binding_contract(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch)
    s=session_for(workspace,recovery_case,policy='efficient')
    r=develop(s,baseline(s,recovery_case))
    assert r.get('admitted'),r
    assert r['source_binding']['state']=='candidate' and len(calls)==1
    assert s.task.data['observations'][r['observation_id']]['tool']=='develop_method'


@pytest.mark.parametrize('attack',['drop','order','partial','none'])
def test_independent_literal_selection_order_contract(recovery_case,attack):
    b=recovery_case[3]
    entry=dict(method='text_spans',request={'text_selection':{'keywords':['900','2100'],'order':'columns_top_down'}},column_edges=[0,40,80,120])
    rows=sorted((t for t in b['texts'] if t['text'] in ('900','2100')),key=lambda t:t['origin'][0])
    items=[dict(entity_id=t['entity_id'],start=0,end=len(t['text'])) for t in rows]
    if attack=='drop':items.pop()
    elif attack=='order':items.reverse()
    elif attack=='partial':items[0]['end']-=1
    result=tm._validate(entry,b,dict(input_id=b['input_id'],items=items))
    assert (result['validation_status']=='passed')==(attack=='none')
    if attack=='none':assert result['text_selection_verified']['target_coverage']=='unconfirmed'


def test_selection_receipts_require_independent_validation():
    from cad_agent.analysis.operations import receipts
    rows=[dict(operation='ranking',fulfilled=True)]
    assert receipts('develop_method',dict(operation_receipts=rows,admitted=True))==[]
    assert receipts('develop_method',dict(operation_receipts=rows,admitted=True,text_selection_verified={'complete_in_input':True}))==rows


def test_position_sorting_is_not_a_literal_drawing_property():
    from cad_agent.analysis.typed_requirements import requirement_type
    step={'requirement':'按原生位置从左到右、同栏从上到下排序筛选文字','operations':['selection','ranking']}
    assert requirement_type({'object':'按原生位置从左','field':'右'},step)=='DELIVERY_REQUIREMENT'


def test_dynamic_source_read_closes_read_requirement_without_semantic_credit(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case,policy='efficient')
    r=develop(s,baseline(s,recovery_case))
    assert s.task.parts[0]['id'] in s.focus.read_ids
    assert r['source_binding']['state']=='candidate'


def test_unknown_dynamic_attribution_can_be_reported(workspace,recovery_case,monkeypatch):
    mocks(monkeypatch);s=session_for(workspace,recovery_case)
    r=develop(s,baseline(s,recovery_case))
    claim=Claim(text='这些局部候选原文的做法归属尚未确认。',state='unknown',evidence_basis='tool_observation',observation_ids=[r['observation_id']])
    assert not tm.claim_issues(s,claim)


@pytest.mark.parametrize('attack',['missing_quote','reversed','partial_page','foreign_keyword','source_cell','none'])
def test_only_fully_delivered_verified_position_selection_discharges_cell(attack):
    cell={'requirement_type':'DELIVERY_REQUIREMENT'}
    step={'requirement':'选择包含甲或乙的原文，从左到右，同栏从上到下。','operations':['selection','ranking'],'observation_ids':['o']}
    result=dict(method_id='m',admitted=True,selection_complete=True,
        text_selection_verified=dict(complete_in_input=True,order='columns_top_down',keywords=['甲','乙']),
        evidence=[{'evidence_id':'a','quote':'甲'},{'evidence_id':'b','quote':'乙'}])
    data={'observations':{'o':{'tool':'develop_method','data':result}}}
    claims=[dict(state='observed',evidence_basis='source',evidence_ids=['a'],text='甲'),dict(state='observed',evidence_basis='source',evidence_ids=['b'],text='乙')]
    if attack=='missing_quote':claims.pop()
    elif attack=='reversed':claims.reverse()
    elif attack=='partial_page':result['selection_complete']=False
    elif attack=='foreign_keyword':result['text_selection_verified']['keywords']=['丙']
    elif attack=='source_cell':cell['requirement_type']='SOURCE_FACT'
    proof=tm.verified_position_delivery(data,step,cell,claims)
    assert bool(proof)==(attack=='none')
    if proof:assert proof['source_attribution'] is False


def test_disabled_tools_are_not_advertised_in_task_hints(workspace,recovery_case):
    s=session_for(workspace,recovery_case,mode='off',policy='efficient')
    assert not (s.focus.names() & tm.TOOLS)
