"""Generalization checks for exact source topology; no live model calls."""
from copy import deepcopy
import math
import pytest
from cad_agent.geometry.spatial_boundaries import boundary_topology, spatial_area, MeasureSpatialArea


def record(eid,path,layout='l'):
    return dict(entity_id=eid,entity_path=[eid],layout_id=layout,status='ready',linear=True,
        kind='LINE' if len(path)==2 else 'LWPOLYLINE',paths=[path])


def object_for(records,exclusions=(),unit='unknown',policy=None):
    return dict(object_id='o',source_sha256='sha',snapshot_id='s',layout_id='l',frame_id='f',scope_id='scope',
        role=dict(value='room_boundary',status='asserted',reference_only=False),
        geometry=dict(records=records),coverage=dict(selected_entities_complete=True),
        exclusions=dict(records=list(exclusions),entity_ids=[r['entity_id'] for r in exclusions]),
        unit=dict(value=unit,policy=policy))


def box_path(x=0,y=0,w=10,h=10):
    return [[x,y],[x+w,y],[x+w,y+h],[x,y+h],[x,y]]


def area(obj,basis='outer_shell',largest=False):
    inspection,_=boundary_topology(obj)
    contour=max(inspection['contours'],key=lambda c:c['measurements']['area_drawing_units_squared']) if largest else inspection['contours'][0]
    return spatial_area(obj,MeasureSpatialArea(object_id='o',contour_id=contour['contour_id'],basis=basis))


@pytest.mark.parametrize('angle,translation',[(0,(0,0)),(.72,(200,-340)),(2.31,(1e7,-1e7))])
def test_segmented_rotated_concave_source_is_measurable(angle,translation):
    path=[[0,0],[8,0],[8,3],[3,3],[3,7],[0,7],[0,0]]
    def transform(p):
        x,y=p;c,s=math.cos(angle),math.sin(angle)
        return [x*c-y*s+translation[0],x*s+y*c+translation[1]]
    path=list(map(transform,path))
    obj=object_for([record(str(i),[a,b]) for i,(a,b) in enumerate(zip(path,path[1:]))],unit='m')
    result=area(obj)
    assert result['measurements']['area_m2']==pytest.approx(36)
    assert result['measurements']['perimeter_m']==pytest.approx(30)
    assert all(e['source_entity_ids'] and not e['inferred'] for r in result['boundary_support'] for e in r['edge_evidence'])
    assert result['coverage']['net_boundary']=='unconfirmed'
    assert not result['engineering_confirmed']


def test_unknown_units_preserve_native_arithmetic_and_condition_policy():
    obj=object_for([record('a',box_path())])
    result=area(obj)
    assert result['measurements']['area_drawing_units_squared']==100
    assert result['measurements']['area_m2'] is None
    obj['unit']['policy']=dict(unit='mm',condition='unknown_units',quote='无单位则按毫米')
    result=area(obj)
    assert result['measurements']['area_m2']==pytest.approx(.0001)
    assert result['measurements']['conditional']
    obj['unit']['policy']['condition']='no_dimensions'
    assert area(obj)['measurements']['area_m2'] is None
    obj['unit'].update(value='m',policy=dict(unit='mm',condition='explicit'))
    assert area(obj)['unit']['unit_status']=='conflict'
    assert area(obj)['measurements']['area_m2'] is None


def test_hole_topology_is_distinct_from_shell_and_hole_semantics():
    obj=object_for([record('outer',box_path()),record('inner',box_path(2,2,2,2))])
    inspection,_=boundary_topology(obj)
    holed=next(c for c in inspection['contours'] if c['holes'])
    result=spatial_area(obj,MeasureSpatialArea(object_id='o',contour_id=holed['contour_id']))
    assert result['measurements']['area_drawing_units_squared']==96
    assert result['engineering_area_basis']=='unconfirmed'
    result=spatial_area(obj,MeasureSpatialArea(object_id='o',contour_id=holed['contour_id'],basis='outer_shell'))
    assert result['measurements']['area_drawing_units_squared']==100
    assert result['ignored_topological_holes']


def test_overlapping_explicit_exclusions_are_unioned_once():
    obj=object_for([record('outer',box_path())], [record('e1',box_path(1,1,3,3)),record('e2',box_path(2,2,3,3))])
    result=area(obj,'selected_exclusions')
    assert result['measurements']['excluded_union_area_drawing_units_squared']==14
    assert result['measurements']['area_drawing_units_squared']==86
    assert len(result['exclusion_boundary_support'])==1
    assert area(obj,'outer_shell')['measurements']['area_drawing_units_squared']==100


def test_exclusion_open_or_crossing_shell_rejected():
    obj=object_for([record('outer',box_path())],[record('e',[[1,1],[2,1],[2,2]])])
    with pytest.raises(ValueError,match='closed source'):
        area(obj,'selected_exclusions')
    obj['exclusions']['records']=[record('e',box_path(9,9,3,3))]
    with pytest.raises(ValueError,match='strictly inside'):
        area(obj,'selected_exclusions')


def test_open_boundary_has_no_bbox_or_nearest_endpoint_closure():
    obj=object_for([record('open',[[0,0],[10,0],[10,10],[0,10],[0,.001]])])
    result,_=boundary_topology(obj)
    assert result['status']=='open_boundary'
    assert not result['measurements'] and not result['contours']
    assert result['automatic_closure_segments']==[]
    assert len(result['open_endpoints'])==2
    assert result['diagnostics']


