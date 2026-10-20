"""B05 scope-to-source geometry checks; unknown units never fabricate metres."""
from copy import deepcopy
import pytest
from cad_agent.analysis.scope_objects import candidates_from_records, InspectScopeObjects
from cad_agent.analysis.scope_workflow import bind_scope_call
from cad_agent.analysis.operations import receipts
from test_remediation_scope import session, invoke
from test_room_batch import room_fixture


def rec(key,path,kind='LWPOLYLINE'):
    return dict(entity_id=key,kind=kind,status='ready',linear=True,paths=[path],
        bbox=[min(x for x,y in path),min(y for x,y in path),max(x for x,y in path),max(y for x,y in path)])


def test_observed_path_not_window_bbox_or_annotation():
    square=rec('real',[[1,1],[4,1],[4,4],[1,4],[1,1]])
    border=rec('border',[[0,0],[10,0],[10,10],[0,10],[0,0]])
    note=rec('dim',square['paths'][0],'DIMENSION')
    rows,issues,excluded=candidates_from_records([square,border,note],[0,0,10,10],{'border'})
    closed=[r for r in rows if r['kind']=='closed_boundary_candidate']
    assert closed and closed[0]['raw_area']==9
    assert closed[0]['entity_ids']==['real'] and excluded==2
    assert all('area_m2' not in r and 'perimeter_m' not in r for r in rows)
    assert all(r['identity_status']=='unconfirmed' for r in rows)


def test_open_path_is_not_closed_by_crop_or_text_box():
    open_path=rec('u',[[0,0],[10,0],[10,10],[0,10]])
    rows,issues,_=candidates_from_records([open_path],[0,0,5,5],set())
    assert not any(r['kind']=='closed_boundary_candidate' for r in rows)
    assert rows[0]['entity_ids']==['u'] and rows[0]['crosses_window']


def test_unknown_units_budget_binding_and_actual_followup(workspace):
    _,_,drawing=room_fixture(workspace,units=0)
    s=session(workspace,drawing)
    resolved=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',names=['试点房间'],task_ids=['q1']))
    assert resolved['next_calls'][0]['tool']=='inspect_scope_objects'
    call=resolved['next_calls'][0]
    result=invoke(s,call['tool'],call['arguments']|{'task_ids':['q1']})
    assert 'error' not in result,result
    assert result['unit']=='unknown' and result['items'] and result['observation_id']
    assert result['measurement_gate']['ready'] is False
    assert all(not r['fulfilled'] for r in receipts('inspect_scope_objects',result))
    assert result['review_calls'][0]['tool']=='read_geometry'
    step=result['review_calls'][0]
    source=invoke(s,step['tool'],step['arguments']|{'task_ids':['q1']})
    assert 'error' not in source,source
    assert source['items'] and source['source_sha256']==drawing['sha256']
    assert s.task.data['subtasks'][0]['calls']==3
    assert result['scope_binding']['task_ids']==['q1']
    assert 'inspect_scope_objects' in s.focus.names()


def test_unlabelled_objects_have_bounded_tiles_and_no_invented_anchor(workspace):
    _,_,drawing=room_fixture(workspace,units=0);s=session(workspace,drawing)
    r=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',names=['不存在对象'],task_ids=['q1']))
    scope=r['selected_scope'];flow=s.task.data['drawing_scopes'][scope['scope_id']]['object_workflow']
    assert not flow['anchors'] and len(flow['windows'])==16
    assert all(w['boundary_role']=='search_window_only' and w['bbox']!=scope['bbox'] for w in flow['windows'])
    call=r['next_calls'][0]
    catalog=invoke(s,call['tool'],call['arguments']|{'task_ids':['q1']})
    assert catalog['has_more'] and catalog['next_offset']==5
    assert catalog['subframe_extent_status']=='unresolved_search_frame_only'
    assert all(sf['bbox'] is None for sf in catalog['subframes'])
    bad=invoke(s,'inspect_scope_objects',dict(scope_id=scope['scope_id'],window_id='invented',task_ids=['q1']))
    assert 'error' in bad and not bad.get('items')


def test_complexity_limit_remains_explicit_without_synthetic_boundary(monkeypatch):
    from cad_agent.analysis import scope_objects
    monkeypatch.setattr(scope_objects,'MAX_SEGMENTS',4)
    records=[rec(str(i),[[0,i],[10,i]],'LINE') for i in range(8)]
    rows,issues,_=candidates_from_records(records,[0,-1,10,10],set())
    assert 'segment_limit_exceeded' in issues
    assert not any(r['kind']=='closed_boundary_candidate' for r in rows)


