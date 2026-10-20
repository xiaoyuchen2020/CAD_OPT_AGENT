import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.tasks import TaskLedger, TaskPlan, PlanStep, TaskUnitConfirmation, TaskBatchNavigation, natural_parts
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.service import GeometryService
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from test_room_batch import room_fixture
from test_siwc import connected_lease, response
from test_siwc_stopping import tool, answer

QUESTION='三层平面图有哪些试点房间？面积分别是多少？并高亮这些候选。同时读取图纸标题原文。'


def session(workspace):
    service,query,drawing=room_fixture(workspace)
    r=AgentRunner(workspace,ChatRequest(snapshot_id=drawing['id'],question=QUESTION,mode='agent',runtime='responses_subscription',model_profile='siwc_sol'))
    return r,query


def plan(r):
    return TaskPlan(steps=[PlanStep(requirement_id=s['id'],kind=s['kind'],depends_on=['q1'] if s['id'] in ('q2','q3') else [],acceptance='有证据的交付，或明确说明缺口。') for s in r.task.data['subtasks']])


def scripted_result(workspace,monkeypatch):
    r,q=session(workspace);connected_lease(workspace,monkeypatch);calls=[]
    rid=next(x.region_id for x in workspace.regions(r.request.snapshot_id) if x.text=='三层平面图')
    def invoke(self,s,payload):
        calls.append(payload)
        if len(calls)==1:
            # Stable schema prefixes retain relevant capabilities; execution
            # remains plan-only until the task's prerequisites are established.
            assert 'plan_task' in {t['name'] for t in payload['tools'][0]['tools']}
            assert {t['function']['name'] for t in s.offered_tools(planning_only=True)} == {'plan_task'}
            return tool('plan_task',plan(r).model_dump(),'p')
        if len(calls)==2:return tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2','q3']},'b')
        if len(calls)==3:
            assert payload['tools'] and not r.policy.stop_reason
            assert r.task.data['subtasks'][1]['status']=='pending'
            return tool('prepare_room_boundaries',{'batch_id':r.room_batch['batch_id'],'task_ids':['q2','q3']},'geometry')
        if len(calls)==4:
            from test_workflow_contract import room_choices
            return tool('select_room_boundaries',room_choices(r)|{'task_ids':['q2','q3']},'select')
        if len(calls)==5:return tool('read_source_context',{'region_id':rid,'neighbors':0,'task_ids':['q4']},'read')
        eid=next(e['evidence_id'] for e in r.evidence.values() if e['region_id']==rid)
        return answer(json.dumps({'claims':[{'text':'图中标题为三层平面图。','state':'observed','evidence_ids':[eid],'question_part_ids':['q4']}]}))
    monkeypatch.setattr(ResponsesTransport,'request',invoke)
    result=r.run()
    return r,result,calls


def test_natural_requirements_preserve_all_clauses():
    p=natural_parts(QUESTION)
    assert len(p)==4 and '高亮' in p[2]['text'] and '标题' in p[3]['text']
    assert len(natural_parts('列出对象，测量面积，并高亮。'))==3
    assert len(natural_parts('①材料②厚度'))==2


@pytest.mark.parametrize('fault',['missing','duplicate','cycle','foreign'])
def test_invalid_plan_cannot_drop_or_cycle_requirements(workspace,fault):
    r,_=session(workspace);p=plan(r).model_dump()
    if fault=='missing':p['steps'].pop()
    if fault=='duplicate':p['steps'][-1]['requirement_id']='q1'
    if fault=='cycle':p['steps'][0]['depends_on']=['q2']
    if fault=='foreign':p['steps'][0]['depends_on']=['q99']
    with pytest.raises(ValueError):r.task.plan(TaskPlan.model_validate(p))
    assert not r.task.data['planned']


def test_batch_block_does_not_stop_independent_lookup(workspace,monkeypatch):
    r,result,calls=scripted_result(workspace,monkeypatch)
    assert len(calls)==6 and len(result['trace'])==5
    assert result['task']['status']=='waiting_input'
    assert [s['status'] for s in result['task']['subtasks']]==['completed','waiting_input','completed','completed']
    assert result['task']['delivery_check']['all_requirements_addressed']
    assert result['citations'] and not result['fallback_used']
    assert workspace.store.get('agent_task',result['task']['task_id'])['evidence']


