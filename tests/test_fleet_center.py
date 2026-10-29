from concurrent.futures import ThreadPoolExecutor
import json
import time
import pytest
from fastapi.testclient import TestClient
from cad_agent.fleet.app import create_app, FleetSettings, password_hash
from cad_agent.fleet.store import FleetStore, Conflict


def seed(store, **kwargs):
    upload = store.add_upload('../sample.dxf', b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n')
    return store.create_job(dict(upload_ids=[upload['id']], question='读取原文', mode='facts', **kwargs))


def test_atomic_claims_and_durable_restart(tmp_path):
    store = FleetStore(tmp_path)
    jobs = [seed(store) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda i: FleetStore(tmp_path).claim('node'+str(i)), range(8)))
    assert len({x['id'] for x in claims}) == 8
    assert len({x['attempt_id'] for x in claims}) == 8
    restarted = FleetStore(tmp_path)
    assert restarted.claim('node0')['attempt_id'] == claims[0]['attempt_id']
    assert len(restarted.jobs()) == len(jobs)


def test_expiration_and_stale_result_fencing(tmp_path):
    store = FleetStore(tmp_path, lease_seconds=60)
    job = seed(store)
    a = store.claim('a')
    with store.connection(True) as db:
        db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time()-1, job['id']))
    b = store.claim('b')
    assert b['attempt_id'] != a['attempt_id']
    with pytest.raises(Conflict):
        store.finish(job['id'], 'a', a['attempt_id'], result={'answer':'stale'})
    done = store.finish(job['id'], 'b', b['attempt_id'], result={'answer':'current'}, artifacts={'evidence':['source']})
    assert done['status'] == 'completed'
    assert json.loads(store.archive(job['id']).read_text())['artifacts']['evidence'] == ['source']
    # Duplicate completion cannot overwrite already saved result.
    assert store.finish(job['id'], 'b', b['attempt_id'], result={'answer':'changed'})['result']['answer'] == 'current'


def test_cancellation_and_retry_preserve_history(tmp_path):
    store = FleetStore(tmp_path)
    job = seed(store)
    active = store.claim('a')
    assert store.cancel(job['id'])['status'] == 'cancel_requested'
    assert store.heartbeat(job['id'], 'a', active['attempt_id'])['cancel_requested']
    assert store.finish(job['id'], 'a', active['attempt_id'], result={'answer':'late'})['status'] == 'cancelled'
    retry = store.retry(job['id'])
    assert retry['id'] != job['id'] and retry['retry_of'] == job['id']
    assert store.job(job['id'])['result'] == {'answer':'late'}


def test_job_polling_keeps_results_in_detail_and_archive(tmp_path):
    store = FleetStore(tmp_path)
    job = seed(store)
    active = store.claim('a')
    result = {'answer': '原文回答', 'trace': ['source text'] * 10000}
    store.finish(job['id'], 'a', active['attempt_id'], result=result, artifacts={'evidence': ['source']})
    listing = store.jobs()
    assert listing[0]['result'] is None
    assert listing[0]['archive_url'] and listing[0]['status'] == 'completed'
    assert len(json.dumps(listing)) < 5000
    assert store.job(job['id'])['result'] == result
    assert json.loads(store.archive(job['id']).read_text())['result'] == result


def test_preferred_node_and_missing_upload(tmp_path):
    store = FleetStore(tmp_path)
    job = seed(store, preferred_node='b')
    assert store.claim('a') is None
    assert store.claim('b')['id'] == job['id']
    with pytest.raises(KeyError):
        store.create_job({'upload_ids':['missing'], 'question':'q'})


def test_auth_upload_and_worker_contract(tmp_path):
    token = 'test-only-token-'+'x'*32
    settings = FleetSettings(tmp_path, token, password_hash=password_hash('password'))
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/fleet/jobs').status_code == 401
        assert client.post('/api/fleet/login', json={'password':'wrong'}).status_code == 401
        assert client.post('/api/fleet/login', json={'password':'password'}, headers={'origin':'http://evil.example'}).status_code == 403
        assert client.post('/api/fleet/login', json={'password':'password'}).status_code == 200
        upload = client.post('/api/fleet/uploads', files={'file':('..\\sample.dxf',b'dxf','application/octet-stream')}).json()
        assert upload['filename'] == 'sample.dxf'
        job = client.post('/api/fleet/jobs', json={'upload_ids':[upload['id']], 'template_id':'tables'}).json()
        assert client.post('/api/fleet/worker/claim', json={'node_id':'a'}).status_code == 401
        headers={'Authorization':'Bearer '+token}
        active = client.post('/api/fleet/worker/claim', json={'node_id':'a','model_profile':'api_sol'}, headers=headers).json()['job']
        assert active['uploads'][0]['url'].endswith('/file')
        response=client.post('/api/fleet/worker/complete', json={'node_id':'a','job_id':job['id'],'attempt_id':active['attempt_id'],'result':{'answer':'source'},'artifacts':{'evidence':[]}},headers=headers)
        assert response.status_code == 200, response.text
        assert client.get('/api/fleet/jobs/'+job['id']+'/archive').json()['result']['answer']=='source'
        assert client.get('/api/fleet/nodes').json()['items'][0]['model_profile']=='api_sol'
        client.post('/api/fleet/logout')
        assert client.get('/api/fleet/jobs').status_code==401
        assert client.get(upload['url'], headers=headers).content == b'dxf'


