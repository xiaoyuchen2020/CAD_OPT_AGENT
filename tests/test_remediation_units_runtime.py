"""Q17 source replay and transport/long-tool budget boundary tests; no network."""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from copy import deepcopy

import httpx
import pytest
from shapely.geometry import box

from cad_agent.adapters.model import ModelError
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.text_units import read_unit_candidates
from cad_agent.contracts import SourceRegion
from cad_agent.geometry.room_batch import RoomBatchService
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
from cad_agent.runtimes.token_budget import charged_tokens
from test_siwc import make_session, connected_lease, response, sse

ROOT = Path(__file__).resolve().parents[1]
Q17 = ROOT/'artifacts/designer-facts-latest-20261019/case-data/Q17-A'
SID = 'snap_e316a1345c7a5db25e19458f'
RID = 'region_8a3aaca06e66314cf7e4907c'


@pytest.mark.parametrize('metadata',[{}, {'entity_ids':[]}, {'entity_ids':None}])
def test_missing_native_entity_binding_keeps_unit_unresolved(metadata):
    target=SimpleNamespace(text='面积30m', **metadata)
    # No scene should be read when the annotation has no source entity IDs.
    assert read_unit_candidates(SimpleNamespace(), target)==[]


def q17_index():
    if not Q17.exists():
        pytest.skip('Immutable real Q17 audit fixture is not installed')
    class ReadOnlyStore:
        def get(self, kind, key):
            with sqlite3.connect(f'file:{Q17}/index.sqlite3?mode=ro',uri=True) as connection:
                return json.loads(connection.execute('SELECT data FROM records WHERE kind=? AND id=?',(kind,key)).fetchone()[0])
        def artifact(self,sid,name):
            return Q17/'drawings'/sid/name
    store=ReadOnlyStore()
    workspace=SimpleNamespace(store=store,lock=threading.RLock(),regions=lambda sid:[SourceRegion.model_validate(r)
        for r in json.loads(store.artifact(sid,'regions.json').read_text())])
    return DrawingIndex(workspace,SID)


def test_real_q17_source_relation_keeps_both_original_quotes_and_no_area_equivalence():
    index=q17_index()
    paths=[index.w.store.artifact(SID,name) for name in ('source.dxf','scene.json','regions.json','entities.json')]
    hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    candidates=read_unit_candidates(index,index.by_id[RID])
    assert len(candidates)==1
    candidate=candidates[0]
    assert candidate['normalized_text_candidate']=='面积154.05m²'
    assert candidate['normalized_unit_candidate']=='m2'
    assert candidate['state']=='candidate' and not candidate['ambiguous']
    assert {e['quote'] for e in candidate['source_evidence']}=={'面积154.05m','2'}
    assert len(candidate['source_entity_ids'])==2
    assert candidate['measurement_comparability']=='unconfirmed'
    assert candidate['relation_checks']['height_ratio']==.5
    assert candidate['relation_checks']['owner']=='matched_native_dxf'
    assert candidate['unit_resolution']=='source_supported_reading'
    assert len(candidate['native_relation_evidence'])==2
    assert index.by_id[RID].text=='面积154.05m'
    svc=RoomBatchService.__new__(RoomBatchService);svc.index=index
    target=index.by_id[RID]
    item={'candidate_area_m2':136.8504,'area_basis':'selected_wall_enclosure','diagnostics':[]}
    svc.check_area_annotations({'scope':{'layout_id':target.layout_id,'frame_id':target.frame_id}},item,
        box(target.bbox[0]-500,target.bbox[1]-500,target.bbox[2]+500,target.bbox[3]+500))
    check=item['area_annotation_checks'][0]
    assert check['status']=='source_supported_compound_unit' and check['unit']=='m2'
    assert check['numeric_difference']==-17.1996
    assert check['basis_comparability']=='unconfirmed' and check['semantic_difference'] is None
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]==hashes


def test_real_q17_compound_reading_is_available_through_public_context_and_assembly(workspace):
    from cad_agent.analysis.evidence_ledger import source_context
    from cad_agent.analysis.fact_tools import read_assembly, ReadAssembly
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from scripts.test_source_record_workflow import seed
    index=q17_index()
    context=source_context(index.w,SID,RID,neighbors=0)
    # Public tools require an authorized session and a writable derived cache;
    # keep the frozen source store read-only and exercise a temporary copy.
    seed(Q17,workspace,SID)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=SID,question='读取面积单位原文。'))
    assembly=read_assembly(session,ReadAssembly(region_id=RID))
    for result in (context,assembly):
        assert result['unit_candidates'][0]['normalized_unit_candidate']=='m2'
        assert {e['quote'] for e in result['unit_candidates'][0]['source_evidence']}=={'面积154.05m','2'}


