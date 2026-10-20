"""Source identity promotion and negative controls, without a model."""
from copy import deepcopy
import pytest
from cad_agent.analysis.spatial_evidence import (BindSpatialObject, SpatialObjectRead,
    bind_spatial_object, get_spatial_object, resolve_object_input, record_measurement)
from cad_agent.analysis.object_registry import record_tool_result
from test_remediation_scope import session, invoke
from test_room_batch import room_fixture
from test_object_registry import session as pure_session, page


def setup_object(workspace):
    _, _, drawing = room_fixture(workspace, units=4)
    s = session(workspace, drawing)
    resolved = invoke(s, 'resolve_drawing_scope', dict(floor_title='三层平面图', names=['试点房间'], task_ids=['q1']))
    s.trace.append(dict(task_ids=['q1']))
    scope = s.task.data['drawing_scopes'][resolved['selected_scope']['scope_id']]
    return s, scope


def geometry_record(key='shape', layout='l', path=None):
    path = path or [[0,0],[10,0],[10,10],[0,10],[0,0]]
    return dict(entity_id=key, entity_path=[key], layout_id=layout, kind='LWPOLYLINE', status='ready', linear=True,
        paths=[path], bbox=[min(p[0] for p in path),min(p[1] for p in path),max(p[0] for p in path),max(p[1] for p in path)])


def test_cross_scope_same_anchor_is_not_one_object():
    s=pure_session()
    a=page(0,['same'],1)
    b=deepcopy(a); b['layout_id']='other'
    record_tool_result(s,'discover_rooms',{},a)
    record_tool_result(s,'discover_rooms',{},b)
    assert len(s.task.data['object_registry']['objects']) == 2


def test_source_binding_promotes_exact_label_relation_not_engineering(workspace, monkeypatch):
    from cad_agent.geometry.service import GeometryService
    s, scope=setup_object(workspace)
    label=next(r for r in s.index.by_id.values() if r.text=='试点房间')
    point=next(t['origin'][:2] for t in s.index.scene['texts'] if t['entity_id'] in label.entity_ids)
    x,y=point
    rec=geometry_record(layout=scope['layout_id'],path=[[x-10,y-10],[x+10,y-10],[x+10,y+10],[x-10,y+10],[x-10,y-10]])
    monkeypatch.setattr(GeometryService,'records',lambda *a:[rec])
    result=bind_spatial_object(s,BindSpatialObject(scope_id=scope['scope_id'],entity_ids=['shape'], label_region_ids=[label.region_id],role='room_boundary'))
    assert result['identity']['status']=='label_containment_verified'
    assert not result['identity']['engineering_confirmed']
    assert result['coverage']['net_boundary']=='unconfirmed'
    assert result['coverage']['object_extent']=='unconfirmed'
    assert result['role']['status']=='asserted'
    record_measurement(s,result['object_id'],dict(source_sha256=s.drawing['sha256'],raw_area=400,basis='selected_closed_path'))
    assert get_spatial_object(s,SpatialObjectRead(object_id=result['object_id']))['measurement_evidence'][0]['raw_area']==400
    # Same text outside selected path remains source-bound, never proximity-confirmed.
    far=deepcopy(rec);far['paths']=[[[0,0],[1,0],[1,1],[0,1],[0,0]]];far['bbox']=[0,0,1,1]
    monkeypatch.setattr(GeometryService,'records',lambda *a:[far])
    result2=bind_spatial_object(s,BindSpatialObject(scope_id=scope['scope_id'],entity_ids=['shape'],label_region_ids=[label.region_id]))
    assert result2['identity']['status']=='source_bound'
    assert result2['object_id']!=result['object_id']


def test_cross_layout_rejects_even_same_coordinates_and_name(workspace, monkeypatch):
    from cad_agent.geometry.service import GeometryService
    s, scope=setup_object(workspace)
    monkeypatch.setattr(GeometryService,'records',lambda *a:[geometry_record(layout='foreign')])
    with pytest.raises(ValueError,match='SPATIAL_SOURCE'):
        bind_spatial_object(s,BindSpatialObject(scope_id=scope['scope_id'],entity_ids=['shape']))


def test_legend_exact_polygon_not_bbox_and_stale_policy(workspace, monkeypatch):
    from cad_agent.geometry.service import GeometryService
    s, scope=setup_object(workspace)
    rec=geometry_record(layout=scope['layout_id'],path=[[1,1],[2,1],[2,2],[1,2],[1,1]])
    monkeypatch.setattr(GeometryService,'records',lambda *a:[rec])
    ref=dict(border_entity_id='legend-border',bbox=[0,0,10,10],boundary=[[0,0],[10,0],[10,10],[0,10],[0,0]],title_region_ids=['legend'])
    scope['object_workflow']['reference_contexts']=[ref]
    result=bind_spatial_object(s,BindSpatialObject(scope_id=scope['scope_id'],entity_ids=['shape'],role='placed_object'))
    assert result['role']['reference_only']
    # A triangular legend's bounding box must not swallow a different object.
    ref['boundary']=[[0,0],[10,0],[10,10],[0,0]]
    rec['paths']=[[[1,8],[2,8],[2,9],[1,9],[1,8]]];rec['bbox']=[1,8,2,9]
    result=bind_spatial_object(s,BindSpatialObject(scope_id=scope['scope_id'],entity_ids=['shape']))
    assert not result['role']['reference_only']
    s.task.data['unit_policy']={'unit':'m'}
    with pytest.raises(ValueError,match='unit policy'):
        resolve_object_input(s,result['object_id'])
