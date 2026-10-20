from copy import deepcopy
import math
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cad_agent.geometry.spatial_sections import (
    MeasureSpatialSections, MeasureElevationRelation, spatial_sections, elevation_relation,
    measure_spatial_sections, measure_elevation_relation)


def rec(eid, points):
    return dict(entity_id=eid, entity_path=[eid], layout_id='l', kind='LINE' if len(points)==2 else 'LWPOLYLINE',
        status='ready', linear=True, paths=[points])


def obj(oid, records, unit='unknown'):
    return dict(object_id=oid, source_sha256='sha', snapshot_id='s', layout_id='l', frame_id='f',
        scope_id='scope', role=dict(value='source_geometry', status='asserted', reference_only=False),
        label=[], geometry=dict(records=records), unit=dict(value=unit, policy=None),
        exclusions=dict(records=[]), coverage=dict(selected_entities_complete=True))


def faces():
    return obj('a',[rec('a',[[-5,-2],[5,-2]])]), obj('b',[rec('b',[[-5,2],[5,2]])])


def square(oid,x1,y1,x2,y2):
    return obj(oid,[rec(oid,[[x1,y1],[x2,y1],[x2,y2],[x1,y2],[x1,y1]])])


def sections(a,b,blocks=(),**kwargs):
    args=MeasureSpatialSections(object_id=a['object_id'], other_object_id=b['object_id'] if b else None,
        axis_origin=kwargs.pop('axis_origin',(0,0)),axis_direction=kwargs.pop('axis_direction',(1,0)),**kwargs)
    return spatial_sections(a,b,list(blocks),args)


@pytest.mark.parametrize('angle,shift',[(0,(0,0)),(.731,(47,-101)),(2.3,(1e7,-1e7))])
def test_rotated_translated_source_opposing_faces(angle,shift):
    a,b=faces()
    c,s=math.cos(angle),math.sin(angle)
    for o in (a,b):
        for r in o['geometry']['records']:
            r['paths']=[[[x*c-y*s+shift[0],x*s+y*c+shift[1]] for x,y in r['paths'][0]]]
    result=sections(a,b,axis_origin=shift,axis_direction=(c,s))
    assert result['sampled_minimum']['width_drawing_units']==pytest.approx(4,abs=1e-8)
    assert result['measurements']['samples'][0]['width_m'] is None
    assert not result['global_minimum_proven']
    assert not result['spatial_fact_scope']['engineering_confirmed']


def test_disjoint_longitudinal_faces_cannot_be_nearest_line_width():
    a,b=faces();b['geometry']['records'][0]['paths']=[[[6,2],[10,2]]]
    result=sections(a,b)
    assert not result['measurements'] and not result['operation_receipts'][0]['fulfilled']
    assert result['diagnostics'][0]['code']=='no_longitudinal_overlap'


def test_axis_validation_and_same_side_faces():
    a,b=faces()
    with pytest.raises(ValueError,match='direction'):sections(a,b,axis_direction=(1,1))
    with pytest.raises(ValueError,match='nonzero'):sections(a,b,axis_direction=(0,0))
    assert not sections(a,b,axis_origin=(0,4))['measurements']
    assert not sections(a,b,axis_origin=(6,0))['measurements']
    with pytest.raises(ValidationError):
        MeasureSpatialSections(object_id='a',axis_origin=(float('nan'),0),axis_direction=(1,0))


def test_strict_model_json_array_schema():
    args=MeasureSpatialSections.model_validate(dict(object_id='a',other_object_id='b',
        axis_origin=[0,0],axis_direction=[1,0],stations=[0]),strict=True)
    assert args.axis_origin==[0,0]
    args=MeasureElevationRelation.model_validate(dict(object_id='a',
        lower_label_region_id='lower',upper_label_region_id='upper'),strict=True)
    assert args.object_id=='a'


def test_selected_protrusions_are_subtracted_at_same_station_only():
    a,b=faces()
    blocks=[square('p',-4,-2,-2,-1),square('q',2,1,4,2)]
    result=sections(a,b,blocks,stations=[-3,0,3])
    samples=result['measurements']['samples']
    assert [s['width_drawing_units'] for s in samples]==[3,4,3]
    assert [s['baseline_width_drawing_units'] for s in samples]==[4,4,4]
    assert result['sampled_minimum']['width_drawing_units']==3
    assert result['obstacle_census']=='selected_objects_only'
    assert result['sections'][0]['active_obstacle_object_ids']==['p']
    assert result['sections'][2]['active_obstacle_object_ids']==['q']


