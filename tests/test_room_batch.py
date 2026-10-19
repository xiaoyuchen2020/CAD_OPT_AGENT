"""Room workflow acceptance without model/network calls."""
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient
from cad_agent.analysis.agent import AgentRunner
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.room_batch import RoomBatchQuery, RoomBatchService, UnitConfirmation
from cad_agent.geometry.service import GeometryService
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from test_room_geometry import ingest
from test_siwc import connected_lease, response


def room_fixture(workspace, units=0):
    def add(doc, pt, line):
        doc.modelspace().add_text('三层平面图',dxfattribs={'insert':pt((1,-3)),'height':150})
        doc.modelspace().add_text('试点房间/走廊做法说明',dxfattribs={'insert':pt((1,-6)),'height':150})
    _, _, drawing = ingest(workspace, units=units, build=add)
    frame=dict(frame_id='floor', name='三层平面图',layout_id=drawing['layouts'][0]['layout_id'],
               bbox=[-5000,-8000,15000,15000],method='closed_border_and_title_candidate')
    drawing['frames']=[frame]
    workspace.store.put('drawing',drawing['id'],drawing)
    regions=workspace.regions(drawing['id'])
    for r in regions:r.frame_id='floor';r.frame_name='三层平面图'
    workspace.store.artifact(drawing['id'],'regions.json').write_text(json.dumps([r.model_dump() for r in regions]))
    return RoomBatchService(workspace,drawing['id']),RoomBatchQuery(floor_title='三层平面图',room_label='试点房间'),drawing


def test_unknown_units_keep_scope_all_labels_and_evidence_without_geometry(workspace,monkeypatch):
    s,q,d=room_fixture(workspace)
    monkeypatch.setattr(GeometryService,'room',lambda *a:pytest.fail('unknown unit must not measure'))
    r=s.run(q)
    assert r['status']=='needs_unit_confirmation' and r['total']==1
    assert r['coverage']['enumeration']=='all_matching_labels'
    assert r['scope_evidence'][0]['quote']=='三层平面图'
    i=r['items'][0]
    assert i['area_m2'] is None and i['highlight']['kind']=='label_locator'
    assert i['label_evidence']['quote']=='试点房间'
    assert i['label_evidence']['snapshot_id']==d['id']
    assert s.run(q)==r
    with pytest.raises(ValueError):RoomBatchQuery(**q.model_dump(),unit_override='mm')


def test_unit_confirmation_measures_locally_retains_immutable_original(workspace):
    s,q,d=room_fixture(workspace)
    before=s.run(q)
    after=s.confirm(before['batch_id'],UnitConfirmation(unit='mm',confirmed=True))
    assert after['unit_basis']=='user_confirmed' and after['parent_batch_id']==before['batch_id']
    i=after['items'][0]
    assert i['status']=='measured',i['diagnostics']
    assert i['area_m2']==pytest.approx(24,abs=.015)
    assert i['highlight']['kind']=='candidate_boundary'
    assert s.get(before['batch_id'])==before
    assert s.drawing['unit']=='unknown'
    assert s.confirm(before['batch_id'],UnitConfirmation(unit='mm',confirmed=True))==after
    assert s.page(after['batch_id'],0)['unit']=='mm'
    with pytest.raises(ValueError):UnitConfirmation(unit='mm',confirmed=False)


def test_frame_ambiguity_nonplan_and_fallback_do_not_guess(workspace):
    s,q,d=room_fixture(workspace)
    for method,name in [('fallback','三层平面图'),('closed_border_and_title_candidate','三层剖面图')]:
        copy=dict(d);copy['frames']=[dict(d['frames'][0],method=method,name=name)]
        workspace.store.put('drawing',d['id'],copy)
        r=RoomBatchService(workspace,d['id']).run(q.model_copy(update={'floor_title':name}))
        assert not r['items'] and r['status']=='needs_scope_confirmation'
    d['frames'].append(dict(d['frames'][0],frame_id='floor2'))
    workspace.store.put('drawing',d['id'],d)
    # Clear cached fallback record to exercise a distinct frame-filter request.
    r=RoomBatchService(workspace,d['id']).run(q.model_copy(update={'layout_id':d['layouts'][0]['layout_id']}))
    assert len(r['scope_choices'])==2 and r['scope']['frame_id']=='floor'
    assert r['scope_choices'][1]['eligible'] is False


