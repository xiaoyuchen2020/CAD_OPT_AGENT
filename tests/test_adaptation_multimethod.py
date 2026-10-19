import copy
import json
import math
import time
from pathlib import Path
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession, tool_definitions
from cad_agent.extensions.adaptation.shadow import ShadowService, Policy, Target, target_key
from cad_agent.extensions.adaptation.dispatch import observe_read, _matches
from cad_agent.extensions.adaptation.contracts import Proposal, Start
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.validation import validate
from cad_agent.extensions.table_recovery.bundle import prepare_region, digest
from test_table_recovery import recovery_case


def region_target(case,method,**kwargs):
    b=case[3]
    return Target(snapshot_id=case[0]['id'],method=method,layout_id=b['layout_id'],bbox=b['bbox'],**kwargs)


def candidate(method,b):
    if method=='text_spans':
        t=b['texts'][0];return dict(input_id=b['input_id'],items=[dict(entity_id=t['entity_id'],start=0,end=len(t['text']))])
    if method=='segment_measurements':
        s=b['segments'][0];return dict(input_id=b['input_id'],items=[dict(segment_id=s['segment_id'],length=math.dist(s['a'],s['b']))])
    if method=='segment_distances':
        from shapely.geometry import LineString
        a,b2=b['segments'][:2];value=LineString([a['a'],a['b']]).distance(LineString([b2['a'],b2['b']]))
        return dict(input_id=b['input_id'],items=[dict(segment_ids=[a['segment_id'],b2['segment_id']],distance=value)])
    # The fixture has native whole edges for the outside rectangle, plus inner grid lines.
    x,y,z,w=b['bbox'];edges=[s['segment_id'] for s in b['segments'] if
        (s['a'][0]==s['b'][0] and s['a'][0] in (x,z)) or (s['a'][1]==s['b'][1] and s['a'][1] in (y,w))]
    return dict(input_id=b['input_id'],segment_ids=edges,area=(z-x)*(w-y))


@pytest.mark.parametrize('method',['text_spans','segment_measurements','segment_distances','closed_contour'])
def test_non_table_trigger_repair_and_result_isolation(workspace,recovery_case,monkeypatch,method):
    t=region_target(recovery_case,method,trigger='authorized_read');calls=[];outputs=[]
    def model(settings,family,instruction,bundle,feedback,**kw):
        calls.append(copy.deepcopy(feedback));outputs.append({} if len(calls)==1 else candidate(family,bundle))
        return Proposal(strategy='code',explanation='fixture',code='def adapt(bundle):\n return {}'),dict(usage={},reserved_tokens=100)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',model)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output=outputs.pop(0)))
    p=ShadowService(workspace).create(Policy(idempotency_key=method,targets=[t],mode='dynamic',instruction='test',
        max_runs=1,max_model_calls=3,max_reserved_tokens=96000,agent_shadow_enabled=True))
    tool='query_entities' if method=='text_spans' else 'query_spatial'
    q=dict(layout_id=t.layout_id,bbox=t.bbox,limit=5)
    request=dict(snapshot_id=t.snapshot_id,question='读取局部',agent_policy='legacy')
    normal=CadAgentSession(workspace,ChatRequest(**request));shadow=CadAgentSession(workspace,ChatRequest(**request,adaptation_policy_id=p['policy_id']))
    a=normal.invoke_tool(tool,json.dumps(q));b=shadow.invoke_tool(tool,json.dumps(q))
    assert a==b and normal.evidence==shadow.evidence and normal.observations==shadow.observations
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        runs=workspace.store.list('adaptation_run')
        if runs and runs[0]['status']=='awaiting_review':break
        time.sleep(.01)
    else:raise AssertionError(runs)
    assert len(calls)==2 and calls[1][0]['errors'] and runs[0]['request']['method']==method
    assert runs[0]['output_mode']=='shadow_only' and not runs[0]['automatic_adoption']
    assert 'code' not in a and 'adaptation' not in a
    # Repeated reads and dispatch do not spend a second model budget.
    events=observe_read(workspace,p['policy_id'],t.snapshot_id,tool,q)
    assert events[0]['run_id']==runs[0]['run_id'] and len(calls)==2


def test_multiple_methods_share_scope_without_identity_collision(workspace,recovery_case,monkeypatch):
    targets=[region_target(recovery_case,m,trigger='authorized_read') for m in ('segment_measurements','segment_distances','closed_contour')]
    outputs=[]
    def model(settings,method,instruction,b,feedback,**kw):
        outputs.append(candidate(method,b))
        return Proposal(strategy='code',explanation='fixture',code='def adapt(bundle):\n return {}'),dict(usage={},reserved_tokens=100)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.propose',model)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output=outputs.pop(0)))
    p=ShadowService(workspace).create(Policy(idempotency_key='multi',targets=targets,mode='dynamic',instruction='test',
        max_runs=3,max_model_calls=9,max_reserved_tokens=288000,agent_shadow_enabled=True))
    t=targets[0];events=observe_read(workspace,p['policy_id'],t.snapshot_id,'query_spatial',dict(layout_id=t.layout_id,bbox=t.bbox))
    assert len(events)==3 and all(e['status']=='queued' for e in events)
    assert len({e['run_id'] for e in events})==3
    assert all(r['status']=='awaiting_review' for r in workspace.store.list('adaptation_run'))


