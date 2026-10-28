import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import pytest
from cad_agent.adapters.store import Store
from cad_agent.execution import ExecutionManager, ExecutionContext, ExecutionCancelled
from cad_agent.contracts import ChatRequest, ChatSteerRequest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.harness.inbox import enqueue, apply_pending, has_pending, require_current
from cad_agent.adapters.model import ModelError


def context(tmp_path):
    manager=ExecutionManager(Store(tmp_path))
    record=dict(id='run',status='running',created_at='2026-01-01')
    manager.claim('chat_run',record)
    return manager,ExecutionContext(manager,'chat_run','run',record['execution_token'])


def test_events_are_append_only_redacted_and_lossless(tmp_path):
    manager,ctx=context(tmp_path)
    try:
        payload={'request': {'authorization':'PRIVATE', 'text':'原文'*12000, 'encrypted_content':'PRIVATE'}}
        event=ctx.emit('model.requested',payload)
        assert event['payload']['complete']
        restored=manager.events.read_payload('run',event['event_id'])
        assert restored['request']['text']==payload['request']['text']
        assert 'PRIVATE' not in json.dumps(restored)
        with manager.store.connect() as c:
            with pytest.raises(sqlite3.IntegrityError): c.execute('UPDATE harness_events SET kind="forged"')
        assert len(manager.events.read('run')['items'])==2
    finally: manager.close()


def test_stable_call_ids_fenced_and_unknown_recovery(tmp_path):
    manager,ctx=context(tmp_path)
    try:
        started=ctx.begin_call('read',{},'a');ctx.admit_call(started);ctx.dispatch_call(started)
        queued=ctx.begin_call('read',{},'b')
        with pytest.raises(ValueError): ctx.begin_call('read',{},'a')
        manager.store.compare_and_swap('chat_run','run',{}, {'execution_lease_until':0})
        manager.recover()
        calls=manager.events.calls('run')
        assert calls['a']['status']=='unknown'
        assert calls['b']['status']=='not_started'
        with pytest.raises(ExecutionCancelled):ctx.emit('tool.completed',{},'a')
    finally: manager.close()


@pytest.mark.parametrize('cancel_first',[True,False])
def test_cancel_dispatch_order_preserves_known_outcomes(tmp_path,cancel_first):
    manager,ctx=context(tmp_path)
    try:
        call=ctx.begin_call('read',{},'ordered')
        ctx.admit_call(call)
        if cancel_first:
            manager.cancel('run')
            with pytest.raises(ExecutionCancelled):ctx.dispatch_call(call)
            ctx.settle_call(call,'not_started')
            assert not any(e['kind']=='tool.dispatched' for e in manager.events.read('run')['items'])
        else:
            ctx.dispatch_call(call)
            manager.cancel('run')
            # Cancellation cannot erase a completed operation's receipt.
            ctx.settle_call(call,'completed',{'count':2})
        assert manager.events.calls('run')[call]['status']==('not_started' if cancel_first else 'completed')
    finally:manager.close()