def test_batch_time_limit_and_per_item_failures_preserve_rows(workspace,monkeypatch):
    s,q,_=room_fixture(workspace,units=4)
    def bad(*args):raise ValueError('invalid anchor')
    monkeypatch.setattr(GeometryService,'room',bad)
    r=s.run(q)
    assert r['items'][0]['status']=='unresolved' and r['items'][0]['highlight']['kind']=='label_locator'


def test_api_confirmation_and_cross_snapshot(workspace,tmp_path):
    s,q,d=room_fixture(workspace)
    app=create_app(Settings(data_dir=workspace.settings.data_dir))
    with TestClient(app) as c:
        r=c.post(f"/api/drawings/{d['id']}/rooms/batch",json=q.model_dump())
        assert r.status_code==200
        path=r.json()['evidence_url']
        assert c.post(path+'/confirm-unit',json={'unit':'mm'}).status_code==422
        assert c.post(path+'/confirm-unit',json={'unit':'mm','confirmed':True}).json()['items'][0]['area_m2']==24
        assert c.get(path.replace(d['id'],'not_this_drawing')).status_code==404


def test_preflight_keeps_geometry_and_unit_inspection_runnable(workspace,monkeypatch):
    from test_workflow_contract import complete_room_operations
    _,q,d=room_fixture(workspace)
    runner=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='三层平面图有哪些试点房间？面积分别是多少？并高亮。',
        mode='agent',runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False))
    runner.build_messages()
    assert runner.calls==1 and runner.trace[0]['tool']=='analyze_room_batch'
    assert not runner.policy.stop_reason and runner.task.pending_work()
    complete_room_operations(runner)
    assert runner.room_batch['unit_resolution']['status']=='needs_input'
    assert runner.room_batch['items'][0]['highlight']['kind']=='candidate_boundary'
    with pytest.raises(ValueError,match='单位尚未确认'):
        runner.finish(json.dumps({'claims':[{'text':'候选面积24平方米','state':'unknown'}]}))


def test_routing_uses_source_vocabulary_and_does_not_intercept_other_questions(workspace):
    s,q,d=room_fixture(workspace)
    assert s.infer_request('三层平面图哪些试点房间？面积分别是多少？')==q
    for question in ['三层平面图标题是什么','试点房间面积多少','三层剖面图哪些试点房间面积是多少','三层平面图有哪些试点房间，面积分别是多少，以及门窗数量？']:
        assert s.infer_request(question) is None


def test_time_budget_returns_every_candidate_and_resume_offset(workspace,monkeypatch):
    s,q,_=room_fixture(workspace,units=4)
    ticks=iter([0,21])
    monkeypatch.setattr('cad_agent.geometry.room_batch.time.monotonic',lambda:next(ticks))
    monkeypatch.setattr(GeometryService,'room',lambda *a:pytest.fail('budget exhausted'))
    r=s.run(q)
    assert len(r['items'])==1 and r['items'][0]['status']=='not_measured'
    assert r['coverage']['measurement']=='partial_time_budget' and r['resume_offset']==0


@pytest.fixture
def real_batch(workspace):
    source=Path(__file__).resolve().parents[1]/'artifacts/hybrid-retrieval-20261015/data'
    if not (source/'index.sqlite3').exists():pytest.skip('local real DXF artifact unavailable')
    sid='snap_8cb104e99dfcfcd2a6b4fd9f'
    with sqlite3.connect(f'file:{source}/index.sqlite3?mode=ro',uri=True) as c:
        row=c.execute('select data from records where id=?',(sid,)).fetchone()
    if not row:pytest.skip('real snapshot unavailable')
    d=json.loads(row[0]);workspace.store.put('drawing',sid,d)
    for name in ('regions.json','scene.json'):
        target=workspace.store.artifact(sid,name);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source/'drawings'/sid/name,target)
    return RoomBatchService(workspace,sid)