def test_scope_binding_rejects_cross_owner_and_stale_source(workspace):
    _,_,drawing=room_fixture(workspace,units=0);s=session(workspace,drawing,'测量甲距离。测量乙距离。')
    r=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))
    c=r['next_calls'][0]
    wrong=invoke(s,c['tool'],c['arguments']|{'task_ids':['q2']})
    assert 'error' in wrong and 'observation_id' not in wrong
    s.task.data['drawing_scopes'][r['selected_scope']['scope_id']]['source_sha256']='changed'
    stale=invoke(s,c['tool'],c['arguments']|{'task_ids':['q1']})
    assert 'error' in stale and 'observation_id' not in stale


def test_verified_extent_cache_keeps_source_stamp_and_scope_guards(workspace,monkeypatch):
    from cad_agent.geometry.service import GeometryService
    from cad_agent.geometry.contracts import GeometryRead
    from cad_agent.cache import file_stamp
    _,_,drawing=room_fixture(workspace,units=0);s=session(workspace,drawing)
    r=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',names=['试点房间'],task_ids=['q1']))
    c=r['next_calls'][0];page=invoke(s,c['tool'],c['arguments']|{'task_ids':['q1']})
    scope=s.task.data['drawing_scopes'][r['selected_scope']['scope_id']]
    cache=scope['source_entity_extents']; eid=next(iter(cache['items']))
    assert cache['source_sha256']==drawing['sha256'] and len(cache['items'])<=256
    monkeypatch.setattr(GeometryService,'records',lambda *a:pytest.fail('validated extent should be reused'))
    s.trace.append(dict(task_ids=['q1']))
    assert bind_scope_call(s,'read_geometry',GeometryRead(entity_ids=[eid])).entity_ids==[eid]
    cache['items'][eid]['bbox']=[1e8,1e8,1e9,1e9]
    with pytest.raises(ValueError,match='SCOPE_ENTITY'):
        bind_scope_call(s,'read_geometry',GeometryRead(entity_ids=[eid]))
    path=workspace.store.artifact(drawing['id'],'source.dxf')
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='SCOPE_STALE'):
        bind_scope_call(s,'read_geometry',GeometryRead(entity_ids=[eid]))
    assert 'source_entity_extents' not in scope
    with pytest.raises(ValueError,match='SCOPE_STALE'):
        bind_scope_call(s,'read_geometry',GeometryRead(entity_ids=[eid]))
    rebound=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))
    assert 'error' in rebound and '哈希' in rebound['error']['message']


def test_many_native_labels_keep_model_packets_bounded_and_catalog_pageable(workspace):
    import json
    from test_room_geometry import ingest
    from cad_agent.analysis.tasks import TaskLedger
    def build(doc,pt,line):
        m=doc.modelspace()
        m.add_text('三层平面图',dxfattribs={'insert':(1000,-3000),'height':150})
        for i in range(1000):
            m.add_text('试点房间',dxfattribs={'insert':(1000+(i%50)*5000,7000+(i//50)*5000),'height':20})
    _,_,drawing=ingest(workspace,units=0,build=build)
    frame=dict(frame_id='floor',name='三层平面图',layout_id=drawing['layouts'][0]['layout_id'],bbox=[-5000,-8000,260000,120000],method='closed_border_and_title_candidate')
    drawing['frames']=[frame];workspace.store.put('drawing',drawing['id'],drawing)
    regions=workspace.regions(drawing['id'])
    for r in regions:r.frame_id='floor';r.frame_name='三层平面图'
    workspace.store.artifact(drawing['id'],'regions.json').write_text(json.dumps([r.model_dump() for r in regions]))
    s=session(workspace,drawing)
    resolved=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',names=['试点房间'],task_ids=['q1']))
    assert 'error' not in resolved,resolved
    compact=resolved['selected_scope'];flow=compact['object_workflow']
    assert flow['anchor_count']>=1000 and 'windows' not in flow and 'anchors' not in flow
    assert resolved['has_more'] and resolved['total']>=1000
    for value in (resolved,s.task.context(),s.task.public(),s.focus.context()):
        assert len(json.dumps(value,ensure_ascii=False))<50000
    stored=TaskLedger.read(workspace,s.task.data['task_id'])['drawing_scopes'][compact['scope_id']]['object_workflow']
    assert len(stored['windows'])>=1016
    for offset in (0,5,10,995):
        page=invoke(s,'inspect_scope_objects',dict(scope_id=compact['scope_id'],offset=offset,limit=5,task_ids=['q1']))
        assert 'error' not in page,page
        assert len(page['items'])==5 and page['next_offset']==offset+5
        assert len(json.dumps(page,ensure_ascii=False))<50000
        assert all(not r['fulfilled'] for r in page['operation_receipts'])
    assert s.policy.stop_reason is None and s.policy.stalled==0 and s.policy.geometry_work
