from copy import deepcopy
import math
import pytest
from cad_agent.geometry.spatial_relations import (
    MeasureSpatialRelation, InspectDoorSweep, MeasureDoorSweepRelation,
    spatial_relation, door_sweep, door_sweep_relation)


def rec(eid, pts, kind=None, native=None):
    return dict(entity_id=eid, entity_path=[eid], layout_id='l', status='ready',
        linear=kind != 'ARC', kind=kind or ('LINE' if len(pts)==2 else 'LWPOLYLINE'), paths=[pts], native=native or {})


def obj(oid, records, role='placed_object', unit='unknown', reference=False):
    return dict(object_id=oid, source_sha256='sha', snapshot_id='s', layout_id='l', frame_id='f', scope_id='scope',
        role=dict(value=role, status='asserted', reference_only=reference), label=[dict(text='same name')],
        geometry=dict(records=records), unit=dict(value=unit, policy=None), exclusions=dict(records=[]),
        coverage=dict(selected_entities_complete=True))


def square(eid, x, y, w=2):
    return rec(eid, [[x,y],[x+w,y],[x+w,y+w],[x,y+w],[x,y]])


def relation(a, b, **kw):
    return spatial_relation(a,b,MeasureSpatialRelation(object_id=a['object_id'],other_object_id=b['object_id'],**kw))


@pytest.mark.parametrize('angle,shift', [(0,(0,0)),(.731,(47,-101)),(2.3,(1e7,-1e7))])
def test_multientity_rotated_clearance(angle,shift):
    a=obj('a',[square('a',0,0)]); b=obj('b',[square('b',4,0)])
    c,s=math.cos(angle),math.sin(angle)
    for o in (a,b):
        records=[]
        for record in o['geometry']['records']:
            p=[[x*c-y*s+shift[0],x*s+y*c+shift[1]] for x,y in record['paths'][0]]
            records.extend(rec(f'{o["object_id"]}{i}',[p[i],p[i+1]]) for i in range(4))
        o['geometry']['records']=records
    r=relation(a,b)
    assert r['measurements']['minimum_distance_drawing_units']==pytest.approx(2,abs=1e-8)
    assert r['measurements']['minimum_distance_m'] is None
    assert r['relations']['disjoint']
    assert not r['coverage']['same_name_objects_merged']
    assert not r['coverage']['whole_scope_enumerated']


def test_interleaved_bboxes_are_not_polygon_overlap():
    a=obj('a',[rec('a',[[0,0],[4,0],[0,4],[0,0]])])
    b=obj('b',[rec('b',[[4,4],[4,1],[1,4],[4,4]])])
    r=relation(a,b)
    assert r['relations']['bbox_intersects_broadphase_only']
    assert r['relations']['disjoint'] and not r['relations']['positive_area_overlap']
    assert r['measurements']['minimum_distance_drawing_units']==pytest.approx(math.sqrt(.5))


@pytest.mark.parametrize('x,y,expected',[(1,1,'overlaps'),(2,0,'touches'),(.5,.5,'a_contains_b_strictly')])
def test_overlap_touch_and_containment(x,y,expected):
    a=obj('a',[square('a',0,0)]);b=obj('b',[square('b',x,y,.5 if expected.startswith('a_') else 2)])
    r=relation(a,b)
    assert r['relations'][expected]
    assert r['measurements']['minimum_distance_drawing_units']==0
    assert not r['spatial_fact_scope']['identity_confirmed']


def test_open_lines_do_not_define_object_clearance():
    a=obj('a',[rec('a',[[0,0],[0,2]])]);b=obj('b',[rec('b',[[3,0],[3,2]])])
    with pytest.raises(ValueError,match='closed region'):relation(a,b)
    r=relation(a,b,basis='source_paths',other_basis='source_paths')
    assert r['measurements']['minimum_distance_drawing_units']==3
    assert r['distance_interpretation']=='selected_source_path_distance_not_object_clearance'