def test_region_scope_and_methods_cannot_be_forged(workspace,recovery_case):
    for extra in ({'method':'arbitrary_python'},{'bbox':[0,0,float('nan'),2]},{'bbox':[5,0,1,1]},
                  {'table_id':recovery_case[3]['table_id']},{'trigger_tools':['extract_facts']}):
        args=region_target(recovery_case,'segment_measurements').model_dump()|extra
        with pytest.raises(ValueError):Target(**args)
    t=region_target(recovery_case,'segment_measurements');idx=recovery_case[1]
    assert not _matches(idx,t.request(),'query_spatial',dict(layout_id=t.layout_id,bbox=[-1,-1,121,46]))
    assert not _matches(idx,t.request(),'measure_geometry',dict(operation='area',entity_ids=['foreign']))
    assert not _matches(idx,t.request(),'read_geometry',dict(entity_ids=['foreign']))
    assert not _matches(idx,t.request(),'read_source_context',dict(region_id='foreign'))
    p=ShadowService(workspace).create(Policy(idempotency_key='scope',targets=[t],mode='dynamic',instruction='test',max_model_calls=3,max_reserved_tokens=96000))
    with pytest.raises(ValueError,match='SCOPE_DENIED'):
        ShadowService(workspace).trial(p['policy_id'],t.snapshot_id,target_id='foreign')
    # Region not_applicable baseline is never interpreted as a detected component failure.
    q=Policy(idempotency_key='no-gap',targets=[t],mode='dynamic',instruction='test',max_model_calls=3,max_reserved_tokens=96000,agent_shadow_enabled=True)
    p=ShadowService(workspace).create(q)
    assert ShadowService(workspace).observe(p['policy_id'],t.snapshot_id,target_id=p['targets'][0]['target_id'])['reason']=='NO_HOST_CONFIRMED_GAP'


@pytest.mark.parametrize('attack',['wrong','fake','duplicate','same','unit','nan','bool','outside'])
def test_distance_validator_rejects_forged_measurements(recovery_case,attack):
    b=recovery_case[3];raw=candidate('segment_distances',b);item=raw['items'][0]
    assert validate('segment_distances',b,raw)['validation_status']=='passed'
    if attack=='wrong':item['distance']+=1
    elif attack=='fake':item['segment_ids'][0]='invented'
    elif attack=='duplicate':raw['items'].append(dict(item,segment_ids=list(reversed(item['segment_ids']))))
    elif attack=='same':item['segment_ids'][1]=item['segment_ids'][0]
    elif attack=='unit':item['unit']='m'
    elif attack=='nan':item['distance']=float('nan')
    elif attack=='bool':item['distance']=False
    elif attack=='outside':raw['input_id']='foreign'
    v=validate('segment_distances',b,raw);assert v['validation_status']=='rejected' and not v['items']


def test_v1_policy_request_idempotency_is_unchanged(workspace,recovery_case):
    t=Target(snapshot_id=recovery_case[0]['id'],table_id=recovery_case[3]['table_id'])
    args=Policy(idempotency_key='v1',targets=[t],mode='dynamic',instruction='test',max_model_calls=3,max_reserved_tokens=96000)
    old=args.model_dump();old['targets']=[dict(snapshot_id=t.snapshot_id,table_id=t.table_id,diagnostic_run_id=None)]
    s=ShadowService(workspace);p=s.create(args);assert p['request_sha256']==digest(old)
    # Emulate a sealed policy written before the scope expansion.
    p['targets'][0].pop('target_id');p['seal']=digest({k:v for k,v in p.items() if k!='seal'})
    workspace.store.put('adaptation_policy',p['policy_id'],p)
    assert s.create(args)==p


def test_multi_method_http_capability_and_scope(workspace,recovery_case,monkeypatch):
    sid=recovery_case[0]['id'];prefix=f'/api/drawings/{sid}/adaptations'
    t=region_target(recovery_case,'text_spans');args=Policy(idempotency_key='http',targets=[t],mode='dynamic',instruction='test',max_model_calls=3,max_reserved_tokens=96000)
    with TestClient(create_app(workspace.settings)) as client:
        caps=client.get(prefix+'/capabilities').json();assert len(caps)==5
        assert caps['closed_contour']['automatic_adoption'] is False
        assert caps['text_spans']['scopes']==['table','explicit_region']
        p=client.post(prefix+'/policies',json=args.model_dump()).json()
        path=prefix+'/policies/'+p['policy_id']+'/trials'
        assert client.post(path,json={'target_id':p['targets'][0]['target_id'],'code':'x'}).status_code==422
        assert client.post(path,json={'target_id':'foreign'}).status_code==400
        assert client.post(path,json={'table_id':'x','target_id':'y'}).status_code==422


