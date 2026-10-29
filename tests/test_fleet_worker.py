from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch
import httpx
import pytest
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.fleet.conversion import validate_bundle, cached_conversion
from cad_agent.fleet.worker import Worker, LostLease
from test_oda_import import OdaServer, config, DWG, JOB


def test_preconverted_import_preserves_provenance_and_rejects_tampering(tmp_path,demo_bytes,monkeypatch):
    monkeypatch.setenv('CAD_FLEET_TOKEN','fixture-fleet-secret')
    server = OdaServer(demo_bytes)
    app = create_app(config(tmp_path))
    def upload(client,dxf=demo_bytes,headers=None):
        return client.post('/api/fleet-import',headers=headers or {},data={'job_id':JOB},files={
            'original':('sample.dwg',DWG),'dxf':('source.dxf',dxf),'oda_json':('oda.json',server.raw)})
    with TestClient(app) as client:
        assert upload(client).status_code == 403
        headers={'Authorization':'Bearer fixture-fleet-secret'}
        assert upload(client,b'broken',headers).status_code == 502
        with patch.object(app.state.workspace.converter,'convert',side_effect=AssertionError('must not reconvert')):
            response=upload(client,headers=headers)
            assert response.status_code == 201,response.text
            record=response.json()
            assert record['source_format']=='DWG'
            assert record['original_sha256']==sha256(DWG).hexdigest()
            assert record['conversion']['json_sha256']==sha256(server.raw).hexdigest()
            assert client.get('/api/drawings/'+record['id']+'/source').content==DWG
            assert client.get('/api/drawings/'+record['id']+'/oda-json').content==server.raw
            assert upload(client,headers=headers).json()['id']==record['id']


def test_cache_reuses_validated_bundle_and_detects_corruption(tmp_path,demo_bytes):
    server=OdaServer(demo_bytes)
    settings=config(tmp_path)
    converted=validate_bundle(settings,'x.dwg',DWG,demo_bytes,server.raw,JOB)
    cache=tmp_path/'cache'
    with patch('cad_agent.fleet.conversion.OdaConverter.convert',return_value=converted) as convert:
        first=cached_conversion(settings,'x.dwg',DWG,cache)
        assert convert.call_count==1
    second=cached_conversion(settings,'x.dwg',DWG,cache)
    assert second.dxf==first.dxf
    bundle=next(p for p in cache.iterdir() if p.is_dir())
    (bundle/'source.dxf').write_bytes(b'corrupt')
    with pytest.raises(Exception,match='完整性'):
        cached_conversion(settings,'x.dwg',DWG,cache)


def make_worker(tmp_path,monkeypatch):
    for key,value in {'CAD_FLEET_NODE_ID':'A','CAD_FLEET_WORKER_DIR':str(tmp_path/'worker'),
        'CAD_FLEET_CACHE_DIR':str(tmp_path/'cache'),'CAD_FLEET_TOKEN':'fixture-secret',
        'CAD_FLEET_URL':'http://center','CAD_FLEET_AGENT_URL':'http://agent',
        'CAD_FLEET_MODEL_PROFILE':'api_astra','CAD_FLEET_RUNTIME':'native'}.items():
        monkeypatch.setenv(key,value)
    return Worker()


def test_submission_uses_explicit_model_no_fallback_and_preserves_run_checkpoint(tmp_path,monkeypatch):
    worker=make_worker(tmp_path,monkeypatch)
    worker.state=dict(node_id='A',job_id='job_fixture',attempt_id='attempt_fixture',
                      drawings=[{'id':'snap_fixture'}],snapshot_id='snap_fixture')
    seen=[]
    def respond(request):
        seen.append(request)
        if request.url.path=='/api/fleet-runs':
            data=json.loads(request.content)
            assert data['model_profile']=='api_astra'
            assert data['runtime']=='native' and data['allow_fallback'] is False
            assert data['source_read_batching'] is False
            assert data['dynamic_methods'] == 'off'
            assert request.headers['x-fleet-execution']=='attempt_fixture'
            return httpx.Response(202,json={'id':'run_fixture'})
        return httpx.Response(200,json={'id':'run_fixture','status':'completed','result':{'answer':'fixture'}})
    worker.agent=httpx.Client(base_url='http://agent',transport=httpx.MockTransport(respond))
    monkeypatch.setattr(worker,'heartbeat',lambda:None)
    monkeypatch.setattr(worker,'archive',lambda run:{'run':run,'task':{'status':'completed'}})
    monkeypatch.setattr(worker,'request',lambda *a,**kw:{})
    job={'uploads':[{'id':'upload'}],'mode':'agent','question':'图纸注明什么？'}
    worker.execute(job)
    assert worker.state['run_id']=='run_fixture'
    worker.execute(job)
    assert sum(r.url.path=='/api/fleet-runs' for r in seen)==1


