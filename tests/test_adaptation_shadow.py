import copy
import json
from dataclasses import replace
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession
from cad_agent.extensions.adaptation.contracts import Start, Proposal, Promote
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.shadow import ShadowService, Policy, Target
from cad_agent.extensions.adaptation.model import subscription_turn
from cad_agent.extensions.table_recovery.worker import recover
from cad_agent.adapters.model import ModelError
from test_table_recovery import recovery_case


def policy(recovery_case, **kw):
    return Policy(idempotency_key='test-shadow', mode='dynamic',
        targets=[Target(snapshot_id=recovery_case[0]['id'], table_id=recovery_case[3]['table_id'])],
        instruction='test', max_runs=1, max_model_calls=3, max_reserved_tokens=96000, **kw)


def fake_model(monkeypatch, bundle):
    calls=[]
    def propose(*a, **kw):
        calls.append((a,kw))
        return Proposal(strategy='code',explanation='test',code='def adapt(bundle):\n return {}'),dict(usage={},reserved_tokens=100)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',propose)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output=recover(bundle)))
    return calls


def test_trial_scope_idempotency_budget_and_no_promotion(workspace,recovery_case,monkeypatch):
    calls=fake_model(monkeypatch,recovery_case[3]);s=ShadowService(workspace);p=s.create(policy(recovery_case))
    sid=recovery_case[0]['id'];tid=recovery_case[3]['table_id']
    with pytest.raises(ValueError,match='SCOPE_DENIED'):s.trial(p['policy_id'],sid,'foreign')
    e=s.trial(p['policy_id'],sid,tid,background=False)
    assert s.trial(p['policy_id'],sid,tid,background=False)==e and len(calls)==1
    r=AdaptationService(workspace,sid).get(e['run_id']);assert r['output_mode']=='shadow_only'
    assert r['status']=='awaiting_review' and not r['automatic_adoption']
    ledger=workspace.store.get('adaptation_policy_ledger',p['policy_id']+'-ledger')
    assert ledger['reserved_model_calls']==3 and ledger['reserved_tokens']==96000
    with pytest.raises(ValueError,match='SHADOW_RUN_NOT_PROMOTABLE'):
        AdaptationService(workspace,sid).promote(Promote(run_ids=[r['run_id']]*2,name='forbidden'))
    s.disable(p['policy_id'])
    with pytest.raises(ValueError,match='DISABLED_OR_EXPIRED'):s.trial(p['policy_id'],sid,tid)


def test_policy_integrity_and_model_pin(workspace,recovery_case):
    s=ShadowService(workspace);p=s.create(policy(recovery_case));sid=recovery_case[0]['id'];tid=recovery_case[3]['table_id']
    workspace.model_catalog['siwc_sol']=replace(workspace.model_catalog['siwc_sol'],model='changed')
    with pytest.raises(ValueError,match='MODEL_CHANGED'):s.trial(p['policy_id'],sid,tid)
    p['spec']['max_runs']=99;workspace.store.put('adaptation_policy',p['policy_id'],p)
    with pytest.raises(ValueError,match='INTEGRITY_FAILED'):s.get(p['policy_id'])


def test_revision_uses_verified_prior_code_and_errors(workspace,recovery_case,monkeypatch):
    b=recovery_case[3];s=AdaptationService(workspace,recovery_case[0]['id'])
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output={}))
    old=s.start(Start(table_id=b['table_id'],mode='fixed',idempotency_key='old'),background=False)
    calls=fake_model(monkeypatch,b)
    new=s.start(Start(table_id=b['table_id'],mode='dynamic',revision_of_run_id=old['run_id'],idempotency_key='new'),background=False)
    assert new['status']=='awaiting_review'
    prior=calls[0][0][4][0]
    assert prior['code'] and prior['errors'] and prior['revision_of']==old['run_id']
    assert new['revision_parent_code_sha256']==old['attempts'][0]['code_sha256']


def test_agent_wire_unchanged_and_skips_without_host_gap(workspace,recovery_case,monkeypatch):
    s=ShadowService(workspace);p=s.create(policy(recovery_case,agent_shadow_enabled=True))
    sid=recovery_case[0]['id'];tid=recovery_case[3]['table_id'];queued=[]
    monkeypatch.setattr(workspace.executor,'submit',lambda fn,*a:queued.append((fn,a)))
    args=dict(snapshot_id=sid,question='表格',agent_policy='legacy')
    base=CadAgentSession(workspace,ChatRequest(**args));shadow=CadAgentSession(workspace,ChatRequest(**args,adaptation_policy_id=p['policy_id']))
    a=base.invoke_tool('read_table',json.dumps({'table_id':tid}));b=shadow.invoke_tool('read_table',json.dumps({'table_id':tid}))
    assert a==b and base.evidence==shadow.evidence and base.observations==shadow.observations
    assert len(queued)==1
    assert s.observe(p['policy_id'],sid,tid)==dict(status='skipped',reason='NO_HOST_CONFIRMED_GAP')
    assert not workspace.store.list('adaptation_run')