def test_skew_protrusion_exact_intersection_not_bbox():
    a,b=faces()
    p=obj('p',[rec('p',[[-2,-2],[2,-2],[-2,-1],[-2,-2]])])
    result=sections(a,b,[p],stations=[-1,1])
    assert [s['width_drawing_units'] for s in result['measurements']['samples']]==pytest.approx([3.25,3.75])


def test_axis_blocked_and_offstation_obstacle():
    a,b=faces();p=square('p',-1,-1,1,1)
    r=sections(a,b,[p],stations=[0,2])
    assert r['sections'][0]['reason']=='selected_obstacle_blocks_axis'
    assert len(r['measurements']['samples'])==1
    assert r['measurements']['samples'][0]['width_drawing_units']==4


def test_closed_region_concavity_and_holes():
    a=obj('a',[rec('a',[[-5,-2],[5,-2],[5,2],[1,2],[1,1],[-1,1],[-1,2],[-5,2],[-5,-2]])])
    result=sections(a,None,stations=[-3,0,3])
    assert [s['width_drawing_units'] for s in result['measurements']['samples']]==[4,3,4]
    assert result['width_interpretation'].startswith('sampled_')


def test_unknown_known_assumed_units():
    a,b=faces()
    assert sections(a,b)['sampled_minimum']['width_m'] is None
    for o in (a,b):o['unit']['value']='mm'
    assert sections(a,b)['sampled_minimum']['width_m']==pytest.approx(.004)
    for o in (a,b):o['unit']=dict(value='unknown',policy=dict(unit='mm',condition='explicit',quote='单位按毫米'))
    assert sections(a,b)['sampled_minimum']['conditional']
    assert sections(a,b)['sampled_minimum']['width_m']==pytest.approx(.004)


def test_gaps_multiple_faces_and_non_native_source_not_hidden():
    a,b=faces()
    a['geometry']['records'][0]['paths']=[[[-5,-2],[-1,-2]],[[1,-2],[5,-2]]]
    assert not sections(a,b)['measurements']
    a,b=faces();a['geometry']['records'].append(rec('extra',[[-5,-1],[5,-1]]))
    assert not sections(a,b)['measurements']
    a,b=faces();a['geometry']['records'][0]['linear']=False
    with pytest.raises(ValueError,match='native linear'):sections(a,b)
    a,b=faces();a['exclusions']['records']=[rec('x',[[0,0],[1,0]])]
    with pytest.raises(ValueError,match='EXCLUSIONS'):sections(a,b)


def labelled(lower,upper):
    a=square('a',-5,-2,5,2)
    a['label']=[dict(region_id=rid,text=text,entity_ids=[rid],points=[[0,0]],relation='unverified')
                for rid,text in [('lower',lower),('upper',upper)]]
    return a


def elevations(a,question='',**kwargs):
    args=MeasureElevationRelation(object_id='a',lower_label_region_id='lower',upper_label_region_id='upper',**kwargs)
    return elevation_relation(a,args,question)


def test_negative_source_elevations_same_datum_and_view_not_clear_height():
    a=labelled('结构标高 -6.600m; 基准:A; 剖面:2-2','结构标高 -3.000m; 基准:A; 剖面:2-2')
    r=elevations(a)
    assert r['status']=='source_annotation_difference'
    assert r['measurements']['elevation_difference_m']==3.6
    assert r['annotations'][0]['role']=='structural'
    assert r['layer_height'] is None and r['clear_height'] is None
    assert not r['spatial_fact_scope']['engineering_confirmed']


def test_adjacent_chinese_annotation_keeps_negative_sign():
    a=labelled('结构标高-6.600m; 基准:A; 剖面:2-2','完成面标高-3.000m; 基准:A; 剖面:2-2')
    r=elevations(a)
    assert r['measurements']['lower_elevation_m']==-6.6
    assert r['measurements']['upper_elevation_m']==-3.0
    assert r['measurements']['elevation_difference_m']==3.6


def test_generic_coordinate_units_do_not_become_annotation_units():
    a=labelled('-6.600','-3.000')
    q='假设同一基准。假设同一剖面。几何单位按米。提取标高，按同一剖面对比用户标签3.7m。'
    r=elevations(a,q,user_datum_quote='假设同一基准',user_view_quote='假设同一剖面')
    assert not r['measurements']
    assert any(d['code']=='missing_annotation_unit' for d in r['diagnostics'])