def test_real_floor_bedroom_enumeration_and_pagination_offline(real_batch):
    q=real_batch.infer_request('一层平面图中有哪些卧室？卧室房间面积分别是多少？并帮我高亮出来。')
    assert q and q.room_label=='卧室'
    all_=real_batch.run(q)
    assert all_['total']==6 and len(all_['items'])==6 and not all_['has_more']
    assert all_['scope']['frame_id']=='frame_8352c68029a1232918e0eba2'
    assert all_['status']=='needs_unit_confirmation'
    assert {i['label_evidence']['quote'] for i in all_['items']}=={'卧室'}
    assert len({i['region_id'] for i in all_['items']})==6
    assert all(i['highlight']['kind']=='label_locator' and i['area_m2'] is None for i in all_['items'])
    first=real_batch.run(q.model_copy(update={'limit':4}))
    second=real_batch.page(first['batch_id'],first['next_offset'])
    assert first['has_more'] and not second['has_more']
    assert [i['region_id'] for i in first['items']+second['items']]==[i['region_id'] for i in all_['items']]
    assert second['coverage']['enumeration']=='page'


def duplicate_title(workspace, sid, *, text=None):
    regions=workspace.regions(sid)
    title=next(r for r in regions if r.text=='三层平面图')
    regions.append(title.model_copy(update={'region_id':title.region_id+'_duplicate',
        'text':text or title.text,'entity_ids':['duplicate_title_entity']}))
    workspace.store.artifact(sid,'regions.json').write_text(json.dumps([r.model_dump() for r in regions]))


def test_duplicate_same_title_keeps_candidates_highlights_and_unit_gate(workspace,monkeypatch):
    s,q,d=room_fixture(workspace)
    duplicate_title(workspace,d['id'])
    monkeypatch.setattr(GeometryService,'room',lambda *a:pytest.fail('unknown units must not measure'))
    r=RoomBatchService(workspace,d['id']).run(q)
    assert r['scope_diagnostics']['candidates'][0]['matching_title_count']==2
    assert len(r['scope_evidence'])==2 and r['total']==1
    assert r['status']=='needs_unit_confirmation'
    assert r['items'][0]['area_m2'] is None
    assert r['items'][0]['highlight']['kind']=='label_locator'


def test_different_plan_titles_remain_blocked_with_actionable_diagnostic(workspace):
    from cad_agent.geometry.room_batch import ScopeConfirmation
    s,q,d=room_fixture(workspace)
    duplicate_title(workspace,d['id'],text='四层平面图')
    s=RoomBatchService(workspace,d['id']);r=s.run(q)
    assert r['status']=='needs_scope_confirmation'
    assert r['scope_choices'][0]['eligible'] is False
    assert r['scope_diagnostics']['candidates'][0]['reasons']==['conflicting_plan_titles']
    assert s.summary(r)['total'] is None
    assert r['coverage']['enumeration']=='not_started'
    with pytest.raises(ValueError,match='不能通过重复确认'):
        s.confirm_scope(r['batch_id'],ScopeConfirmation(frame_id='floor',confirmed=True))


def test_scope_confirmation_persisted_idempotent_and_cannot_bypass_boundary(workspace):
    from cad_agent.geometry.room_batch import ScopeConfirmation
    s,q,d=room_fixture(workspace)
    d['frames'].append(dict(d['frames'][0],frame_id='floor2'))
    workspace.store.put('drawing',d['id'],d)
    rows=workspace.regions(d['id']);title=next(r for r in rows if r.text=='三层平面图')
    rows.append(title.model_copy(update={'region_id':'second_title','entity_ids':['second_title_entity'],'frame_id':'floor2'}))
    workspace.store.artifact(d['id'],'regions.json').write_text(json.dumps([r.model_dump() for r in rows]))
    s=RoomBatchService(workspace,d['id']);old=s.run(q)
    new=s.confirm_scope(old['batch_id'],ScopeConfirmation(frame_id='floor',confirmed=True))
    assert new['status']=='needs_unit_confirmation' and new['total']==1
    assert new['scope_confirmation']=={'frame_id':'floor','confirmed':True}
    assert new['parent_batch_id']==old['batch_id']
    assert s.confirm_scope(old['batch_id'],ScopeConfirmation(frame_id='floor',confirmed=True))==new
    assert s.get(old['batch_id'])['scope'] is None
    assert s.page(new['batch_id'],0)['scope_confirmation']==new['scope_confirmation']
    measured=s.confirm(new['batch_id'],UnitConfirmation(unit='mm',confirmed=True))
    assert measured['scope_confirmation']==new['scope_confirmation']
    assert measured['items'][0]['area_m2']==24
    with pytest.raises(ValueError):s.confirm_scope(old['batch_id'],ScopeConfirmation(frame_id='foreign',confirmed=True))
    assert s.confirm_scope(old['batch_id'],ScopeConfirmation(frame_id='floor2',confirmed=True))['scope']['frame_id']=='floor2'


