import copy
import json
from pathlib import Path
from threading import Event
import pytest
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.extensions.adaptation.contracts import Start, Review, Promote, Proposal
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.validation import validate, derive_grid
from cad_agent.extensions.table_recovery.worker import recover
from cad_agent.extensions.table_recovery.bundle import digest
from test_table_recovery import recovery_case, reseal


def test_source_coordinate_missing_from_input_is_rejected(recovery_case):
    b=copy.deepcopy(recovery_case[3]); b['ys']=[b['ys'][0],b['ys'][-1]];reseal(b)
    result=validate('table_grid',b,recover(b))
    assert result['validation_status']=='rejected'
    assert 'CELL_ERASES_NATIVE_SEPARATOR' in result['errors']


def test_derived_axes_require_native_support_and_cannot_change_scope(recovery_case):
    b=recovery_case[3]; raw=copy.deepcopy(recover(b));raw['xs'][1]+=3
    with pytest.raises(ValueError,match='ANCHOR_WITHOUT_NATIVE_SOURCE'):derive_grid(b,raw)
    raw=copy.deepcopy(recover(b));raw['xs'][0]-=1
    with pytest.raises(ValueError,match='INVALID_GRID_AXES'):derive_grid(b,raw)


def test_other_family_validators_bind_source_and_refuse_forgery(recovery_case):
    b=recovery_case[3];t=b['texts'][0];s=b['segments'][0]
    raw=dict(input_id=b['input_id'],items=[dict(entity_id=t['entity_id'],start=0,end=1)])
    v=validate('text_spans',b,raw);assert v['validation_status']=='passed' and v['items'][0]['quote']==t['text'][0]
    raw['items'][0]['quote']='forged';assert validate('text_spans',b,raw)['validation_status']=='rejected'
    raw=dict(input_id=b['input_id'],items=[dict(segment_id=s['segment_id'],length=999)])
    assert validate('segment_measurements',b,raw)['errors']==['MEASUREMENT_MISMATCH']
    raw=dict(input_id=b['input_id'],segment_ids=[s['segment_id']]*3,area=1)
    assert validate('closed_contour',b,raw)['validation_status']=='rejected'


def test_bounded_repair_idempotence_review_and_promotion_gate(workspace,recovery_case,monkeypatch):
    b=recovery_case[3];sid=recovery_case[0]['id'];calls=[]
    def proposal(*a,**kw):
        calls.append(1)
        return Proposal(strategy='code',explanation='test',code='def adapt(bundle):\n return {}'),dict(usage={'total_tokens':10},reserved_tokens=100)
    outputs=[dict(input_id=b['input_id'],status='candidate',xs=b['xs'],ys=b['ys'],cells=[]),recover(b)]
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',proposal)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output=outputs.pop(0)))
    service=AdaptationService(workspace,sid)
    args=Start(table_id=b['table_id'],mode='dynamic',idempotency_key='repair')
    run=service.start(args,background=False)
    assert run['status']=='awaiting_review' and len(calls)==2
    assert run['attempts'][0]['validation_status']=='rejected'
    assert service.start(args)==run and len(calls)==2
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        service.start(args.model_copy(update={'instruction':'changed'}))
    with pytest.raises(ValueError,match='STALE'):
        service.review(run['run_id'],Review(candidate_sha256='stale',decision='accepted',scope='table_identity_and_grid',note='test'))
    reviewed=service.review(run['run_id'],Review(candidate_sha256=run['candidate_sha256'],decision='accepted',scope='table_identity_and_grid',note='test fixture only'))
    assert reviewed['status']=='accepted' and not reviewed['semantic_acceptance']
    with pytest.raises(ValueError,match='CROSS_DRAWING'):
        service.promote(Promote(run_ids=[run['run_id']]*2,name='not enough'))


def test_retry_cap_and_cancel(workspace,recovery_case,monkeypatch):
    b=recovery_case[3];calls=[]
    def proposal(*a,**kw):
        calls.append(1)
        return Proposal(strategy='code',explanation='test',code='def adapt(bundle):\n return {}'),dict(usage={},reserved_tokens=100)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',proposal)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output={}))
    service=AdaptationService(workspace,recovery_case[0]['id'])
    r=service.start(Start(table_id=b['table_id'],mode='dynamic',idempotency_key='cap'),background=False)
    assert r['status']=='unresolved' and len(calls)==3
    # Cancellation before any worker/model step must not invoke either.
    monkeypatch.setattr(workspace.executor,'submit',lambda *a:None)
    r=service.start(Start(table_id=b['table_id'],idempotency_key='cancel'))
    service.cancel(r['run_id']);service.work(r['run_id'],workspace._adaptation_jobs[r['run_id']])
    assert service.get(r['run_id'])['status']=='cancelled' and len(calls)==3