def test_unit_resume_preserves_unrelated_sources_and_does_not_call_model(workspace,monkeypatch):
    r,result,_=scripted_result(workspace,monkeypatch);tid=result['task']['task_id'];bid=result['room_batch']['batch_id']
    oid=next(oid for oid,o in r.observations.items() if o['tool']=='analyze_room_batch')
    r.task.data['subtasks'][0]['claims']=[{'text':'有一个名称候选','state':'derived','observation_ids':[oid]}]
    r.task.save()
    old=deepcopy(r.task.data['subtasks'][3]);old_evidence=deepcopy(r.task.data['evidence'])
    monkeypatch.setattr(ResponsesTransport,'request',lambda *a:pytest.fail('confirmation must be local'))
    args=TaskUnitConfirmation(batch_id=bid,unit='mm',confirmed=True)
    out=TaskLedger.confirm_unit(workspace,tid,args)
    assert out['model_calls']==0 and out['room_batches'][0]['items'][0]['candidate_area_m2']==pytest.approx(24)
    assert out['task']['subtasks'][3]==old
    assert TaskLedger.read(workspace,tid)['evidence']==old_evidence
    assert out['task']['subtasks'][1]['status']=='partial'
    assert out['task']['subtasks'][1]['claims']==[]
    assert out['task']['subtasks'][0]['claims']==[]  # old batch observations were superseded
    assert out['task']['delivery_check']['all_requirements_addressed']
    monkeypatch.setattr(GeometryService,'room',lambda *a:pytest.fail('idempotent confirmation'))
    assert TaskLedger.confirm_unit(workspace,tid,args)['room_batches']==out['room_batches']
    with pytest.raises(ValueError):TaskLedger.confirm_unit(workspace,tid,args.model_copy(update={'unit':'m'}))


def test_missing_requirement_cannot_publish_complete_answer(workspace):
    r,q=session(workspace);r.task.plan(plan(r))
    r.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2','q3']})
    with pytest.raises(ValueError,match='TASK_WORK_PENDING|未覆盖问题分项'):
        r.finish(json.dumps({'claims':[{'text':'单位待确认','state':'unknown','question_part_ids':['q2']}]}))
    assert r.task.data['subtasks'][3]['status']=='pending'


def test_batch_delivery_checks_each_row_and_unknown_unit(workspace,monkeypatch):
    r,result,_=scripted_result(workspace,monkeypatch)
    b=next(iter(r.task.data['batches'].values()));b['items']=[]
    with pytest.raises(ValueError,match='逐项交付'):r.task.finalise(result)


def test_confirmation_invalidates_dependent_comparison_only(workspace,monkeypatch):
    r,result,_=scripted_result(workspace,monkeypatch)
    d=r.task.data;d['subtasks'].append(dict(d['subtasks'][3],id='q5',kind='compare',depends_on=['q2'],status='completed'))
    r.task.save()
    out=TaskLedger.confirm_unit(workspace,d['task_id'],TaskUnitConfirmation(batch_id=result['room_batch']['batch_id'],unit='mm',confirmed=True))
    assert out['task']['subtasks'][-1]['status']=='pending'
    assert out['task']['subtasks'][3]['status']=='completed'
    resumed=AgentRunner(workspace,r.request.model_copy(update={'resume_task_id':d['task_id']}))
    assert resumed.evidence and not resumed.task.needs_plan
    with pytest.raises(ValueError):resumed.task.start(['q4'])
    assert resumed.trace[0]['tool']=='restore_task'


def test_api_confirmation_rejects_foreign_batch_and_running_task(workspace,monkeypatch):
    r,result,_=scripted_result(workspace,monkeypatch)
    with TestClient(create_app(Settings(data_dir=workspace.settings.data_dir))) as client:
        tid=result['task']['task_id'];path='/api/tasks/'+tid
        assert client.get(path).status_code==200
        assert client.post(path+'/confirm-unit',json={'batch_id':'foreign','unit':'mm','confirmed':True}).status_code==400
        assert client.post(path+'/confirm-unit',json={'batch_id':result['room_batch']['batch_id'],'unit':'mm'}).status_code==422
        d=TaskLedger.read(workspace,tid);d['status']='running';workspace.store.put('agent_task',tid,d)
        assert client.post(path+'/confirm-unit',json={'batch_id':result['room_batch']['batch_id'],'unit':'mm','confirmed':True}).status_code==400