def test_legacy_duplicate_scope_can_be_confirmed_through_http(workspace):
    s,q,d=room_fixture(workspace);duplicate_title(workspace,d['id'])
    r=s.run(q);r.update(batch_id='rooms_legacy',status='needs_scope_confirmation',scope=None,items=[],total=0)
    r.pop('scope_diagnostics');workspace.store.put('room_batch',r['batch_id'],r)
    with TestClient(create_app(Settings(data_dir=workspace.settings.data_dir))) as c:
        url=f"/api/drawings/{d['id']}/room-batches/rooms_legacy/confirm-scope"
        assert c.post(url,json={'frame_id':'floor'}).status_code==422
        assert c.post(url,json={'frame_id':'floor','confirmed':False}).status_code==422
        new=c.post(url,json={'frame_id':'floor','confirmed':True}).json()
        assert new['status']=='needs_unit_confirmation' and len(new['scope_evidence'])==2
        assert new['parent_batch_id']=='rooms_legacy'


def test_real_tenth_floor_repeated_titles_twelve_labels_offline(workspace,monkeypatch):
    source=Path(__file__).resolve().parents[1]/'artifacts/hybrid-retrieval-20261015/data'
    sid='snap_aea4896cab02f61ff67d4b6c'
    if not (source/'index.sqlite3').exists():pytest.skip('local real artifact unavailable')
    with sqlite3.connect(f'file:{source}/index.sqlite3?mode=ro',uri=True) as c:
        row=c.execute('select data from records where id=?',(sid,)).fetchone()
    if not row:pytest.skip('local tenth-floor snapshot unavailable')
    workspace.store.put('drawing',sid,json.loads(row[0]))
    for name in ('regions.json','scene.json'):
        shutil.copy2(source/'drawings'/sid/name,workspace.store.artifact(sid,name))
    monkeypatch.setattr(GeometryService,'room',lambda *a:pytest.fail('unknown unit must not measure'))
    s=RoomBatchService(workspace,sid)
    r=s.run(RoomBatchQuery(floor_title='十层平面图',room_label='卧室'))
    assert r['status']=='needs_unit_confirmation' and r['total']==12 and len(r['items'])==12
    assert len(r['scope_evidence'])==2 and not r['has_more']
    assert r['scope']['frame_id']=='frame_4edadc66fc0efd006c9bae54'
    assert r['scope_diagnostics']['candidates'][0]['matching_title_count']==2
    assert len({i['region_id'] for i in r['items']})==12
    assert all(i['area_m2'] is None and i['highlight']['kind']=='label_locator' and i['label_evidence']['quote']=='卧室' for i in r['items'])


def test_single_label_table_heuristic_retained_but_prose_and_merged_cells_excluded(workspace):
    s,q,d=room_fixture(workspace)
    rows=workspace.regions(d['id']);label=next(r for r in rows if r.text=='试点房间')
    label.table_row_key='possible_wall_band';label.kind='table_row'
    rows.extend([label.model_copy(update={'region_id':'merged','entity_ids':['one','two']}),
                 label.model_copy(update={'region_id':'prose','text':'试点房间的面积应核查'})])
    workspace.store.artifact(d['id'],'regions.json').write_text(json.dumps([r.model_dump() for r in rows]))
    result=RoomBatchService(workspace,d['id']).run(q)
    assert result['total']==1
    assert result['items'][0]['source_classification']=='parser_table_row_candidate'
    assert result['items'][0]['identity_status']=='label_candidate'
    assert result['items'][0]['area_m2'] is None