def test_subscription_proposal_payload_and_no_provider_fallback(workspace,monkeypatch):
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    settings=workspace.model_catalog['siwc_sol'];seen=[]
    account=SimpleNamespace(lease=lambda:nullcontext())
    def request(self,session,payload):
        seen.append(payload);session.usage.append(dict(total_tokens=12,source='siwc',billing='subscription'))
        return dict(output=[dict(type='reasoning'),dict(type='message',role='assistant',content=[dict(type='output_text',text='{}')])])
    monkeypatch.setattr(ResponsesTransport,'request',request)
    m,u,d=subscription_turn(settings,[dict(role='system',content='instructions'),dict(role='user',content='local')],5,account)
    assert m['content']=='{}' and u=={'total_tokens':12}
    p=seen[0];assert p['model']=='gpt-6.1-sol' and p['store'] is False and p['stream'] is True
    assert p['tool_choice']=='none' and p['input']==[dict(role='user',content='local')]
    assert 'max_output_tokens' not in p and 'temperature' not in p
    def failed(*a,**kw):raise ModelError('SIWC_ACCESS_DENIED','denied')
    monkeypatch.setattr(ResponsesTransport,'request',failed)
    with pytest.raises(ModelError):subscription_turn(settings,[dict(content='system'),dict(role='user',content='local')],5,account)


def test_shared_budget_expiry_and_sealed_diagnostic(workspace,recovery_case,monkeypatch):
    from cad_agent.extensions.table_recovery.bundle import digest
    b=recovery_case[3];sid=recovery_case[0]['id'];tid=b['table_id'];s=ShadowService(workspace)
    args=policy(recovery_case).model_copy(update={'targets':[Target(snapshot_id=sid,table_id=tid),Target(snapshot_id=sid,table_id='other')]})
    p=s.create(args);fake_model(monkeypatch,b);s.trial(p['policy_id'],sid,tid,background=False)
    with pytest.raises(ValueError,match='BUDGET_EXHAUSTED'):s.trial(p['policy_id'],sid,'other')
    p['expires_at']='2000-01-01T00:00:00+00:00';p['seal']=digest({k:v for k,v in p.items() if k!='seal'})
    workspace.store.put('adaptation_policy',p['policy_id'],p)
    with pytest.raises(ValueError,match='DISABLED_OR_EXPIRED'):s.trial(p['policy_id'],sid,tid)


def test_observe_blocked_inputs_and_verified_method_gap(workspace,recovery_case,monkeypatch):
    from cad_agent.extensions.table_recovery.bundle import digest
    b=recovery_case[3];sid=recovery_case[0]['id'];tid=b['table_id'];s=AdaptationService(workspace,sid)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output={}))
    old=s.start(Start(table_id=tid,mode='fixed',idempotency_key='diagnostic'),background=False)
    args=policy(recovery_case,agent_shadow_enabled=True).model_copy(update={'targets':[Target(snapshot_id=sid,table_id=tid,diagnostic_run_id=old['run_id'])]})
    shadow=ShadowService(workspace);p=shadow.create(args);queued=[]
    monkeypatch.setattr(workspace.executor,'submit',lambda fn,*a:queued.append((fn,a)))
    bad=copy.deepcopy(b);bad['coverage']['blocking_diagnostics']=['MISSING']
    monkeypatch.setattr('cad_agent.extensions.table_recovery.bundle.prepare',lambda *a:bad)
    assert shadow.observe(p['policy_id'],sid,tid)['reason']=='INPUT_INCOMPLETE_OR_UNSUPPORTED'
    assert not queued
    monkeypatch.setattr('cad_agent.extensions.table_recovery.bundle.prepare',lambda *a:b)
    result=shadow.observe(p['policy_id'],sid,tid)
    assert result['initiator']=='agent_shadow' and result['reason']=='VALIDATED_METHOD_GAP'
    assert len(queued)==1
    again=shadow.observe(p['policy_id'],sid,tid)
    assert again['run_id']==result['run_id'] and len(queued)==1
    # Do not leave the deliberately unstarted fixture job active.
    workspace._adaptation_jobs[result['run_id']]['done'].set()


def test_shadow_api_rejects_raw_code_and_foreign_target(workspace,recovery_case):
    from fastapi.testclient import TestClient
    from cad_agent.app import create_app
    sid=recovery_case[0]['id'];prefix=f'/api/drawings/{sid}/adaptations'
    with TestClient(create_app(workspace.settings)) as client:
        payload=policy(recovery_case).model_dump()
        assert client.post(prefix+'/policies',json={**payload,'code':'arbitrary'}).status_code==422
        response=client.post(prefix+'/policies',json=payload);assert response.status_code==200
        p=response.json()
        response=client.post(prefix+'/policies/'+p['policy_id']+'/trials',json={'table_id':'foreign'})
        assert response.status_code==400 and 'SHADOW_SCOPE_DENIED' in response.text


def test_unavailable_sidecar_does_not_replace_tool_result(workspace,recovery_case,monkeypatch):
    from cad_agent.contracts import stable_id
    sid=recovery_case[0]['id'];tid=recovery_case[3]['table_id'];args=dict(snapshot_id=sid,question='表格',agent_policy='legacy')
    normal=CadAgentSession(workspace,ChatRequest(**args));wire=normal.invoke_tool('read_table',json.dumps({'table_id':tid}))
    def closed(*a,**kw):raise RuntimeError('executor closed')
    monkeypatch.setattr(workspace.executor,'submit',closed)
    shadow=CadAgentSession(workspace,ChatRequest(**args,adaptation_policy_id='policy'))
    assert shadow.invoke_tool('read_table',json.dumps({'table_id':tid}))==wire
    event=workspace.store.get('adaptation_shadow_observation',stable_id('adobserve','policy',sid,tid))
    assert event==dict(status='refused',reason='SHADOW_EXECUTOR_UNAVAILABLE')
