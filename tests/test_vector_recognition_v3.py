from cad_agent.geometry.vector_recognition import Primitive,recognize_vectors
from cad_agent.geometry.vector_recognition_v3 import recognize_vectors_v3,Refinements
from tests.test_vector_recognition_v2 import door,line,swing_references
from dataclasses import replace
import math
import pytest


def test_local_chain_and_partial_swing_without_global_replacement():
    p=door();r=recognize_vectors_v3(p)
    assert set(r['entity_roles'])=={'arc','leaf','leaf2'}
    assert set(r['entity_roles'].values())=={'door'}
    assert r['original_geometry_preserved']


def test_existing_window_is_retained():
    p=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1)),line('c',(0,2),(20,2)),line('l',(0,0),(0,2)),line('r',(20,0),(20,2))]
    assert recognize_vectors_v3(p)['entity_roles']==recognize_vectors(p)['entity_roles']


def test_bounded_split_caps_support_window():
    p=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1)),line('c',(0,2),(20,2)),
       line('l1',(0,0),(0,1)),line('l2',(0,1),(0,2)),line('r1',(20,0),(20,1)),line('r2',(20,1),(20,2))]
    r=recognize_vectors_v3(p)
    assert r['entity_roles']['b']=='window'
    assert set(r['entity_roles'].values())=={'window'}


def test_arbitrarily_long_crossbars_do_not_identify_window():
    p=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1)),line('c',(0,2),(20,2)),line('l',(0,-20),(0,20)),line('r',(20,-20),(20,20))]
    assert not any(c['kind']=='window' for c in recognize_vectors_v3(p)['candidates'])


def test_only_wall_gate_changes_original_wall_candidates():
    p=swing_references()+[line('a',(0,10),(5,10)),line('b',(0,10.05),(5,10.05)),line('cap',(0,10),(0,10.05))]
    assert 'a' not in recognize_vectors_v3(p)['entity_roles']
    assert recognize_vectors_v3(p,features=Refinements(False,False,False,False))['entity_roles']==recognize_vectors(p)['entity_roles']


def test_background_near_jamb_is_not_grown_into_door():
    p=door()+[line('background',(.4,.8),(.5,.8))]
    assert 'background' not in recognize_vectors_v3(p)['entity_roles']


def test_radial_chain_does_not_fill_a_real_gap():
    p=door();p[2]=line('leaf2',(.7,0),(1,0))
    assert not recognize_vectors_v3(p)['entity_roles']


def test_v3_scale_rotation_and_translation_invariant():
    p=door();angle=.372;scale=250
    def xf(pt):x,y=pt;return (scale*(x*math.cos(angle)-y*math.sin(angle))+41,scale*(x*math.sin(angle)+y*math.cos(angle))-92)
    q=[replace(v,points=tuple(xf(pt) for pt in v.points),center=xf(v.center) if v.center else None,radius=v.radius*scale if v.radius else None) for v in p]
    assert recognize_vectors_v3(p)['entity_roles']==recognize_vectors_v3(q)['entity_roles']


def test_cad_v3_is_opt_in_and_retains_segment_provenance():
    from cad_agent.geometry.vector_adapter import recognize_cad_records
    root=dict(layout_id='layout',coordinate_space='dxf_wcs',status='ready')
    arc=root|dict(entity_id='arc',entity_path=['insert','arc'],kind='ARC',linear=False,
        paths=[[(1,0),(.5,math.sqrt(3)/2)]],native=dict(center=[0,0,0],radius=1,start_angle=0,end_angle=60))
    leaf=root|dict(entity_id='leaf',entity_path=['insert','polyline'],kind='LWPOLYLINE',linear=True,paths=[[(0,0),(.5,0),(1,0)]])
    assert not recognize_cad_records([arc,leaf])['candidates']
    result=recognize_cad_records([arc,leaf],method='v3')
    assert result['candidates'][0]['entity_ids']==['arc','leaf']
    assert len(result['candidates'][0]['source_parts'])==3
    assert result['requested_method']=='v3' and not result['automatic_room_closure']
    assert result['refinements']['bounded_caps'] is False
    with pytest.raises(ValueError):recognize_cad_records([arc,leaf],method='unknown')