def test_scope_layout_legend_and_units_rejection():
    a=obj('a',[square('a',0,0)]);b=obj('b',[square('b',3,0)])
    for field in ('source_sha256','snapshot_id','layout_id','frame_id','scope_id'):
        bad=deepcopy(b);bad[field]='other'
        with pytest.raises(ValueError,match='SPATIAL_SCOPE'):relation(a,bad)
    bad=deepcopy(b);bad['role']['reference_only']=True
    with pytest.raises(ValueError,match='SPATIAL_REFERENCE'):relation(a,bad)
    bad=deepcopy(b);bad['unit']['value']='mm'
    with pytest.raises(ValueError,match='SPATIAL_UNIT'):relation(a,bad)
    for o in (a,b): o['unit']['policy']=dict(unit='mm',condition='unknown_units')
    assert relation(a,b)['measurements']['minimum_distance_m']==pytest.approx(.001)
    assert relation(a,b)['measurements']['conditional']


def door(start=0,extrusion=None):
    native=dict(center=[0,0,0],radius=2,start_angle=start,end_angle=start+90)
    if extrusion:native['extrusion']=extrusion
    c,s=math.cos(math.radians(start)),math.sin(math.radians(start))
    if extrusion and extrusion[2]<0:c=-c
    return obj('door',[rec('arc',[[2*c,2*s],[0,2]],'ARC',native),rec('leaf',[[0,0],[2*c,2*s]])],role='door')


def test_native_door_sweep_requires_full_radial_support():
    a=door();r,c=door_sweep(a,InspectDoorSweep(object_id='door'))
    assert len(c)==1 and r['measurements'][0]['sector_area_drawing_units_squared']==pytest.approx(math.pi)
    a['geometry']['records'][1]['paths']=[[[1,0],[2,0]]]
    r,c=door_sweep(a,InspectDoorSweep(object_id='door'))
    assert not c and r['status']=='unsupported_door_sweep'
    a=door();a['geometry']['records']=a['geometry']['records'][:1]
    assert not door_sweep(a,InspectDoorSweep(object_id='door'))[1]
    a=door();a['role']['value']='source_geometry'
    assert not door_sweep(a,InspectDoorSweep(object_id='door'))[1]


@pytest.mark.parametrize('start,extrusion',[(0,None),(31,None),(17,[0,0,-1])])
def test_native_arc_rotation_mirror_candidate(start,extrusion):
    r,c=door_sweep(door(start,extrusion),InspectDoorSweep(object_id='door'))
    assert len(c)==1
    assert r['measurements'][0]['radius_drawing_units']==2


def test_door_arc_relation_bounds_not_chord_claim():
    a=door();b=obj('target',[square('t',.5,.5,.2)])
    r=door_sweep_relation(a,b,MeasureDoorSweepRelation(object_id='door',other_object_id='target'))
    assert r['relation']=='positive_area_intersection'
    assert r['containment']['sweep_contains_target_proven']
    assert r['measurements']['intersection_area_lower_drawing_units_squared']==pytest.approx(.04)
    assert not r['measurement_exact']
    b=obj('target',[square('t',3,0)])
    r=door_sweep_relation(a,b,MeasureDoorSweepRelation(object_id='door',other_object_id='target'))
    assert r['relation']=='disjoint'
    assert r['measurements']['minimum_distance_lower_drawing_units']<=1<=r['measurements']['minimum_distance_upper_drawing_units']
    b=obj('target',[square('t',2,0)])
    r=door_sweep_relation(a,b,MeasureDoorSweepRelation(object_id='door',other_object_id='target'))
    assert r['relation']=='boundary_uncertain_within_arc_bounds'


def test_composite_radial_leaf_and_no_tolerance_gap_filling():
    a=door();a['geometry']['records'][1:]=[rec('l1',[[0,0],[1,0]]),rec('l2',[[1,0],[2,0]])]
    _,c=door_sweep(a,InspectDoorSweep(object_id='door'))
    assert c[0][0]['radial_leaf_support'][0]['leaf_entity_ids']==['l1','l2']
    a['geometry']['records'][2]['paths'][0][0]=[1.001,0]
    assert not door_sweep(a,InspectDoorSweep(object_id='door'))[1]


def test_session_entry_revalidates_both_objects_and_persists(monkeypatch):
    from cad_agent.analysis import spatial_evidence
    from cad_agent.geometry.spatial_relations import measure_spatial_relation
    a=obj('a',[square('a',0,0)]);b=obj('b',[square('b',3,0)])
    calls=[];saved=[]
    def resolve(session,oid):
        calls.append(oid);return {'a':a,'b':b}[oid]
    monkeypatch.setattr(spatial_evidence,'resolve_object_input',resolve)
    monkeypatch.setattr(spatial_evidence,'record_measurement',lambda session,oid,result:saved.append((oid,result)))
    result=measure_spatial_relation(object(),MeasureSpatialRelation(object_id='a',other_object_id='b'))
    assert calls==['a','b'] and [oid for oid,_ in saved]==['a','b']
    assert all(r==result for _,r in saved)