def test_subtask_repetition_does_not_exhaust_independent_task(workspace):
    r,_=session(workspace);r.task.plan(plan(r))
    args={'query':'nothing-matches','task_ids':['q4']}
    for _ in range(3):r.invoke_tool('search_sources',args)
    assert r.task.data['subtasks'][3]['status']=='partial'
    assert r.task.data['subtasks'][3]['gap_kind']=='budget_exhausted'
    assert 'q1' in r.task.runnable() and not r.policy.stop_reason


def test_pagination_stays_owned_and_does_not_complete_unseen_items(workspace):
    r,q=session(workspace)
    regions=workspace.regions(r.request.snapshot_id)
    label=next(x for x in regions if x.text=='试点房间')
    regions.append(label.model_copy(update={'region_id':'another_label','entity_ids':['another_entity'],'bbox':[8000,0,8500,500]}))
    workspace.store.artifact(r.request.snapshot_id,'regions.json').write_text(json.dumps([x.model_dump() for x in regions]))
    r.task.plan(plan(r))
    r.invoke_tool('analyze_room_batch',q.model_copy(update={'limit':1}).model_dump()|{'task_ids':['q1','q2','q3']})
    assert r.task.data['subtasks'][0]['status']=='partial'
    tid=r.task.data['task_id'];bid=r.room_batch['batch_id']
    out=TaskLedger.navigate_batch(workspace,tid,TaskBatchNavigation(batch_id=bid,offset=1))
    assert out['model_calls']==0 and out['task']['subtasks'][0]['status']=='completed'
    assert out['task']['subtasks'][1]['status']=='pending'
    assert len(out['task']['batch_refs'])==2
    assert out['batch']['batch_id'] in TaskLedger.read(workspace,tid)['batches']
    with pytest.raises(ValueError):TaskLedger.navigate_batch(workspace,tid,TaskBatchNavigation(batch_id=bid,offset=999))


def test_task_scope_selection_retains_original_requirements(workspace):
    r,q=session(workspace);d=workspace.store.get('drawing',r.request.snapshot_id)
    d['frames'].append(dict(d['frames'][0],frame_id='floor2'))
    workspace.store.put('drawing',d['id'],d)
    rows=workspace.regions(d['id']);title=next(x for x in rows if x.text=='三层平面图')
    rows.append(title.model_copy(update={'region_id':'second_title','entity_ids':['second_title_entity'],'frame_id':'floor2'}))
    workspace.store.artifact(d['id'],'regions.json').write_text(json.dumps([x.model_dump() for x in rows]))
    r.task.plan(plan(r));r.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2','q3']})
    bid=r.room_batch['batch_id'];tid=r.task.data['task_id']
    assert r.task.data['subtasks'][0]['status']=='waiting_input'
    with pytest.raises(ValueError):TaskLedger.navigate_batch(workspace,tid,TaskBatchNavigation(batch_id=bid,frame_id='foreign'))
    out=TaskLedger.navigate_batch(workspace,tid,TaskBatchNavigation(batch_id=bid,frame_id='floor'))
    assert out['task']['subtasks'][0]['status']=='completed'
    assert out['task']['subtasks'][1]['status']=='pending'
    assert out['task']['subtasks'][3]['status']=='pending'
    assert bid not in TaskLedger.read(workspace,tid)['batches']


def test_restart_keeps_completed_delivery_and_recovers_running_step(workspace):
    from cad_agent.adapters.store import Store
    r,_=session(workspace);r.task.plan(plan(r));r.task.data['status']='running'
    r.task.data['subtasks'][0].update(status='completed',delivered=True)
    r.task.data['subtasks'][3]['status']='running';r.task.save()
    assert Store(workspace.settings.data_dir).get('agent_task',r.task.data['task_id'])['status']=='running'
    workspace.execution.recover()
    restored=workspace.store.get('agent_task',r.task.data['task_id'])
    assert restored['status']=='partial' and restored['subtasks'][0]['status']=='completed'
    assert restored['subtasks'][3]['status']=='pending'
