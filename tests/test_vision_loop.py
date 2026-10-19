import base64
import hashlib
import json
import os
import struct

import pytest
from fastapi.testclient import TestClient

from cad_agent.app import create_app
from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.vision import VisionService, RenderRegion, BindVisualObjects
from cad_agent.geometry.contracts import GeometryMeasure
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.runtimes.vision import attach_images, mark_images_sent
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
from test_room_geometry import ingest
from test_siwc import connected_lease, response


def make_geometry_session(workspace, single=False):
    geometry, args, drawing = ingest(workspace, single=single)
    request = ChatRequest(snapshot_id=drawing['id'], question='查看局部图像，绑定两线并计算距离，说明能否认定墙厚。',
                          mode='agent', runtime='responses_subscription', model_profile='siwc_sol', allow_fallback=False)
    session = AgentRunner(workspace, request)
    lid = drawing['layouts'][0]['layout_id']
    ids = [e['entity_id'] for e in geometry.index.data['entities'] if e['kind']=='LINE'][:2]
    crop = RenderRegion(layout_id=lid, bbox=[-500,-500,6500,4500], highlight_entity_ids=ids)
    return session, geometry, args, crop, ids


def claim(oid, text, state='observed', kind='measurement_diagnostic'):
    return {'claims':[dict(text=text, state=state, evidence_basis='tool_observation', observation_ids=[oid], observation_kind=kind)]}


def test_pending_diagnostic_is_observed_but_area_stays_unknown(workspace):
    s,g,args,_,_ = make_geometry_session(workspace, single=True)
    value = json.loads(s.invoke_tool('measure_room', args.model_dump()))
    oid = value['observation_id']
    answer = s.validate_answer(json.dumps(claim(oid, '工具报告边界外侧墙面支持不足，需核查。')))
    assert answer['claims'][0]['state'] == 'observed'
    assert '诊断观测' in answer['answer']
    for kind in ('measurement_result','auto','geometry_observation','measurement_diagnostic'):
        with pytest.raises(ValueError):
            s.validate_answer(json.dumps(claim(oid, '面积24平方米。', 'observed', kind)))
    accepted = s.validate_answer(json.dumps(claim(oid, '候选面积24平方米，净面积无法确认。', 'unknown', 'measurement_result')))
    assert '待确认测量' in accepted['answer']


def test_measured_result_derived_and_diagnostic_contract(workspace):
    s,g,args,_,_ = make_geometry_session(workspace)
    value=json.loads(s.invoke_tool('measure_room', args.model_dump()))
    oid=value['observation_id']
    assert value['claim_contract']['measurement_result'] == 'derived/unknown'
    s.validate_answer(json.dumps(claim(oid,'按候选边界计算24平方米。','derived','measurement_result')))
    with pytest.raises(ValueError):
        s.validate_answer(json.dumps(claim(oid,'按候选边界计算24平方米。','observed','measurement_result')))


def test_crop_png_coordinates_integrity_and_http(workspace):
    s,g,args,crop,ids=make_geometry_session(workspace)
    service=VisionService(workspace,s.request.snapshot_id)
    image=service.render(crop);raw=service.png(image['image_id'])
    assert raw.startswith(b'\x89PNG\r\n\x1a\n')
    assert struct.unpack('>II',raw[16:24])==(image['width'],image['height'])
    assert max(image['width'],image['height'])==1024
    assert hashlib.sha256(raw).hexdigest()==image['image_sha256']
    a,b,c,d,e,f=image['wcs_to_pixel'];ia,ib,ic,id_,ie,iff=image['pixel_to_wcs']
    for x,y in [(1000,2000),crop.bbox[:2]]:
        u,v=a*x+c*y+e,b*x+d*y+f
        assert ia*u+ic*v+ie==pytest.approx(x)
        assert ib*u+id_*v+iff==pytest.approx(y)
    assert set(ids)<=set(x['entity_id'] for x in image['candidates'])
    assert service.render(crop)==image
    with TestClient(create_app(workspace.settings)) as client:
        assert client.get(image['image_url']).content==raw
        assert client.get(image['evidence_url']).json()==image
    service.path(image['image_id']).write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='integrity'):service.png(image['image_id'])


@pytest.mark.parametrize('bbox', [[0,0,0,1],[1,1,0,2],[0,0,float('inf'),1]])
def test_invalid_crop_coordinates(bbox):
    with pytest.raises(ValueError):RenderRegion(layout_id='x',bbox=bbox)


def test_foreign_entities_images_and_unsent_images_rejected(workspace):
    s,g,args,crop,ids=make_geometry_session(workspace)
    service=VisionService(workspace,s.request.snapshot_id)
    with pytest.raises(ValueError):service.render(crop.model_copy(update={'highlight_entity_ids':['foreign']}))
    image=json.loads(s.invoke_tool('render_region',crop.model_dump()))
    binding_args=dict(image_id=image['image_id'],entity_ids=ids,interpretation='相邻线候选')
    assert 'error' in json.loads(s.invoke_tool('bind_visual_objects',binding_args))
    s.sent_image_ids.add(image['image_id'])
    binding=json.loads(s.invoke_tool('bind_visual_objects',binding_args))
    assert binding['identity_status']=='visual_candidate_unverified'
    with pytest.raises(ValueError):service.bind(BindVisualObjects(**(binding_args|{'entity_ids':['foreign']})))
    _,_,other=ingest(workspace,angle=.2)
    with pytest.raises(KeyError):VisionService(workspace,other['id']).get(image['image_id'])
    with pytest.raises(KeyError):VisionService(workspace,other['id']).binding(binding['binding_id'])
    other_id=next(e for e in g.index.by_id if e not in ids)
    with pytest.raises(ValueError):g.measure(GeometryMeasure(operation='length',entity_ids=[other_id],binding_id=binding['binding_id']))
    with pytest.raises(ValueError):s.validate_answer(json.dumps(claim(binding['observation_id'],'确定是墙体。','observed','visual_interpretation')))


