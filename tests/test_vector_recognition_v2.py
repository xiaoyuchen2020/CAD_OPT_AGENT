from dataclasses import replace
import math
from pathlib import Path
import subprocess
import sys

import pytest

from cad_agent.geometry.vector_recognition import Primitive, recognize_vectors
from cad_agent.geometry.vector_recognition_v2 import recognize_vectors_v2, Refinements, join_collinear
from scripts.floorplancad_dev_v2 import forbidden, read


def line(i,a,b):return Primitive(i,'line',(a,b))


def door():
    return [Primitive('arc','arc',((1,0),(.5,math.sqrt(3)/2)),(0,0),1,60),
            line('leaf',(0,0),(.5,0)),line('leaf2',(.5,0),(1,0))]


def test_segment_join_restores_partial_swing_without_metadata():
    p=door();assert not recognize_vectors(p)['candidates']
    result=recognize_vectors_v2(p)
    assert set(result['entity_roles'])=={'arc','leaf','leaf2'}
    assert set(result['entity_roles'].values())=={'door'}


def test_no_leaf_no_swing_classification():
    assert not recognize_vectors_v2(door()[:1])['candidates']


def test_join_does_not_fill_real_gaps_or_turns():
    p=[line('a',(0,0),(1,0)),line('b',(1.2,0),(2,0)),line('c',(1,0),(1,1))]
    assert len(join_collinear(p)[0])==3


def test_join_returns_source_ids_and_preserves_input():
    p=[line('a',(0,0),(1,0)),line('b',(1,0),(2,0))]
    original=repr(p);joined,mapping=join_collinear(p)
    assert len(joined)==1 and list(mapping.values())==[['a','b']]
    assert repr(p)==original


def test_extended_window_caps_support_without_claiming_whole_wall():
    p=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1)),line('c',(0,2),(20,2)),
       line('left',(0,-5),(0,8)),line('right',(20,-5),(20,8))]
    result=recognize_vectors_v2(p,features=Refinements(False,False,True,False))
    assert set(result['entity_roles'])=={'a','b','c'}
    assert set(result['entity_roles'].values())=={'window'}
    assert set(result['candidates'][0]['evidence']['cap_support_ids'])=={'left','right'}


def test_missing_cap_does_not_create_window():
    p=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1)),line('c',(0,2),(20,2)),line('left',(0,-5),(0,8))]
    assert not any(c['kind']=='window' for c in recognize_vectors_v2(p)['candidates'])


def test_disabled_features_reproduce_v1_roles():
    p=[Primitive('arc','arc',((1,0),(0,1)),(0,0),1,90),line('leaf',(0,0),(1,0))]
    assert recognize_vectors_v2(p,features=Refinements(False,False,False,False))['entity_roles']==recognize_vectors(p)['entity_roles']


def test_rotated_scaled_translated_v2_invariant():
    p=door();a=.421;s=1250
    def xf(pt):x,y=pt;return (s*(x*math.cos(a)-y*math.sin(a))+18,s*(x*math.sin(a)+y*math.cos(a))-31)
    q=[replace(v,points=tuple(xf(pt) for pt in v.points),center=xf(v.center) if v.center else None,radius=v.radius*s if v.radius else None) for v in p]
    assert recognize_vectors_v2(p)['entity_roles']==recognize_vectors_v2(q)['entity_roles']


def test_holdout_and_test_paths_rejected_before_read():
    for p in ('/data/test-00.tar.xz','/dataset/svg/validation/train-00/a.svg','/baseline/validation/metrics.json'):
        assert forbidden(p)
        with pytest.raises(ValueError):read({'split':'development','path':p})


def test_runtime_read_guard_blocks_other_splits(tmp_path):
    code='from scripts.floorplancad_dev_v2 import install_guard; install_guard([]); open("/tmp/validation/metrics.json")'
    r=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
    assert r.returncode!=0 and 'PermissionError' in r.stderr


def test_renaming_source_ids_does_not_change_semantic_counts():
    p=door();q=[replace(v,entity_id='renamed-'+v.entity_id) for v in p]
    a=recognize_vectors_v2(p)['entity_roles'];b=recognize_vectors_v2(q)['entity_roles']
    assert a=={k.removeprefix('renamed-'):v for k,v in b.items()}


def swing_references():
    out=[]
    for i in range(3):
        x=10+i*5
        out.extend([Primitive(f'arc{i}','arc',((x+1,0),(x,1)),(x,0),1,90),line(f'leaf{i}',(x,0),(x+1,0))])
    return out


def test_thin_strip_needs_context_when_swing_scale_available():
    p=swing_references()+[line('a',(0,10),(5,10)),line('b',(0,10.05),(5,10.05)),line('cap',(0,10),(0,10.05))]
    full=recognize_vectors_v2(p)
    assert full['wall_rejections'] and 'a' not in full['entity_roles']
    no_wall=recognize_vectors_v2(p,features=Refinements(True,True,True,False))
    assert no_wall['entity_roles']['a']=='wall'


def test_thin_connected_strip_retained_with_external_support():
    p=swing_references()+[line('a',(0,10),(5,10)),line('b',(0,10.05),(5,10.05)),
        line('left',(0,9),(0,11)),line('right',(5,9),(5,11))]
    r=recognize_vectors_v2(p)
    assert r['entity_roles']['a']=='wall' and not r['wall_rejections']


def test_no_swing_scale_does_not_invent_physical_wall_width():
    p=[line('a',(0,10),(5,10)),line('b',(0,10.05),(5,10.05)),line('cap',(0,10),(0,10.05))]
    r=recognize_vectors_v2(p)
    assert r['entity_roles']['a']=='wall' and not r['wall_rejections']


def test_input_order_does_not_change_simple_connected_roles():
    p=door()
    assert recognize_vectors_v2(p)['entity_roles']==recognize_vectors_v2(p[::-1])['entity_roles']