@pytest.mark.parametrize('points,expected',[
    (((0,0),(4,4),(0,4),(4,0)),0),
    (((0,0),(4,0),(2,0),(6,0)),0),
    (((0,0),(4,0),(7,0),(9,0)),3),
    (((0,0),(4,0),(0,3),(4,3)),3),
    (((0,0),(2,0),(3,1),(3,2)),2**.5),
])
def test_distance_independent_numeric_cases(recovery_case,points,expected):
    from test_table_recovery import reseal
    b=copy.deepcopy(recovery_case[3]);b['segments']=copy.deepcopy(b['segments'][:2])
    for s,p,q in zip(b['segments'],points[::2],points[1::2]):s['a']=list(p);s['b']=list(q)
    reseal(b)
    raw=dict(input_id=b['input_id'],items=[dict(segment_ids=[s['segment_id'] for s in b['segments']],distance=expected)])
    assert validate('segment_distances',b,raw)['validation_status']=='passed'


def test_non_table_diagnostic_from_wrong_method_is_refused(workspace,recovery_case,monkeypatch):
    b=recovery_case[3];sid=recovery_case[0]['id']
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output={}))
    old=AdaptationService(workspace,sid).start(Start(table_id=b['table_id'],mode='fixed',idempotency_key='old-grid'),background=False)
    target=Target(snapshot_id=sid,table_id=b['table_id'],method='text_spans',diagnostic_run_id=old['run_id'])
    p=ShadowService(workspace).create(Policy(idempotency_key='wrong-method',mode='dynamic',targets=[target],instruction='test',max_model_calls=3,max_reserved_tokens=96000,agent_shadow_enabled=True))
    with pytest.raises(ValueError,match='DIAGNOSTIC_SOURCE_OR_METHOD_MISMATCH'):
        ShadowService(workspace).observe(p['policy_id'],sid,target_id=p['targets'][0]['target_id'])


def test_replay_cannot_mix_method_origin(workspace,recovery_case,monkeypatch):
    from cad_agent.extensions.table_recovery.worker import recover
    b=recovery_case[3];sid=recovery_case[0]['id'];service=AdaptationService(workspace,sid)
    monkeypatch.setattr('cad_agent.extensions.adaptation.service.execute',lambda *a,**kw:dict(output=recover(b)))
    old=service.start(Start(table_id=b['table_id'],mode='fixed',idempotency_key='grid'),background=False)
    with pytest.raises(ValueError,match='REPLAY_METHOD_MISMATCH'):
        ShadowService(workspace).create(Policy(idempotency_key='mix',mode='replay',reuse_run_id=old['run_id'],
            targets=[region_target(recovery_case,'closed_contour')],instruction='test'))


def test_large_coordinate_review_keeps_source_and_overlay_visible(workspace,tmp_path):
    import io
    import re
    import xml.etree.ElementTree as ET
    import resvg_py
    from PIL import Image
    from conftest import dxf_bytes
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.extensions.adaptation.review import source_svg
    pts=[(1000000,2000000),(1000006.4,2000004.8),(1000002.8,2000009.6),(999996.4,2000004.8)]
    def build(doc):
        m=doc.modelspace()
        for a,b in zip(pts,pts[1:]+pts[:1]):m.add_line(a,b)
        m.add_text('SOURCE B',dxfattribs={'insert':(1000000,2000004),'height':.4})
    d=workspace.ingest('large-review.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    bundle=prepare_region(idx,d['layouts'][0]['layout_id'],[999995,1999999,1000008,2000011])
    raw=dict(input_id=bundle['input_id'],segment_ids=[s['segment_id'] for s in bundle['segments']],area=48)
    report=validate('closed_contour',bundle,raw);assert report['validation_status']=='passed'
    source_before=workspace.store.artifact(d['id'],'source.dxf').read_bytes();before=digest(bundle)
    class ReviewFixture:
        w=workspace;sid=d['id']
        def get(self,_):return dict(input_artifact_id='input',accepted_attempt=1,status='awaiting_review')
        def artifact(self,*_):return bundle
        def result(self,_):return dict(validation=report)
        def source_check(self,b):assert digest(b)==before
        def immutable(self,*_):pass
    svg=source_svg(ReviewFixture(),'run',True,local_origin=True)
    root=ET.fromstring(svg);ns={'s':'http://www.w3.org/2000/svg'}
    view=list(map(float,root.attrib['viewBox'].split()));assert max(map(abs,view))<100
    points=root.find('s:polygon',ns).attrib['points'];assert max(abs(float(n)) for n in re.split('[ ,]',points))<10
    # resvg does not implement non-scaling-stroke; scale to two screen pixels.
    scale=max(view[2]/600,view[3]/600)
    svg=re.sub(r'stroke-width="([0-9.]+)"',lambda m:f'stroke-width="{float(m[1])*scale}"',svg).replace('vector-effect="non-scaling-stroke"','')
    im=Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_string=svg,width=600))).convert('RGB')
    assert sum(r>180 and 50<g<180 and b<70 for r,g,b in im.getdata())>300
    assert sum(max(rgb)<80 for rgb in im.getdata())>50  # source text still visible
    assert workspace.store.artifact(d['id'],'source.dxf').read_bytes()==source_before and digest(bundle)==before