@pytest.mark.parametrize('change',[{'layer':'other'},{'rotation':90},{'height':350},{'owner':'different'}, {'origin':[112542,-10398]}])
def test_superscript_rejects_incompatible_relationships(change):
    index=q17_index();scene=deepcopy(index.scene)
    texts=[t for t in scene['texts'] if t['entity_id'] in {'ent_dda5a42476c6c5ad90aa016b','ent_23cfddd0d31fc55511d19197'}]
    anchor=next(t for t in texts if t['text']!='2');exponent=next(t for t in texts if t['text']=='2')
    anchor['owner']=exponent['owner']='same';exponent.update(change)
    proxy=SimpleNamespace(snapshot=SID,scene={'texts':texts},entity_evidence=index.entity_evidence)
    assert not read_unit_candidates(proxy,index.by_id[RID])


def transport_session(workspace,demo_bytes,monkeypatch,handler):
    session=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    session.runtime_diagnostics={'model_requests':[{}],'automatic_retries':0}
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handler)))
    return session,ResponsesTransport(workspace.siwc_account)


def test_explicit_503_retries_once_retains_each_attempt_and_unknown_usage_debt(workspace,demo_bytes,monkeypatch):
    calls=[]
    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(503,json={'error':{'code':'server_error'}}) if len(calls)==1 else httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse(response([])))
    session,transport=transport_session(workspace,demo_bytes,monkeypatch,handler)
    result=transport.request(session,{'model':'gpt-6.1-sol','input':[]})
    assert result['status']=='completed' and len(calls)==2
    assert calls[0]==calls[1] and calls[0]['max_output_tokens']==8192
    attempts=session.runtime_diagnostics['model_requests'][0]['transport_attempts']
    assert [a['http_status'] for a in attempts]==[503,200]
    assert attempts[0]['unknown_usage'] and attempts[0]['reserved_tokens']>0
    assert attempts[0]['retry_scheduled'] and not attempts[1]['unknown_usage']
    assert sum(u['total_tokens'] for u in session.usage)==120
    assert charged_tokens(session)==120+attempts[0]['reserved_tokens']


@pytest.mark.parametrize('status,body',[(401,{'error':{'code':'server_error'}}),(403,{}),(429,{}),
    (503,{'error':{'code':'insufficient_quota'}}),(503,{'error':{'code':'server_error'},'output':[]}),
    (503,{'error':{'code':'server_error'},'status':'in_progress'}),(503,None)])
def test_auth_quota_ambiguous_and_partial_responses_never_retry(workspace,demo_bytes,monkeypatch,status,body):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(status,json=body) if body is not None else httpx.Response(status,text='service unavailable')
    session,transport=transport_session(workspace,demo_bytes,monkeypatch,handler)
    with pytest.raises(ModelError):transport.request(session,{'model':'gpt-6.1-sol'})
    assert len(calls)==1 and not session.usage
    assert session.runtime_diagnostics['unknown_usage']


def test_partial_sse_and_timeout_are_not_retried(workspace,demo_bytes,monkeypatch):
    for partial in (True,False):
        calls=[]
        def handler(req):
            calls.append(req)
            if not partial:raise httpx.ReadTimeout('fixture')
            return httpx.Response(200,headers={'content-type':'text/event-stream'},text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
        session,transport=transport_session(workspace,demo_bytes,monkeypatch,handler)
        with pytest.raises(ModelError):transport.request(session,{'model':'gpt-6.1-sol'})
        assert len(calls)==1 and session.runtime_diagnostics['unknown_usage']


def test_503_retry_is_bounded_and_cannot_extend_time_or_token_budget(workspace,demo_bytes,monkeypatch):
    calls=[]
    session,transport=transport_session(workspace,demo_bytes,monkeypatch,
        lambda req:calls.append(req) or httpx.Response(503,json={'error':{'code':'server_error'}}))
    with pytest.raises(ModelError):transport.request(session,{'model':'gpt-6.1-sol'})
    assert len(calls)==2 and session.runtime_diagnostics['automatic_retries']==1
    calls.clear();session.runtime_diagnostics={}
    monkeypatch.setattr(session,'_remaining',lambda:10)
    with pytest.raises(ModelError):transport.request(session,{'model':'gpt-6.1-sol'})
    assert len(calls)==1
    calls.clear();monkeypatch.setattr(session,'_remaining',lambda:100)
    session.runtime_diagnostics['reserved_unknown_usage_tokens']=CodexSubscriptionRuntime().total_budget(session)
    with pytest.raises(ModelError,match='预算'):transport.request(session,{'model':'gpt-6.1-sol'})
    assert not calls


@pytest.mark.parametrize('visual',[True,False])
def test_valid_first_image_or_long_history_request_is_not_blocked_by_byte_estimate(workspace,demo_bytes,monkeypatch,visual):
    calls=[]
    session,transport=transport_session(workspace,demo_bytes,monkeypatch,
        lambda req:calls.append(req) or httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse(response([]))))
    # A 97KB PNG's base64 transport is larger than the entire token budget.
    content=([{'type':'input_image','image_url':'data:image/png;base64,'+'A'*129336}]
             if visual else '历史'*45000)
    session.usage.append({'total_tokens':80000})
    result=transport.request(session,{'model':'gpt-6.1-sol','input':[{'role':'user','content':content}]})
    assert result['status']=='completed' and len(calls)==1
    assert session.runtime_diagnostics.get('reserved_unknown_usage_tokens',0)==0