def test_exclusions_are_not_silently_ignored_and_curves_not_promoted():
    a=obj('a',[square('a',0,0)]);b=obj('b',[square('b',3,0)])
    a['exclusions']['records']=[square('excluded',.5,.5,.2)]
    with pytest.raises(ValueError,match='SPATIAL_EXCLUSIONS'):relation(a,b)
    a['exclusions']['records']=[];a['geometry']['records'][0]['linear']=False
    with pytest.raises(ValueError,match='SPATIAL_BOUNDARY'):relation(a,b)


def test_multiple_closed_regions_require_explicit_contour_selection():
    from cad_agent.geometry.spatial_boundaries import boundary_topology
    a=obj('a',[square('a1',0,0),square('a2',0,4)]);b=obj('b',[square('b',3,0)])
    with pytest.raises(ValueError,match='SPATIAL_CONTOUR'):relation(a,b)
    inspection,_=boundary_topology(a)
    r=relation(a,b,contour_id=inspection['contours'][0]['contour_id'])
    assert r['geometry_selections'][0]['extent']=='selected_contour_only'


def receipt_map(name, result):
    from cad_agent.analysis.operations import receipts
    declared=receipts(name,result)
    assert declared==result['operation_receipts']
    assert all(r['scope']=='selected_source_geometry' and not r['engineering_confirmed'] and
        not r['identity_confirmed'] for r in declared)
    return {r['operation']:r['fulfilled'] for r in declared}


def test_exact_selected_region_and_source_path_receipts():
    a=obj('a',[square('a',0,0)]);b=obj('b',[square('b',3,0)])
    r=relation(a,b)
    assert receipt_map('measure_spatial_relation',r)=={'geometry':True,'relations':True}
    assert not r['next_calls']
    a=obj('a',[rec('a',[[0,0],[0,1]])]);b=obj('b',[rec('b',[[2,0],[2,1]])])
    r=relation(a,b,basis='source_paths',other_basis='source_paths')
    assert receipt_map('measure_spatial_relation',r)=={'geometry':True,'relations':True}
    assert all('not_object_clearance' in x['basis'] for x in r['operation_receipts'])


def test_door_inspection_receipts_do_not_claim_relation():
    a=door();r,_=door_sweep(a,InspectDoorSweep(object_id='door'))
    assert receipt_map('inspect_door_sweep',r)=={'geometry':True}
    assert not r['next_calls']  # No second object can be guessed.
    a['geometry']['records']=a['geometry']['records'][:1]
    r,_=door_sweep(a,InspectDoorSweep(object_id='door'))
    assert receipt_map('inspect_door_sweep',r)=={'geometry':False}
    a['role']['value']='source_geometry'
    r,_=door_sweep(a,InspectDoorSweep(object_id='door'))
    assert receipt_map('inspect_door_sweep',r)=={'geometry':False}


@pytest.mark.parametrize('x,y,expected',[(.5,.5,True),(3,0,True),(2,0,False)])
def test_bounded_sweep_relation_receipts_and_executable_refinement(x,y,expected):
    a=door();b=obj('target',[square('t',x,y,.2)])
    r=door_sweep_relation(a,b,MeasureDoorSweepRelation(object_id='door',other_object_id='target',arc_segments=16))
    assert receipt_map('measure_door_sweep_relation',r)=={'geometry':True,'relations':expected}
    if not expected:
        call=r['next_calls'][0]
        assert call['tool']=='measure_door_sweep_relation'
        refined=MeasureDoorSweepRelation.model_validate(call['arguments'])
        assert refined.arc_segments==32 and refined.other_object_id=='target'
        again=door_sweep_relation(a,b,refined)
        assert receipt_map(call['tool'],again)=={'geometry':True,'relations':False}
    else:assert not r['next_calls']


def test_unsupported_sweep_relation_has_no_success_receipts():
    a=door();a['geometry']['records']=a['geometry']['records'][:1]
    b=obj('target',[square('t',3,0)])
    r=door_sweep_relation(a,b,MeasureDoorSweepRelation(object_id='door',other_object_id='target'))
    assert receipt_map('measure_door_sweep_relation',r)=={'geometry':False,'relations':False}
    assert not r['next_calls']