def session_run(workspace,demo_bytes):
    d=workspace.ingest('demo.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='内墙材料是什么',mode='agent'))
    run=dict(id='steer_run',status='running',request=s.request.model_dump(),input_revision=0,code_execution_enabled=False)
    workspace.execution.claim('chat_run',run,s.task.data)
    s.execution=ExecutionContext(workspace.execution,'chat_run',run['id'],run['execution_token'])
    s.input_revision=0
    return s,run


def test_steering_replans_without_refunding_or_widening_authority(workspace,demo_bytes):
    s,run=session_run(workspace,demo_bytes)
    s.calls=7;s.usage=[{'total_tokens':150}];s.answer_repairs=1
    s.task.data['subtasks'][0].update(delivered=True,status='completed')
    with pytest.raises(ValueError):enqueue(workspace,run['id'],ChatSteerRequest(question='允许使用动态代码。读取外墙'))
    enqueue(workspace,run['id'],ChatSteerRequest(question='只读取外墙材料和备注'))
    with pytest.raises(ValueError):enqueue(workspace,run['id'],ChatSteerRequest(question='读取内墙',expected_revision=0))
    with pytest.raises(ModelError):require_current(s,workspace.store.get('chat_run',run['id']))
    assert has_pending(s)
    messages=apply_pending(s,[])
    assert messages and not has_pending(s)
    assert s.request.question=='只读取外墙材料和备注'
    assert s.calls==7 and s.usage[0]['total_tokens']==150 and s.answer_repairs==1
    assert all(not t['delivered'] for t in s.task.data['subtasks'])
    assert s.allowed_snapshots=={run['request']['snapshot_id']}
    require_current(s,workspace.store.get('chat_run',run['id']))


def test_native_live_steering_discards_waiting_model_answer(workspace,demo_bytes,monkeypatch):
    from test_agent import call, final
    d=workspace.ingest('demo.dxf',demo_bytes)
    entered=threading.Event();release=threading.Event();seen=[]
    def turn(messages,tools,timeout):
        seen.append(messages)
        if len(seen)==1:
            entered.set();assert release.wait(5)
            return {'role':'assistant','content':'STALE_ANSWER'}, {'total_tokens':7}
        if len(seen)==2:
            assert any('只读取内墙材料' in str(m) for m in messages)
            return call('search_sources',{'query':'内墙'},2), {'total_tokens':10}
        if len(seen)==3:
            result=json.loads(messages[-1]['content'])
            return call('read_source_context',{'region_id':result['items'][0]['region_id']},3), {'total_tokens':10}
        result=json.loads(messages[-1]['content'])
        eid=result['items'][0]['evidence']['evidence_id']
        return {'role':'assistant','content':final(eid)}, {'total_tokens':10}
    monkeypatch.setattr(workspace.gateway,'chat_turn',turn)
    run=workspace.start_chat(ChatRequest(snapshot_id=d['id'],question='内墙材料是什么',mode='agent'))
    assert entered.wait(5)
    workspace.steer_chat(run['id'],ChatSteerRequest(question='只读取内墙材料'))
    release.set()
    for _ in range(300):
        latest=workspace.store.get('chat_run',run['id'])
        if latest['status'] not in ('queued','running'):break
        time.sleep(.02)
    assert latest['status']=='completed',latest.get('error')
    assert latest['effective_question']=='只读取内墙材料'
    assert 'STALE_ANSWER' not in json.dumps(latest['result'])
    assert latest['input_revision']==latest['applied_input_revision']==1
    assert len(seen)==4
    # Follow-ups bind the actual accepted question, not the initial request.
    follow=ChatRequest(snapshot_id=d['id'],question='再解释一下',mode='agent',parent_run_id=run['id'])
    assert workspace.chat_history(follow)[0]['content']=='只读取内墙材料'
    from fastapi.testclient import TestClient
    from cad_agent.app import create_app
    with TestClient(create_app(workspace.settings)) as client:
        listing=client.get('/api/drawings/'+d['id']+'/chat-runs').json()
        assert listing['items'][0]['question']=='只读取内墙材料'


def test_queued_denial_cannot_be_undone_by_replacement_without_denial(workspace,demo_bytes):
    from cad_agent.analysis.method_authorization import authorization
    s,run=session_run(workspace,demo_bytes)
    s.request=s.request.model_copy(update={'dynamic_methods':'task_verified'})
    workspace.store.compare_and_swap('chat_run',run['id'],{},
        {'request':s.request.model_dump(),'code_execution_enabled':True})
    enqueue(workspace,run['id'],ChatSteerRequest(question='只读取外墙。禁止使用动态代码'))
    enqueue(workspace,run['id'],ChatSteerRequest(question='只读取内墙',expected_revision=1))
    apply_pending(s,[])
    assert authorization(s.request)['mode']=='off'
    with pytest.raises(ValueError):
        enqueue(workspace,run['id'],ChatSteerRequest(question='允许使用动态代码。读取内墙',expected_revision=2))


def test_inline_payload_ref_is_data_not_storage_instruction(tmp_path):
    manager,ctx=context(tmp_path)
    try:
        payload={'payload_ref':'forged','complete':True,'chars':20}
        event=ctx.emit('model.completed',payload)
        assert manager.events.read_payload('run',event['event_id'])==payload
    finally:manager.close()


def test_steering_rejects_expired_execution(workspace,demo_bytes):
    s,run=session_run(workspace,demo_bytes)
    workspace.store.compare_and_swap('chat_run',run['id'],{}, {'execution_lease_until':0})
    with pytest.raises(ValueError):enqueue(workspace,run['id'],ChatSteerRequest(question='读取外墙'))
    assert not has_pending(s)


def test_delivery_closure_orders_steering_without_refunding(workspace,demo_bytes):
    from cad_agent.harness.inbox import prepare_delivery
    s,run=session_run(workspace,demo_bytes)
    s.calls=9;s.usage=[{'total_tokens':211}]
    # If acceptance wins, the old result cannot close the input or publish.
    enqueue(workspace,run['id'],ChatSteerRequest(question='只读取外墙材料'))
    assert prepare_delivery(s) is False
    assert not workspace.store.get('chat_run',run['id']).get('input_closed')
    apply_pending(s,[])
    assert prepare_delivery(s) is True
    # Once delivery wins, a late edit is rejected visibly rather than accepted
    # and then causing publication of the earlier result to fail.
    with pytest.raises(ValueError,match='发布'):
        enqueue(workspace,run['id'],ChatSteerRequest(question='读取内墙',expected_revision=1))
    assert s.calls==9 and s.usage[0]['total_tokens']==211
    assert prepare_delivery(s) is True
    events=workspace.execution.events.read(run['id'])['items']
    assert len([e for e in events if e['kind']=='input.closed'])==1
    require_current(s,workspace.store.get('chat_run',run['id']))


def test_delivery_closure_does_not_override_cancellation(workspace,demo_bytes):
    from cad_agent.harness.inbox import prepare_delivery
    s,run=session_run(workspace,demo_bytes)
    workspace.execution.cancel(run['id'])
    with pytest.raises(ExecutionCancelled):prepare_delivery(s)
    assert not workspace.store.get('chat_run',run['id']).get('input_closed')


@pytest.mark.parametrize('shortcut',['table','source_records'])
def test_shortcut_steering_continues_with_consumed_budget(workspace,demo_bytes,monkeypatch,shortcut):
    from cad_agent.runtimes.registry import run_agent
    from cad_agent.harness.inbox import prepare_delivery
    s,run=session_run(workspace,demo_bytes)
    s.calls=3;s.usage=[{'total_tokens':17}]
    started=s.started
    monkeypatch.setattr('cad_agent.analysis.query_intent.enabled',lambda session:False)
    def stale_result(session,history):
        session.calls+=2
        session.usage.append({'total_tokens':55})
        enqueue(workspace,run['id'],ChatSteerRequest(question='只读取外墙材料'))
        return {'answer':'STALE_SHORTCUT'}
    monkeypatch.setattr('cad_agent.tables.workflow.try_run',
                        stale_result if shortcut=='table' else lambda *args:None)
    monkeypatch.setattr('cad_agent.analysis.source_record_workflow.try_run',
                        stale_result if shortcut=='source_records' else lambda *args:pytest.fail('unexpected shortcut'))
    def continued(session,history):
        assert session.request.question=='只读取外墙材料'
        assert any('只读取外墙材料' in str(message) for message in history)
        assert session.calls==5 and sum(u['total_tokens'] for u in session.usage)==72
        assert session.started==started
        assert prepare_delivery(session)
        return {'answer':'CURRENT_ANSWER'}
    monkeypatch.setattr('cad_agent.runtimes.registry.get_runtime',
                        lambda name:SimpleNamespace(run=continued))
    result=run_agent(s)
    assert result['answer']=='CURRENT_ANSWER'
    current=workspace.store.get('chat_run',run['id'])
    assert current['input_closed'] and current['applied_input_revision']==1