def test_unknown_image_usage_does_not_repeat_a_visual_request(workspace,demo_bytes,monkeypatch):
    calls=[]
    session,transport=transport_session(workspace,demo_bytes,monkeypatch,
        lambda req:calls.append(req) or httpx.Response(503,json={'error':{'code':'server_error'}}))
    with pytest.raises(ModelError):
        transport.request(session,{'model':'gpt-6.1-sol','input':[{'content':[{'type':'input_image','image_url':'data:image/png;base64,AAAA'}]}]})
    assert len(calls)==1
    assert charged_tokens(session)==CodexSubscriptionRuntime().total_budget(session)


def test_local_auth_failure_before_post_never_reserves_unknown_usage(workspace,demo_bytes,monkeypatch):
    session,transport=transport_session(workspace,demo_bytes,monkeypatch,lambda req:pytest.fail('No POST expected'))
    def failed_auth():raise ModelError('SIWC_AUTH_EXPIRED','local token unavailable')
    monkeypatch.setattr(workspace.siwc_account,'access_token',failed_auth)
    with pytest.raises(ModelError):transport.request(session,{'model':'gpt-6.1-sol'})
    attempt=session.runtime_diagnostics['model_requests'][0]['transport_attempts'][0]
    assert not attempt['request_started'] and not attempt['unknown_usage']
    assert charged_tokens(session)==0


def test_boundary_preparation_reuses_unreviewed_cache_and_checks_time(workspace,monkeypatch):
    from test_room_boundary import prepare
    from cad_agent.geometry.room_boundary import PrepareBoundaries
    import cad_agent.geometry.room_boundary as module
    service,batch,review,_,_=prepare(workspace,monkeypatch)
    args=PrepareBoundaries(batch_id=batch['batch_id'])
    monkeypatch.setattr(module,'scoped_geometry_index',lambda *a:pytest.fail('Cached preparation must not recompute geometry'))
    cached=service.prepare(args,remaining=lambda:10)
    assert cached['review_id']==review['review_id'] and cached['status']=='awaiting_visual_selection'
    assert not any('model_choice' in row for row in cached['items'])
    # A source artifact change must miss the preparation cache before preflight.
    path=workspace.store.artifact(service.sid,'scene.json');path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='剩余时间不足'):service.prepare(args,remaining=lambda:10)
    with pytest.raises(RuntimeError,match='cancelled'):
        service.prepare(args,remaining=lambda:(_ for _ in ()).throw(RuntimeError('cancelled')))


def test_geometry_deadline_is_request_local_and_caps_worker_timeout():
    from cad_agent.geometry.compute import compute, remaining_budget
    calls=[]
    executor=SimpleNamespace(timeout=120,compute=lambda op,payload,timeout=None:calls.append(timeout) or {'ok':True})
    workspace=SimpleNamespace(geometry_executor=executor)
    with remaining_budget(lambda:30):
        assert compute(workspace,'test',{})=={'ok':True}
        with remaining_budget(lambda:10):compute(workspace,'test',{})
        compute(workspace,'test',{})
    compute(workspace,'test',{})
    assert calls==[25,5,25,None] and executor.timeout==120
    with remaining_budget(lambda:4),pytest.raises(ValueError,match='剩余时间不足'):
        compute(workspace,'test',{})
    assert len(calls)==4
