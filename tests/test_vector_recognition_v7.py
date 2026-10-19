from dataclasses import replace
import json,math,subprocess,sys
from pathlib import Path
import pytest
from cad_agent.geometry.vector_recognition import Primitive,RecognitionOptions
from cad_agent.geometry.vector_recognition_v5 import recognize_vectors_v5
from cad_agent.geometry.vector_recognition_v7 import LocalParts,OwnershipV7,build_v7,apply_v7,recognize_vectors_v7
from scripts.floorplancad_dev_v7_data import OUT,BASE,OLD,PREV,PREV5,PREV6,roster,forbidden

def line(e,a,b):return Primitive(e,'line',(a,b))
def door():
    return [Primitive('arc','arc',((10,0),(0,10)),(0,0),10,90),line('leaf',(0,0),(10,0)),line('edge',(0,.03),(5,.03)),line('duplicate',(0,.03),(5,.03)),line('unrelated',(3,.3),(5,.3))]
def window(extensions=True):
    ps=[line('low',(0,-1),(10,-1)),line('inner',(0,0),(10,0)),line('high',(0,1),(10,1)),line('cap',(0,-1),(0,1)),line('right',(10,-1),(10,1))]
    for a,b,side in [(-8,0,'l'),(10,18,'r')]:
        for y in (-1,1):ps.append(line(f'{side}{y}',(a,y),(b,y)))
    if extensions:ps += [line('below',(0,-4),(0,-1)),line('above',(0,1),(0,4))]
    return ps

def test_disabled_policies_preserve_v5_roles():
    ps=door()+[replace(p,entity_id='w'+p.entity_id,points=tuple((x+40,y) for x,y in p.points)) for p in window()]
    assert recognize_vectors_v7(ps)['entity_roles']==recognize_vectors_v5(ps)['entity_roles']

def test_coincident_added_parts_abstain_without_erasing_observed_seed():
    ps=door();r=recognize_vectors_v7(ps,features=OwnershipV7(door_policy='unique'))
    assert r['entity_roles']['arc']=='door' and r['entity_roles']['leaf']=='door'
    assert r['entity_roles'].get('edge')!='door' and r['entity_roles'].get('duplicate')!='door'
    assert any({'edge','duplicate'}<=set(d['removed_added_ids']) for d in r['door_part_decisions'])
    assert r['automatic_room_closure'] is False and r['original_geometry_preserved']

def test_unique_door_parts_do_not_require_host_wall():
    ps=[p for p in door() if p.entity_id!='duplicate'];r=recognize_vectors_v7(ps,features=OwnershipV7(door_policy='connected_unique'))
    assert r['entity_roles']['edge']=='door' and r['entity_roles'].get('unrelated')!='door'

def test_seed_connectivity_uses_real_endpoints_not_virtual_hinge():
    ps=[line('seed',(.2,0),(10,0)),line('disconnected',(0,0),(0,.03))]
    c={'evidence':dict(seed_ids=['seed'],added_part_ids=['disconnected'],radius=10,hinge=(0,0),open_tip=(10,0))}
    assert not LocalParts(ps).door(c)['disconnected']['seed_connected']

def test_closed_contour_does_not_get_fake_degree_from_duplicates():
    ps=[line('seed',(0,0),(10,0)),line('short',(0,0),(1,0)),line('duplicate',(1,0),(0,0))]
    c={'evidence':dict(seed_ids=['seed'],added_part_ids=['short','duplicate'],radius=10,hinge=(0,0),open_tip=(10,0))}
    assert not LocalParts(ps).door(c)['short']['closed_contour']

def test_bilateral_cap_extension_preserves_source_support():
    r=recognize_vectors_v7(window(),features=OwnershipV7(cap_policy='both_extensions'))
    assert r['entity_roles'].get('cap')!='window' and r['entity_roles'].get('inner')=='window'
    assert 'cap' in r['support_only_ids']
    assert r['entity_roles'].get('cap')!='wall'
    for c in r['candidates']:
        if c['kind']=='window':
            ev=c['evidence'];assert set(ev['observed_frame_entity_ids'])==set(c['entity_ids'])|set(ev['unassigned_boundary_support_ids'])

def test_one_sided_extension_is_not_bilateral_evidence():
    ps=[p for p in window() if p.entity_id!='above'];a=recognize_vectors_v7(ps,features=OwnershipV7(cap_policy='both_extensions'));b=recognize_vectors_v7(ps,features=OwnershipV7(cap_policy='any_extension'))
    assert a['entity_roles'].get('cap')=='window' and b['entity_roles'].get('cap')!='window'

def test_window_policy_does_not_change_doors():
    state=build_v7(door());ref=state['full']['entity_roles']
    for p in ('both_extensions','any_extension','coincident','both_or_coincident'):
        assert apply_v7(state,OwnershipV7(cap_policy=p))['entity_roles']==ref

def test_geometry_transform_and_id_permutation_invariant():
    ps=door();ang=.63;scale=23
    def xy(pt):return (scale*(pt[0]*math.cos(ang)-pt[1]*math.sin(ang))+51,scale*(pt[0]*math.sin(ang)+pt[1]*math.cos(ang))-38)
    moved=[replace(p,entity_id=f'opaque-{i}',points=tuple(xy(pt) for pt in p.points),center=xy(p.center) if p.center else None,radius=p.radius*scale if p.radius else None) for i,p in enumerate(ps)]
    f=OwnershipV7(door_policy='connected_unique');a=recognize_vectors_v7(ps,features=f);b=recognize_vectors_v7(list(reversed(moved)),features=f)
    assert [a['entity_roles'].get(p.entity_id) for p in ps]==[b['entity_roles'].get(p.entity_id) for p in moved]

def test_budget_failure_is_explicit():assert not recognize_vectors_v7(window(),RecognitionOptions(max_primitives=1))['complete']
def test_unknown_policy_rejected():
    with pytest.raises(ValueError):recognize_vectors_v7([],features=OwnershipV7(door_policy='drawing_specific'))

def test_fresh_roster_excludes_544_previous_families():
    used=set(json.loads((OUT/'excluded_used_families.json').read_text()));fresh=roster('fresh_checks');analysis=roster('analysis')
    assert len(used)==544 and len(analysis)==154 and len({r['group_id'] for r in fresh})==96
    assert used.isdisjoint(r['group_id'] for r in fresh)
    assert all(r['split']=='development' for r in analysis+fresh)
    assert len({r['geometry_sha256'] for r in fresh})==96

def test_guard_covers_all_old_checks_test_and_validation():
    allowed={roster('analysis')[0]['path']}
    for p in [OLD/'refined/checks_results.json',PREV/'fresh_checks_results.json',PREV5/'fresh_checks_results.json',PREV6/'fresh_checks_results.json',BASE/'dataset/svg/validation/a.svg','/tmp/test-00.tar.xz',roster('fresh_checks')[0]['path']]:assert forbidden(p,allowed)

def test_actual_read_guard_blocks_before_opening():
    code="""
from pathlib import Path
from scripts.floorplancad_dev_v7_data import *
a=roster('analysis');f=roster('fresh_checks');install_guard([a[0]])
assert Path(a[0]['path']).read_bytes().startswith(b'<')
for p in [OLD/'refined/checks_results.json',PREV/'fresh_checks_results.json',PREV5/'fresh_checks_results.json',PREV6/'fresh_checks_results.json',f[0]['path']]:
 try:Path(p).read_bytes()
 except PermissionError:pass
 else:raise AssertionError('Isolation failed')
"""
    r=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True);assert r.returncode==0,r.stderr
