"""Control receipts, admission races and live status must reflect actual execution."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from cad_agent.fleet.operations import OperationsStore, bounded_telemetry, usage_summary
from cad_agent.fleet.store import FleetStore, Conflict
from cad_agent.exploration import ExplorationStore, ExplorationConflict
from test_exploration import effective
from test_fleet_center import seed
from test_fleet_worker import make_worker


def command(ops,ident,action,revision=0,payload=None,key=None,kind='job'):
    return ops.submit(kind,ident,key or action,revision,action,payload or {})


def test_queued_pause_survives_restart_and_claim_skips(tmp_path):
    fleet=FleetStore(tmp_path);ops=OperationsStore(fleet)
    job=seed(fleet);other=seed(fleet)
    result=command(ops,job['id'],'pause')
    assert result['state']=='paused' and result['commands'][0]['boundary']=='before_worker_claim'
    assert FleetStore(tmp_path).claim('a')['id']==other['id']
    restarted=OperationsStore(FleetStore(tmp_path))
    assert restarted.control(job['id'])['desired_state']=='paused'
    command(restarted,job['id'],'resume',1)
    assert fleet.claim('b')['id']==job['id']


def test_commands_cas_idempotency_and_authority(tmp_path):
    fleet=FleetStore(tmp_path);ops=OperationsStore(fleet);job=seed(fleet)
    first=command(ops,job['id'],'configure',payload={'budget':{'max_tool_calls':9}})
    assert first['commands'][0]['status']=='pending'
    assert command(ops,job['id'],'configure',payload={'budget':{'max_tool_calls':9}})['revision']==1
    with pytest.raises(Conflict):command(ops,job['id'],'configure',payload={'budget':{'max_tool_calls':10}})
    with pytest.raises(Conflict):command(ops,job['id'],'pause',0)
    with pytest.raises(ValueError):ops.submit('job',job['id'],'monitor',1,'configure',{'budget':{'max_rounds':10}},'supervisor')
    for bad in ({'network':True},{'sandbox':{'network':True}},{'sandbox':{'memory_mb':2048}},{'validator':{'disable_source_checks':True}}):
        with pytest.raises(ValueError):command(ops,job['id'],'configure',1,bad,key='bad')
    with pytest.raises(ValueError):command(ops,job['id'],'pause',1,key='a'*161)


def test_running_pause_only_applied_after_fenced_receipt(tmp_path):
    fleet=FleetStore(tmp_path);ops=OperationsStore(fleet);job=seed(fleet);active=fleet.claim('a')
    fleet.heartbeat(job['id'],'a',active['attempt_id'],run_id='run1')
    control=command(ops,job['id'],'pause')
    assert control['state']=='pause_requested' and control['commands'][0]['status']=='pending'
    pending=ops.pending(job['id'],'a',active['attempt_id']);assert len(pending['commands'])==1
    kwargs={'job_id':job['id'],'node_id':'a','attempt_id':active['attempt_id']}
    telemetry=bounded_telemetry({'id':'run1','status':'running','usage':[{'total_tokens':45}],'run_events':[{'id':'step1','kind':'tool','tool':'read_source','status':'running','title':'read_source'}]},
        {'state':'pause_requested','effective':{'budget':{'max_rounds':10}}})
    ops.report(**kwargs,telemetry=telemetry,receipts=[{'command_id':'pause','status':'pending'}])
    assert ops.control(job['id'])['state']=='pause_requested'
    telemetry['control']['state']='paused'
    result=ops.report(**kwargs,telemetry=telemetry,receipts=[{'command_id':'pause','status':'applied','boundary':'before_model'}])
    assert result['state']=='paused' and result['commands'][0]['status']=='applied'
    assert ops.pending(**kwargs)['commands']==[]
    detail=ops.job_detail(job['id'])
    assert detail['usage']['total_tokens']==45
    assert any(n['kind']=='tool' and n['status']=='running' for n in detail['graph']['nodes'])
    assert any(n['kind']=='agent' and n['status']=='paused' for n in detail['graph']['nodes'])
    with fleet.connection(True) as db:db.execute('UPDATE jobs SET lease_until=? WHERE id=?',(time.time()-1,job['id']))
    with pytest.raises(Conflict):ops.report(**kwargs,telemetry=telemetry)
    assert fleet.claim('b') is None  # paused work is not silently restarted
    command(ops,job['id'],'resume',1)
    replacement=fleet.claim('b')
    assert ops.pending(job['id'],'b',replacement['attempt_id'])['commands'][0]['command_id']=='pause'


def test_unknown_usage_is_not_zero_and_stale_sample_marked(tmp_path):
    assert usage_summary({})['total_tokens'] is None
    assert usage_summary({'usage':[{'total_tokens':12},{}]})=={**usage_summary({'usage':[{'total_tokens':12},{}]}),'known':False,'total_tokens':None}
    fleet=FleetStore(tmp_path);ops=OperationsStore(fleet);job=seed(fleet);active=fleet.claim('a')
    old=(datetime.now(timezone.utc)-timedelta(seconds=40)).isoformat()
    ops.report(job['id'],'a',active['attempt_id'],telemetry=bounded_telemetry({'id':'run','usage':[]}))
    with fleet.connection(True) as db:db.execute('UPDATE operation_telemetry SET updated_at=?',(old,))
    assert ops.job_detail(job['id'])['telemetry_stale']
    overview=ops.overview();assert overview['totals']['usage_unknown_jobs']==1
    assert 'commands' not in overview['items'][0]['control']


def e0_fixture(tmp_path):
    fleet=FleetStore(tmp_path/'fleet');explore=ExplorationStore(tmp_path/'explore');fleet.exploration=explore
    upload=fleet.add_upload('test.dxf',b'dxf')
    run=explore.create_run(effective_set=effective(2),drawings=[upload])
    return fleet,explore,OperationsStore(fleet,explore),run


def test_e0_control_overlays_frozen_scope_and_admission(tmp_path):
    fleet,explore,ops,run=e0_fixture(tmp_path);ident=run['run_id'];case=run['cases'][0]
    command(ops,ident,'pause',kind='exploration')
    with pytest.raises(ExplorationConflict,match='暂停'):explore.prepare_case(ident,case['case_id'])
    command(ops,ident,'configure',1,{'dispatch_budget':{'max_concurrent_cases':2,'max_case_starts':3}},kind='exploration')
    changed=explore.get_run(ident)
    assert changed['scope']==run['scope'] and changed['scope_hash']==run['scope_hash']
    assert changed['budget']['max_concurrent_cases']==2
    command(ops,ident,'resume',2,kind='exploration')
    explore.prepare_case(ident,case['case_id'])
    assert ops.exploration_detail(ident)['control']['effective']['dispatch_budget']['max_case_starts']==3


def test_e0_pause_after_prepare_before_binding_blocks_claim_and_recovers_fanout(tmp_path):
    fleet,explore,ops,run=e0_fixture(tmp_path);ident=run['run_id'];case=run['cases'][0]
    explore.prepare_case(ident,case['case_id'])
    command(ops,ident,'pause',kind='exploration')
    payload={'upload_ids':[run['scope']['drawings'][0]['id']],'question':'read','mode':'agent','exploration_run_id':ident}
    job=fleet.create_job(payload,idempotency_key=case['dispatch_key'])
    assert fleet.claim('a') is None
    ops.reconcile_exploration(ident)
    assert ops.control(job['id'])['desired_state']=='paused'
    ops.reconcile_exploration(ident)
    assert ops.control(job['id'])['revision']==1
    command(ops,ident,'resume',1,kind='exploration')
    assert fleet.claim('a')['id']==job['id']


def test_worker_forwarding_keeps_pending_receipt_and_attempt_header(tmp_path,monkeypatch):
    worker=make_worker(tmp_path,monkeypatch)
    worker.state={'node_id':'a','job_id':'job','attempt_id':'attempt','run_id':'run'}
    forwarded=[];reports=[]
    base={'revision':0,'state':'running','commands':[],'effective':{}}
    def agent(request):
        if request.url.path.endswith('/control'):
            if request.method=='POST':
                value=json.loads(request.content);forwarded.append(value)
                assert request.headers['X-Fleet-Execution']=='attempt'
                base.update(revision=1,state='pause_requested',commands=[dict(command_id='pause',status='pending')])
            return httpx.Response(200,json=base)
        if request.url.path.endswith('/harness-events'):return httpx.Response(200,json={'items':[],'next_after':0})
        return httpx.Response(200,json={'id':'run','status':'running','usage':[]})
    worker.agent=httpx.Client(base_url='http://agent',transport=httpx.MockTransport(agent))
    def center(endpoint,**fields):
        if endpoint=='operations/poll':return {'commands':[{'command_id':'pause','revision':1,'action':'pause','payload':{},'source':'human'}]}
        reports.append(fields);return {}
    monkeypatch.setattr(worker,'request',center)
    worker.sync_operations()
    assert len(forwarded)==1 and reports[-1]['receipts'][0]['status']=='pending'
    worker.sync_operations()
    assert len(forwarded)==1
    base['commands'][0]['status']='applied';base['state']='paused'
    worker.sync_operations()
    assert reports[-1]['receipts'][0]['status']=='applied'
    assert reports[-1]['telemetry']['control']['state']=='paused'


def test_api_auth_source_and_cas(tmp_path):
    from cad_agent.fleet.app import create_app,FleetSettings
    app=create_app(FleetSettings(tmp_path,'token-'+'x'*32,password='pw'))
    job=seed(app.state.fleet_store)
    with TestClient(app) as client:
        assert client.get('/api/fleet/operations').status_code==401
        client.post('/api/fleet/login',json={'password':'pw'})
        body={'command_id':'pause','expected_revision':0,'action':'pause','payload':{}}
        assert client.post('/api/fleet/jobs/'+job['id']+'/control',json=body|{'source':'supervisor'}).status_code==422
        response=client.post('/api/fleet/jobs/'+job['id']+'/control',json=body)
        assert response.status_code==200 and response.json()['state']=='paused'
        assert client.post('/api/fleet/jobs/'+job['id']+'/control',json=body|{'command_id':'resume','action':'resume'}).status_code==409
        assert client.get('/api/fleet/operations').json()['items'][0]['control']['revision']==1
        assert client.post('/api/fleet/worker/operations/poll',json={'node_id':'a','job_id':job['id'],'attempt_id':'stale'}).status_code==401


def test_retry_usage_preserved_without_stale_attempt_double_count(tmp_path):
    fleet=FleetStore(tmp_path);ops=OperationsStore(fleet);job=seed(fleet);first=fleet.claim('a')
    fleet.heartbeat(job['id'],'a',first['attempt_id'],run_id='run-old')
    ops.report(job['id'],'a',first['attempt_id'],telemetry=bounded_telemetry({'id':'run-old','status':'failed','usage':[{'total_tokens':100}]}))
    fleet.finish(job['id'],'a',first['attempt_id'],error={'code':'retry'},retryable=True)
    second=fleet.claim('b')
    between=ops.job_detail(job['id'])
    assert between['usage']['reported_tokens']==100 and between['usage']['total_tokens'] is None
    assert between['usage']['unknown_attempts']==1
    fleet.heartbeat(job['id'],'b',second['attempt_id'],run_id='run-new')
    ops.report(job['id'],'b',second['attempt_id'],telemetry=bounded_telemetry({'id':'run-new','status':'running','usage':[{'total_tokens':50}]}))
    combined=ops.job_detail(job['id'])
    assert combined['usage']['total_tokens']==150 and combined['usage']['reported_tokens']==150


def test_pending_control_becomes_explicitly_rejected_when_job_ends(tmp_path):
    fleet=FleetStore(tmp_path);ops=OperationsStore(fleet);job=seed(fleet);active=fleet.claim('a')
    command(ops,job['id'],'guidance',payload={'message':'check source'})
    fleet.finish(job['id'],'a',active['attempt_id'],result={'answer':'done'})
    c=ops.control(job['id'])
    assert c['state']=='terminal' and c['commands'][0]['status']=='rejected'
    assert c['commands'][0]['reason']=='execution_ended_before_control_applied'


def test_pre_agent_pause_ack_only_reached_boundary(tmp_path,monkeypatch):
    worker=make_worker(tmp_path,monkeypatch)
    worker.state={'node_id':'a','job_id':'j','attempt_id':'attempt'}
    reports=[]
    def center(endpoint,**data):
        if endpoint=='operations/poll':
            return {'commands':[{'command_id':'oldpause','action':'pause'},{'command_id':'resume','action':'resume'}]}
        reports.append(data);return {}
    monkeypatch.setattr(worker,'request',center)
    worker.report_pre_agent_pause(False)
    receipts=reports[0]['receipts']
    assert receipts[0]['status']=='rejected' and receipts[1]['status']=='applied'
    assert reports[0]['telemetry']['control']['state']=='running'


def test_e0_effective_readback_and_rejection_reason(tmp_path):
    fleet,explore,ops,run=e0_fixture(tmp_path);ident=run['run_id'];case=run['cases'][0]
    explore.prepare_case(ident,case['case_id'])
    job=fleet.create_job({'upload_ids':[run['scope']['drawings'][0]['id']],'question':'read','mode':'agent','exploration_run_id':ident})
    explore.bind_job(ident,case['case_id'],job['id']);active=fleet.claim('a')
    control=command(ops,ident,'configure',payload={'budget':{'max_rounds':9}},kind='exploration')
    assert control['commands'][0]['status']=='pending' and 'applied_at' not in control['commands'][0]
    queued=ops.pending(job['id'],'a',active['attempt_id'])['commands'][0]
    effective={'budget':{'max_rounds':9},'sandbox':{'memory_mb':256},'validator':{'min_evidence_count':1}}
    ops.report(job['id'],'a',active['attempt_id'],telemetry=bounded_telemetry({'id':'r','status':'running'}, {'state':'running','effective':effective}),
               receipts=[{'command_id':queued['command_id'],'status':'applied','boundary':'model_step'}])
    detail=ops.exploration_detail(ident)
    assert detail['control']['effective']['budget']['max_rounds']==9
    assert detail['control']['effective_consistent']
    assert detail['control']['commands'][0]['status']=='applied'
    command(ops,ident,'guidance',1,{'message':'read more'},kind='exploration')
    queued=ops.pending(job['id'],'a',active['attempt_id'])['commands'][0]
    ops.report(job['id'],'a',active['attempt_id'],receipts=[{'command_id':queued['command_id'],'status':'rejected','reason':'terminal_before_delivery'}])
    detail=ops.exploration_detail(ident)
    assert detail['control']['commands'][1]['targets'][0]['reason']=='terminal_before_delivery'


def bound_e0_jobs(fleet,explore,run):
    jobs=[]
    explore.control_run(run['run_id'],'widen',0,'configure',{'dispatch_budget':{'max_concurrent_cases':2}})
    for case in run['cases']:
        explore.prepare_case(run['run_id'],case['case_id'])
        job=fleet.create_job({'upload_ids':[run['scope']['drawings'][0]['id']],'question':'read','mode':'agent','exploration_run_id':run['run_id']})
        explore.bind_job(run['run_id'],case['case_id'],job['id'])
        jobs.append(job)
    return jobs


def test_heartbeat_does_not_block_on_agent_control_io(tmp_path,monkeypatch):
    worker=make_worker(tmp_path,monkeypatch)
    worker.state={'node_id':'a','job_id':'j','attempt_id':'attempt','run_id':'r'}
    worker.lease_seconds=60;worker.last_heartbeat=time.monotonic()-10
    monkeypatch.setattr(worker,'request',lambda endpoint,**kw:{'control':{'desired_state':'paused'}})
    monkeypatch.setattr(worker,'sync_operations',lambda *a,**kw:pytest.fail('lease thread must not wait on control IO'))
    worker.heartbeat()
    assert worker.pause_before_agent and time.monotonic()-worker.last_heartbeat<1


def test_e0_pause_projection_transitions_to_applied_and_resume_waits(tmp_path):
    fleet,explore,ops,run=e0_fixture(tmp_path);ident=run['run_id'];jobs=bound_e0_jobs(fleet,explore,run)
    active=[fleet.claim('a'),fleet.claim('b')]
    command(ops,ident,'pause',1,kind='exploration')
    assert ops.exploration_detail(ident)['control']['state']=='pause_requested'
    for job in active:
        c=ops.pending(job['id'],job['node_id'],job['attempt_id'])['commands'][0]
        ops.report(job['id'],job['node_id'],job['attempt_id'],telemetry=bounded_telemetry({'id':'r','status':'running'},{'state':'paused','effective':{}}),
                   receipts=[{'command_id':c['command_id'],'status':'applied','boundary':'model_step'}])
    assert ops.exploration_detail(ident)['control']['state']=='paused'
    assert explore.get_run(ident)['control']['state']=='paused'
    command(ops,ident,'resume',2,kind='exploration')
    assert ops.exploration_detail(ident)['control']['state']=='resume_requested'
    for job in active:
        c=ops.pending(job['id'],job['node_id'],job['attempt_id'])['commands'][0]
        ops.report(job['id'],job['node_id'],job['attempt_id'],telemetry=bounded_telemetry({'id':'r','status':'running'},{'state':'running','effective':{}}),
                   receipts=[{'command_id':c['command_id'],'status':'applied','boundary':'model_step'}])
    assert ops.exploration_detail(ident)['control']['state']=='running'


def test_e0_missing_child_receipt_never_counts_as_applied(tmp_path,monkeypatch):
    fleet,explore,ops,run=e0_fixture(tmp_path);ident=run['run_id'];jobs=bound_e0_jobs(fleet,explore,run)
    active=[fleet.claim('a'),fleet.claim('b')]
    command(ops,ident,'configure',1,{'budget':{'max_rounds':9}},kind='exploration')
    good=ops.pending(active[0]['id'],'a',active[0]['attempt_id'])['commands'][0]
    ops.report(active[0]['id'],'a',active[0]['attempt_id'],receipts=[{'command_id':good['command_id'],'status':'applied'}])
    with fleet.connection(True) as db:db.execute('DELETE FROM operation_commands WHERE job_id=?',(active[1]['id'],))
    monkeypatch.setattr(ops,'reconcile_exploration',lambda run_id:None)  # fanout CAS failed for second child
    detail=ops.exploration_detail(ident)
    value=next(c for c in detail['control']['commands'] if c['action']=='configure' and 'budget' in c['payload'])
    assert value['status']=='pending'
    assert any(t.get('reason')=='receipt_not_received' for t in value['targets'])


def test_e0_receipt_lookup_is_not_limited_to_browser_tail(tmp_path):
    fleet,explore,ops,run=e0_fixture(tmp_path);ident=run['run_id'];case=run['cases'][0]
    explore.prepare_case(ident,case['case_id'])
    job=fleet.create_job({'upload_ids':[run['scope']['drawings'][0]['id']],'question':'read','mode':'agent','exploration_run_id':ident})
    explore.bind_job(ident,case['case_id'],job['id']);active=fleet.claim('a')
    command(ops,ident,'configure',payload={'budget':{'max_rounds':9}},kind='exploration')
    c=ops.pending(job['id'],'a',active['attempt_id'])['commands'][0]
    ops.report(job['id'],'a',active['attempt_id'],receipts=[{'command_id':c['command_id'],'status':'applied'}])
    for n in range(102):command(ops,job['id'],'guidance',n+1,{'message':'note'},key='guide'+str(n))
    assert len(ops.control(job['id'])['commands'])==100
    detail=ops.exploration_detail(ident)
    assert detail['control']['commands'][0]['status']=='applied'