def test_api_excludes_raw_code_and_exposes_registered_specs(workspace,recovery_case):
    sid=recovery_case[0]['id']
    with TestClient(create_app(workspace.settings)) as client:
        prefix=f'/api/drawings/{sid}/adaptations'
        assert set(client.get(prefix+'/methods').json()['specs'])=={'table_grid','text_spans','segment_measurements','segment_distances','closed_contour'}
        r=client.post(prefix+'/runs',json={'table_id':'x','idempotency_key':'x','code':'print(1)'})
        assert r.status_code==422


def test_real_accepted_tables_and_real_negative_control():
    folder=Path(__file__).resolve().parents[1]/'artifacts/table-recovery-gap-audit-20261016'
    if not folder.exists():pytest.skip('local real drawing evidence not distributed')
    records=json.loads((folder/'anchor-experiment.json').read_text())
    results={}
    for record in records:
        if 'execution' not in record:continue
        b=json.loads((folder/'experimental-inputs'/(record['experimental_input_id']+'.json')).read_text())
        v=validate('table_grid',b,record['execution']['output']);results[record['case_id']]=v
    assert len(results['11816-27']['cells'])==15
    assert len(results['11816-28']['cells'])==15
    assert results['11816-36']['validation_status']=='rejected'


def test_region_input_is_source_bound_without_table_identity(recovery_case):
    from cad_agent.extensions.table_recovery.bundle import prepare_region, verify_bundle
    d,index,table,b=recovery_case
    scoped=prepare_region(index,b['layout_id'],b['bbox'])
    verify_bundle(scoped)
    assert scoped['coverage']['scope']=='explicit_native_region'
    assert scoped['source_sha256']==b['source_sha256'] and scoped['texts']==b['texts']
    with pytest.raises(ValueError,match='LAYOUT_NOT_IN_SNAPSHOT'):
        prepare_region(index,'foreign',b['bbox'])
    with pytest.raises(ValueError):
        Start(method='table_grid',layout_id=b['layout_id'],bbox=b['bbox'],idempotency_key='x')


def test_valid_native_contour_and_measurement_recomputed_independently(recovery_case):
    from shapely.geometry import LineString
    b=recovery_case[3];a,y,c,d=b['bbox'];ids=[]
    for s in b['segments']:
        p,q=s['a'],s['b']
        if (p[0]==q[0] and p[0] in (a,c)) or (p[1]==q[1] and p[1] in (y,d)):
            ids.append(s['segment_id'])
    raw=dict(input_id=b['input_id'],segment_ids=ids,area=(c-a)*(d-y))
    assert validate('closed_contour',b,raw)['validation_status']=='passed'
    raw['area']*=1.01
    assert validate('closed_contour',b,raw)['errors']==['AREA_MISMATCH']
    s=b['segments'][0]
    raw=dict(input_id=b['input_id'],items=[dict(segment_id=s['segment_id'],length=LineString([s['a'],s['b']]).length)])
    v=validate('segment_measurements',b,raw)
    assert v['validation_status']=='passed' and v['items'][0]['unit']=='drawing_unit'


def test_source_change_and_artifact_change_block_review(workspace,recovery_case,monkeypatch):
    b=recovery_case[3];sid=recovery_case[0]['id']
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output=recover(b)))
    service=AdaptationService(workspace,sid)
    r=service.start(Start(table_id=b['table_id'],mode='fixed',idempotency_key='integrity'),background=False)
    entry=r['attempts'][0];record=workspace.store.get('adaptation_code',entry['code_artifact_id'])
    original=copy.deepcopy(record);record['code']='changed';workspace.store.put('adaptation_code',entry['code_artifact_id'],record)
    with pytest.raises(ValueError,match='ARTIFACT_INTEGRITY_FAILED'):service.result(r['run_id'])
    workspace.store.put('adaptation_code',entry['code_artifact_id'],original)
    source=workspace.store.artifact(sid,'source.dxf');source.write_bytes(source.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='SOURCE_INTEGRITY_FAILED'):service.result(r['run_id'])