def test_final_payload_keeps_images_without_base64_in_logs(workspace):
    s,g,args,crop,ids=make_geometry_session(workspace)
    image=json.loads(s.invoke_tool('render_region',crop.model_dump()))
    entry={};payload={'input':[{'role':'user','content':'final'}]}
    attached=attach_images(s,payload,entry)
    assert len(payload['input'])==1
    part=attached['input'][-1]['content'][-1]
    raw=base64.b64decode(part['image_url'].split(',')[1])
    assert hashlib.sha256(raw).hexdigest()==image['image_sha256']
    assert 'data:image' not in json.dumps(entry)
    assert not s.sent_image_ids
    mark_images_sent(s,entry)
    assert s.sent_image_ids=={image['image_id']}
    assert attach_images(s,payload,{})['input'][-1]==attached['input'][-1]


def test_geometry_reserves_answer_without_text_evidence(workspace):
    s,g,args,_,_=make_geometry_session(workspace)
    s.invoke_tool('measure_room',args.model_dump() | {'region_id':'', 'layout_id':s.drawing['layouts'][0]['layout_id']})
    s.usage.append({'total_tokens':31000})
    assert not s.evidence
    # A room measurement does not fulfill the requested image inspection.
    assert CodexSubscriptionRuntime().stop_reason(s) is None
    assert any(w['operation']=='vision' for w in s.task.pending_work())


@pytest.mark.parametrize('runtime',['responses_subscription','codex'])
def test_image_binding_measurement_loop_no_paid_api(workspace,monkeypatch,runtime):
    if runtime=='codex' and os.environ.get('CAD_TEST_EXTERNAL_HARNESSES')!='1':
        pytest.skip('Real CLI with mocked subscription requires opt-in; never inference')
    s,g,args,crop,ids=make_geometry_session(workspace)
    s.request.runtime=runtime
    connected_lease(workspace,monkeypatch)
    monkeypatch.setattr(workspace.gateway,'chat_turn',lambda *a,**kw:pytest.fail('Paid API prohibited'))
    payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        n=len(payloads)
        assert n<=4
        has_image=any(c.get('type')=='input_image' for m in payload['input'] if isinstance(m.get('content'),list) for c in m['content'])
        if n==2:assert has_image
        if n>2:
            assert not has_image  # Bound evidence persists; no repeated bitmap charge.
            assert s.sent_image_ids and s.visual_bindings
        if n==1:name,arguments='render_region',crop.model_dump()
        elif n==2:name,arguments='bind_visual_objects',dict(image_id=next(iter(s.visual_images)),entity_ids=ids,interpretation='相邻线段候选')
        elif n==3:name,arguments='measure_geometry',dict(operation='distance',entity_ids=ids,binding_id=next(iter(s.visual_bindings)))
        else:
            measurement=next(o for o in s.observations.values() if o['tool']=='measure_geometry')
            visual=next(o for o in s.observations.values() if o['tool']=='bind_visual_objects')
            final=claim(measurement['observation_id'],f"计算距离为{measurement['data']['value']}米，不代表墙厚。",'derived','measurement_result')
            final['claims']+=claim(visual['observation_id'],'图像中的线段为候选边界，身份待确认。','derived','visual_interpretation')['claims']
            r=response([{'type':'message','content':[{'type':'output_text','text':json.dumps(final)}]}])
            self.record_usage(session,r);return r
        r=response([{'type':'function_call','namespace':'cad','name':name,'call_id':f'c{n}','arguments':json.dumps(arguments)}])
        self.record_usage(session,r);return r
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run()
    assert result['vision']['unique_images_sent']==1 and len(result['vision']['binding_ids'])==1
    assert len(result['trace'])==3 and all(t['status']=='completed' for t in result['trace'])
    assert result['efficiency']['answer_repairs']==0
    assert result['runtime_used']==runtime and result['fallback_used'] is False
    assert 'data:image' not in json.dumps(result)
    measure=next(o for o in result['tool_observations'] if o['tool']=='measure_geometry')['data']
    assert measure['visual_binding']['binding_id']==result['vision']['binding_ids'][0]


@pytest.mark.parametrize('tool',['discover_rooms','review_discovered_spaces','review_space_connections'])
def test_discovery_diagnostics_can_report_units_without_promoting_measurement(workspace,tool):
    s,_,_,_,_=make_geometry_session(workspace)
    oid='obs_diagnostic'
    s.observations[oid]=dict(observation_id=oid,tool=tool,data=dict(status='partial',applied_unit='mm',area_m2=24))
    result=s.validate_answer(json.dumps(claim(oid,'工具已按毫米单位处理，几何覆盖仍不完整。')))
    assert result['claims'][0]['state']=='observed'
    with pytest.raises(ValueError):s.validate_answer(json.dumps(claim(oid,'测得24平方米。')))