def test_open_graph_with_closed_local_loop_is_local_fact_not_complete_object():
    obj=object_for([record('outer',box_path()),record('tail',[[10,10],[15,15]])])
    result,_=boundary_topology(obj)
    assert result['measurements']
    assert not result['selected_graph_fully_polygonized']
    assert result['coverage']['object_extent']=='unconfirmed'
    assert result['coverage']['main_outline']=='unconfirmed'


def test_layout_and_reference_negatives():
    with pytest.raises(ValueError,match='cross layouts'):
        boundary_topology(object_for([record('a',box_path()),record('b',box_path(),layout='other')]))
    obj=object_for([record('frame',box_path(0,0,10000,10000))])
    obj['role']['reference_only']=True
    with pytest.raises(ValueError,match='reference-only'):
        boundary_topology(obj)
    obj['role'].update(reference_only=False,value='main_outline')
    result,_=boundary_topology(obj)
    assert result['coverage']['main_outline']=='unconfirmed'
    assert result['main_outline_selection']=='not_inferred_from_area_or_bbox'


def test_curves_and_invalid_closed_path_are_not_silently_treated_as_exact():
    obj=object_for([record('valid',box_path()),record('curve',box_path(1,1,2,2))])
    obj['geometry']['records'][1]['linear']=False
    result,_=boundary_topology(obj)
    assert not result['measurements'] and not result['source_geometry_complete']
    obj=object_for([record('bowtie',[[0,0],[5,5],[0,5],[5,0],[0,0]])])
    result,_=boundary_topology(obj)
    assert not result['measurements']
    assert result['diagnostics'][0]['code']=='invalid_closed_path'


def test_contour_identity_stale_object_is_not_accepted():
    obj=object_for([record('a',box_path())])
    result,_=boundary_topology(obj)
    cid=result['contours'][0]['contour_id']
    changed=deepcopy(obj);changed['object_id']='changed'
    with pytest.raises(ValueError,match='absent or stale'):
        spatial_area(changed,MeasureSpatialArea(object_id='changed',contour_id=cid))


def test_union_of_exclusions_preserves_hole_island_and_every_ring_support():
    exclusions=[record('bottom',box_path(2,2,6,1)),record('top',box_path(2,7,6,1)),
        record('left',box_path(2,3,1,4)),record('right',box_path(7,3,1,4))]
    result=area(object_for([record('shell',box_path())],exclusions),'selected_exclusions')
    assert result['measurements']['excluded_union_area_drawing_units_squared']==20
    assert result['measurements']['area_drawing_units_squared']==80
    assert len(result['result_polygons'])==2
    assert {r['kind'] for r in result['exclusion_boundary_support']}=={'exterior','interior'}
    assert all(e['source_entity_ids'] for r in result['exclusion_boundary_support'] for e in r['edge_evidence'])


def test_session_entry_points_resolve_and_record_without_identity_upgrade(monkeypatch):
    from cad_agent.geometry.spatial_boundaries import InspectSpatialBoundary,inspect_spatial_boundary,measure_spatial_area
    from cad_agent.analysis import spatial_evidence
    obj=object_for([record('a',box_path())]); evidence=[];calls=[]
    def resolve(session,oid):
        calls.append(oid)
        return deepcopy(obj)
    monkeypatch.setattr(spatial_evidence,'resolve_object_input',resolve)
    monkeypatch.setattr(spatial_evidence,'record_measurement',lambda session,oid,result:evidence.append(result))
    inspection=inspect_spatial_boundary(None,InspectSpatialBoundary(object_id='o'))
    result=measure_spatial_area(None,MeasureSpatialArea(object_id='o',contour_id=inspection['contours'][0]['contour_id']))
    assert len(evidence)==2 and calls==['o','o']
    assert result['kind']=='spatial_method'
    assert not result['spatial_fact_scope']['identity_confirmed']
    assert obj['role']['status']=='asserted'


def test_receipts_and_next_call_do_not_choose_largest_or_claim_main_outline():
    obj=object_for([record('large',box_path()),record('small',box_path(20,0,1,1))])
    result,_=boundary_topology(obj)
    assert result['operation_receipts'][0]['fulfilled']
    assert len(result['next_calls'])==2
    for call in result['next_calls']:
        measured=spatial_area(obj,MeasureSpatialArea(**call['arguments']))
        assert {r['operation'] for r in measured['operation_receipts']}=={'area','perimeter'}
        assert all(r['scope']=='selected_source_geometry' for r in measured['operation_receipts'])
        assert measured['coverage']['main_outline']=='unconfirmed'
    open_result,_=boundary_topology(object_for([record('open',[[0,0],[1,1]])]))
    assert not open_result['operation_receipts'][0]['fulfilled']
    assert not open_result['next_calls']


def test_containment_relation_direction_and_shared_segment_exclusion_ambiguity():
    obj=object_for([record('outer',box_path()),record('inner',box_path(2,2,2,2))])
    result,_=boundary_topology(obj)
    small=next(c for c in result['contours'] if c['measurements']['area_drawing_units_squared']==4)
    assert small['contained_by_contour_ids']
    assert all(next(c for c in result['contours'] if c['contour_id']==cid)['measurements']['area_drawing_units_squared']>4 for cid in small['contained_by_contour_ids'])
    # A shared open line graph does not encode which of several bounded faces
    # were intended as exclusions; force explicit closed source choices.
    segments=[record('seg'+str(i),[a,b]) for i,(a,b) in enumerate(zip(box_path(1,1,3,3),box_path(1,1,3,3)[1:]))]
    segments.append(record('split',[[1,2],[4,2]]))
    with pytest.raises(ValueError,match='ambiguous'):
        area(object_for([record('outer',box_path())],segments),'selected_exclusions')