def test_stale_lease_stops_execution(tmp_path,monkeypatch):
    worker=make_worker(tmp_path,monkeypatch)
    worker.state=dict(node_id='A',job_id='job_fixture',attempt_id='attempt_fixture')
    worker.center=httpx.Client(base_url='http://center',transport=httpx.MockTransport(
        lambda request:httpx.Response(409,json={'error':'stale'})))
    with pytest.raises(LostLease): worker.request('heartbeat')
    with pytest.raises(LostLease): worker.check()


def test_authenticated_run_key_reuses_persisted_submission(tmp_path,monkeypatch):
    from cad_agent.contracts import ChatRequest
    monkeypatch.setenv('CAD_FLEET_TOKEN','fixture-fleet-secret')
    app=create_app(config(tmp_path))
    payload=ChatRequest(snapshot_id='snap_fixture',question='读取原文',mode='agent',
                        runtime='native',model_profile='api_sol').model_dump()
    app.state.workspace.store.put('chat_run','chat_fixture',dict(id='chat_fixture',
        snapshot_id='snap_fixture',status='completed',fleet_execution_key='attempt_fixture',request=payload))
    with TestClient(app) as client:
        headers={'Authorization':'Bearer fixture-fleet-secret','X-Fleet-Execution':'attempt_fixture'}
        assert client.post('/api/fleet-runs',json=payload).status_code==403
        response=client.post('/api/fleet-runs',json=payload,headers=headers)
        assert response.status_code==202,response.text
        assert response.json()['id']=='chat_fixture'
        response=client.post('/api/fleet-runs',json={**payload,'question':'不同问题'},headers=headers)
        assert response.status_code==400


@pytest.mark.parametrize('export_status', [200, 413, 404])
def test_archive_materializes_generated_code_or_records_gap(tmp_path, monkeypatch, export_status):
    worker = make_worker(tmp_path, monkeypatch)
    worker.state = dict(attempt_id='attempt_fixture', job={'exploration_run_id': 'exp_fixture'})
    bundle = dict(schema_version='generated-artifacts@1', run_id='run_fixture',
                  task_id='task_fixture', complete=True, items=[], diagnostics=[])
    def respond(request):
        if request.url.path.endswith('/generated-artifacts'):
            assert request.headers['x-fleet-execution'] == 'attempt_fixture'
            return httpx.Response(export_status, json=bundle if export_status == 200 else {'detail': 'unavailable'})
        if request.url.path.endswith('/evidence-records'):
            return httpx.Response(200, json={'items': [], 'has_more': False})
        if request.url.path.endswith('/journal-export'):
            return httpx.Response(200, text='{"event":"completed"}\n')
        return httpx.Response(200, json={'task_id': 'task_fixture'})
    worker.agent = httpx.Client(base_url='http://agent', transport=httpx.MockTransport(respond))
    archive = worker.archive({'id': 'run_fixture', 'task_id': 'task_fixture'})
    assert archive['journal'] == [{'event': 'completed'}]
    assert archive['generated_artifacts']['complete'] is (export_status == 200)
    if export_status != 200:
        assert archive['generated_artifacts']['diagnostics'][0]['http_status'] == export_status


def test_model_snapshot_detects_configuration_drift_without_credentials(tmp_path, monkeypatch):
    from cad_agent.fleet.snapshots import model_snapshot, check_model_snapshot
    monkeypatch.setenv('CAD_GPT_API_KEY', 'never-return-this-fixture-key')
    monkeypatch.setenv('CAD_GPT_API_BASE_URL', 'https://fixture.invalid/v1')
    frozen = model_snapshot('api_sol')
    check_model_snapshot(frozen)
    assert 'fixture.invalid' not in json.dumps(frozen)
    assert 'never-return' not in json.dumps(frozen)
    monkeypatch.setenv('CAD_GPT_API_BASE_URL', 'https://changed.invalid/v1')
    with pytest.raises(ValueError, match='MODEL_VERSION_MISMATCH'):
        check_model_snapshot(frozen)