def test_unqualified_negative_values_not_layer_height():
    a=labelled('-6.600','-3.000')
    for unit in ['unknown','mm','m']:
        a['unit']['value']=unit
        r=elevations(a)
        assert not r['measurements']
        assert {d['code'] for d in r['diagnostics']}=={'missing_common_view','missing_common_datum','missing_annotation_unit'}


@pytest.mark.parametrize('field,a,b',[('datum','A','B'),('view','2-2','3-3')])
def test_source_datum_or_view_conflict_never_overridden(field,a,b):
    texts=['结构标高 -6.600m; 基准:A; 剖面:2-2','建筑完成面标高 -3.000m; 基准:A; 剖面:2-2']
    token='基准:' if field=='datum' else '剖面:'
    texts[1]=texts[1].replace(token+a,token+b)
    question='假设同一基准。假设同一剖面。'
    r=elevations(labelled(*texts),question,user_datum_quote='假设同一基准',user_view_quote='假设同一剖面')
    assert r['status']=='conflicting_elevation_basis'
    assert not r['measurements']


def test_conditional_user_assumption_must_be_literal_not_verification_request():
    a=labelled('-6.600','-3.000')
    q='假设同一基准。假设同一剖面。标高单位按米。'
    r=elevations(a,q,user_datum_quote='假设同一基准',user_view_quote='假设同一剖面')
    assert r['measurements']['elevation_difference_m']==3.6 and r['measurements']['conditional']
    assert r['status']=='conditional_annotation_difference'
    with pytest.raises(ValueError,match='current-question'):
        elevations(a,'核实同一基准',user_datum_quote='核实同一基准')
    with pytest.raises(ValueError,match='current-question'):
        elevations(a,'核实同一基准',user_datum_quote='假设同一基准')
    with pytest.raises(ValueError,match='current-question'):
        elevations(a,'不要假设同一基准',user_datum_quote='不要假设同一基准')


def test_roles_retained_and_reversed_order_or_ambiguous_sources_blocked():
    a=labelled('结构标高 -3.000m; 基准:A; 视图:V','完成面标高 -6.600m; 基准:A; 视图:V')
    r=elevations(a)
    assert not r['measurements'] and r['annotations'][1]['role']=='finished'
    for bad in ['LT1-2.600标高平面图1:50','-9.750(绝对标高-6.050)','600','-3.0至-2.0']:
        a['label'][0]['text']=bad
        assert not elevations(a)['measurements']


def test_session_sections_resolves_every_source_and_records(monkeypatch):
    from cad_agent.analysis import spatial_evidence
    a,b=faces();p=square('p',1,-2,2,-1)
    objects={o['object_id']:o for o in (a,b,p)};calls=[];saved=[]
    monkeypatch.setattr(spatial_evidence,'resolve_object_input',lambda s,oid:(calls.append(oid),objects[oid])[1])
    monkeypatch.setattr(spatial_evidence,'record_measurement',lambda s,oid,r:saved.append(oid))
    args=MeasureSpatialSections(object_id='a',other_object_id='b',axis_origin=(0,0),axis_direction=(1,0),obstacle_object_ids=['p'])
    assert measure_spatial_sections(object(),args)['measurements']
    assert calls==saved==['a','b','p']


def test_session_annotation_source_revalidation(monkeypatch):
    from cad_agent.analysis import spatial_evidence
    a=labelled('-6.600','-3.000');saved=[]
    monkeypatch.setattr(spatial_evidence,'resolve_object_input',lambda s,oid:deepcopy(a))
    monkeypatch.setattr(spatial_evidence,'_scope',lambda s,sid:{})
    monkeypatch.setattr(spatial_evidence,'_labels',lambda *args:deepcopy(a['label']))
    monkeypatch.setattr(spatial_evidence,'record_measurement',lambda s,oid,r:saved.append(r))
    args=MeasureElevationRelation(object_id='a',lower_label_region_id='lower',upper_label_region_id='upper')
    session=SimpleNamespace(request=SimpleNamespace(question=''))
    r=measure_elevation_relation(session,args)
    assert not r['measurements'] and saved==[r]
    monkeypatch.setattr(spatial_evidence,'_labels',lambda *args:[])
    with pytest.raises(ValueError,match='STALE'):measure_elevation_relation(session,args)