def test_replay_register_disable_lifecycle_uses_same_code(workspace,recovery_case,monkeypatch):
    import io
    import ezdxf
    from cad_agent.analysis.general import DrawingIndex
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda data,bundle,code,**kw:dict(output=recover(bundle)))
    first=AdaptationService(workspace,recovery_case[0]['id'])
    r1=first.start(Start(table_id=recovery_case[3]['table_id'],mode='fixed',idempotency_key='lifecycle-one'),background=False)
    doc=ezdxf.read(io.StringIO(workspace.store.artifact(first.sid,'source.dxf').read_text()))
    doc.header['$PROJECTNAME']='separate synthetic lifecycle fixture'
    text=io.StringIO();doc.write(text)
    drawing=workspace.ingest('holdout-fixture.dxf',text.getvalue().encode())
    second=AdaptationService(workspace,drawing['id']);table=DrawingIndex(workspace,drawing['id']).tables[0]
    r2=second.start(Start(table_id=table['table_id'],mode='replay',reuse_run_id=r1['run_id'],idempotency_key='lifecycle-two'),background=False)
    assert r2['status']=='awaiting_review' and r2['model_calls']==0
    for s,r in ((first,r1),(second,r2)):
        s.review(r['run_id'],Review(candidate_sha256=r['candidate_sha256'],decision='accepted',scope='table_identity_and_grid',note='synthetic unit test only'))
    version=first.promote(Promote(name='test-only',run_ids=[r1['run_id'],r2['run_id']]))
    assert version['status']=='registered' and version['cross_project_generalization'] is False
    first.disable(version['method_version_id'])
    r=first.start(Start(table_id=recovery_case[3]['table_id'],mode='registered',method_version_id=version['method_version_id'],idempotency_key='disabled'),background=False)
    assert r['error']=='METHOD_NOT_REGISTERED_OR_DISABLED' and r['model_calls']==0


def test_invalid_code_feedback_repairs_without_host_execution(workspace,recovery_case,monkeypatch):
    proposals=iter(['def adapt(:', 'def adapt(bundle):\n return {}'])
    feedback=[]
    def proposal(*a,**kw):
        feedback.append(a[4][:])
        return Proposal(strategy='code',explanation='test',code=next(proposals)),dict(usage={},reserved_tokens=100)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',proposal)
    calls=[]
    def execution(data,bundle,code,**kw):
        calls.append(code);return dict(output=recover(bundle))
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',execution)
    s=AdaptationService(workspace,recovery_case[0]['id'])
    r=s.start(Start(table_id=recovery_case[3]['table_id'],mode='dynamic',idempotency_key='syntax'),background=False)
    assert r['status']=='awaiting_review' and len(calls)==1
    assert feedback[1][0]['errors']==['INVALID_CODE_INTERFACE_OR_SYNTAX']


def test_http_full_job_review_flow(workspace,recovery_case,monkeypatch):
    import time
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda data,bundle,code,**kw:dict(output=recover(bundle)))
    sid=recovery_case[0]['id'];table_id=recovery_case[3]['table_id']
    with TestClient(create_app(workspace.settings)) as client:
        prefix=f'/api/drawings/{sid}/adaptations/runs'
        response=client.post(prefix,json=dict(table_id=table_id,mode='fixed',idempotency_key='http'))
        assert response.status_code==202
        url=prefix+'/'+response.json()['run_id']
        for _ in range(50):
            run=client.get(url).json()
            if run['status'] not in ('queued','running'):break
            time.sleep(.01)
        assert run['status']=='awaiting_review'
        assert client.get(url+'/result').json()['validation']['validation_status']=='passed'
        for suffix in ('source.svg','overlay.svg'):
            r=client.get(url+'/'+suffix);assert r.status_code==200 and '<svg' in r.text
        assert client.get(url+'/review').status_code==200
        assert client.get(url+'/review.js').headers['content-type'].startswith('text/javascript')
        r=client.post(url+'/reviews',json=dict(candidate_sha256=run['candidate_sha256'],scope='table_identity_and_grid',decision='accepted',note='synthetic HTTP test'))
        assert r.status_code==200 and r.json()['status']=='accepted'


@pytest.mark.parametrize('failure',['INPUT_LIMIT_EXCEEDED: segments','SOURCE_INTEGRITY_FAILED'])
def test_input_failures_keep_classification_and_never_call_model(workspace,recovery_case,monkeypatch,failure):
    def fail(*a,**kw):raise ValueError(failure)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.prepare',fail)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',lambda *a,**kw:pytest.fail('input failures must not reach model'))
    s=AdaptationService(workspace,recovery_case[0]['id'])
    r=s.start(Start(table_id=recovery_case[3]['table_id'],mode='dynamic',idempotency_key='input-gate'),background=False)
    assert r['error']==failure.split(':')[0] and r['model_calls']==0