def test_failed_run_keeps_evidence_archive(tmp_path):
    store = FleetStore(tmp_path)
    job = seed(store)
    active = store.claim('a')
    failed = store.finish(job['id'], 'a', active['attempt_id'], error={'code':'MODEL_FAILED'}, artifacts={'evidence_records':[{'id':'read-before-failure'}]})
    assert failed['status'] == 'failed' and failed['archive_url']
    assert json.loads(store.archive(job['id']).read_text())['artifacts']['evidence_records'][0]['id'] == 'read-before-failure'


def test_expired_cancel_and_retry_limit(tmp_path):
    store = FleetStore(tmp_path, max_attempts=2)
    job = seed(store)
    for attempt in range(2):
        claimed = store.claim('a')
        with store.connection(True) as db:
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?',(time.time()-1,job['id']))
        assert store.job(job['id'])['status'] == ('queued' if attempt == 0 else 'failed')
    assert store.claim('a') is None
    other=seed(store)
    active=store.claim('b')
    store.cancel(other['id'])
    with store.connection(True) as db:
        db.execute('UPDATE jobs SET lease_until=? WHERE id=?',(time.time()-1,other['id']))
    assert store.job(other['id'])['status']=='cancelled'
    with pytest.raises(Conflict):
        store.heartbeat(other['id'],'b',active['attempt_id'])


def test_retryable_failure_is_idempotent_and_attempt_archives_survive(tmp_path):
    store=FleetStore(tmp_path)
    job=seed(store)
    first=store.claim('a')
    failed=store.finish(job['id'],'a',first['attempt_id'],error={'code':'TEMPORARY'},retryable=True,artifacts={'evidence':['original']})
    assert failed['status']=='queued' and failed['delivery_status']=='failed'
    archive_before=store.archive(job['id']).read_bytes()
    # Lost HTTP acknowledgement must not replace archived evidence or promote failed work.
    duplicate=store.finish(job['id'],'a',first['attempt_id'],result={'answer':'unexpected'},delivery_status='completed')
    assert duplicate['status']=='queued' and store.archive(job['id']).read_bytes()==archive_before
    second=store.claim('b')
    assert second['attempt_id']!=first['attempt_id']
    done=store.finish(job['id'],'b',second['attempt_id'],result={'answer':'verified'},artifacts={'evidence':['new']},delivery_status='partial')
    assert done['status']=='completed' and done['delivery_status']=='partial'
    attempts=store.attempts(job['id'])
    assert len(attempts)==2 and all(a['archive_url'] for a in attempts)
    assert store.archive(job['id'],first['attempt_id']).read_bytes()==archive_before
    with pytest.raises(KeyError):
        store.archive('different-job',first['attempt_id'])


def test_late_cancelled_result_never_reports_complete_delivery(tmp_path):
    store=FleetStore(tmp_path)
    job=seed(store)
    active=store.claim('a')
    store.cancel(job['id'])
    result=store.finish(job['id'],'a',active['attempt_id'],result={'answer':'completed during cancellation'},delivery_status='completed')
    assert result['status']=='cancelled' and result['delivery_status']=='cancelled'
    archive=json.loads(store.archive(job['id']).read_text())
    assert archive['execution_status']=='cancelled' and archive['delivery_status']=='cancelled'


def test_agent_input_limits_rejected_before_enqueue(tmp_path,monkeypatch):
    from cad_agent.fleet.app import TEMPLATES
    with TestClient(create_app(FleetSettings(tmp_path,'secret-'+'x'*32,password='pw'))) as client:
        client.post('/api/fleet/login',json={'password':'pw'})
        assert client.post('/api/fleet/jobs',json={'upload_ids':['x'],'question':'q'*3001}).status_code==422
        assert client.post('/api/fleet/jobs',json={'upload_ids':['x']*10,'question':'q'}).status_code==422
        monkeypatch.setitem(TEMPLATES[0],'question','q'*3001)
        assert client.post('/api/fleet/jobs',json={'upload_ids':['x'],'template_id':TEMPLATES[0]['id']}).status_code==400


def test_browser_answer_view_preserves_facts_and_full_default(tmp_path):
    token='test-only-token-'+'x'*32
    app=create_app(FleetSettings(tmp_path,token,password='password'))
    store=app.state.fleet_store;job=seed(store);active=store.claim('a')
    result={'answer':'逐字原文50年','citations':[{'quote':'逐字原文50年','bbox':[1,2,3,4]}], 'claims':[{'text':'逐字原文50年'}], 'requirement_outcomes':[{'status':'fulfilled'}], 'missing_information':[], 'trace':['large trace']*10000,'task':{'evidence':'large duplicate ledger'}}
    store.finish(job['id'],'a',active['attempt_id'],result=result)
    with TestClient(app) as c:
        c.post('/api/fleet/login',json={'password':'password'})
        full=c.get('/api/fleet/jobs/'+job['id']).json()
        view=c.get('/api/fleet/jobs/'+job['id']+'?view=answer').json()
        assert full['result']==result and store.job(job['id'])['result']==result
        assert all(view['result'][k]==result[k] for k in ('answer','citations','claims','requirement_outcomes','missing_information'))
        assert 'trace' not in view['result'] and 'task' not in view['result']
        assert c.get('/api/fleet/jobs/'+job['id']+'?view=bad').status_code==400
