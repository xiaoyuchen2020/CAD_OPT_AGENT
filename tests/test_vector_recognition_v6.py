from dataclasses import replace
import json,math,subprocess,sys
from pathlib import Path
from cad_agent.geometry.vector_recognition import Primitive,RecognitionOptions
from cad_agent.geometry.vector_recognition_v6 import build_v6,apply_v6,recognize_vectors_v6,Ownership
from scripts.floorplancad_dev_v6_data import OUT,PREV,PREV5,OLD,BASE,roster,forbidden

def line(e,a,b):return Primitive(e,'line',(a,b))
def frame(host=True):
    ps=[line('low',(0,-1),(10,-1)),line('inner',(0,0),(10,0)),line('high',(0,1),(10,1)),line('leftcap',(0,-1),(0,1)),line('rightcap',(10,-1),(10,1))]
    if host:
        for side,a,b in [('l',-8,0),('r',10,18)]:
            for y in (-1,1):ps.append(line(f'{side}{y}',(a,y),(b,y)))
    return ps

def test_shared_caps_remain_observed_support_not_window_or_fabricated_wall():
    state=build_v6(frame());r=apply_v6(state,Ownership(cap_policy='shared'))
    assert r['entity_roles'].get('inner')=='window'
    for eid in ['leftcap','rightcap']:
        assert r['entity_roles'].get(eid) not in ('window','wall')
        assert eid in r['support_only_ids']
    cs=[c for c in r['candidates'] if c['kind']=='window'];assert cs
    for c in cs:
        assert set(c['evidence']['observed_frame_entity_ids'])==set(c['entity_ids'])|set(c['evidence']['shared_boundary_support_ids'])
        assert set(c['entity_ids']).isdisjoint(c['evidence']['shared_boundary_support_ids'])

def test_shared_ownership_requires_outside_geometry():
    ps=[replace(p,points=tuple((x*2,y) for x,y in p.points)) for p in frame(False)]
    r=recognize_vectors_v6(ps,features=Ownership(cap_policy='shared'))
    assert r['entity_roles'].get('leftcap')=='window'
    allcaps=recognize_vectors_v6(ps,features=Ownership(cap_policy='all'))
    assert allcaps['entity_roles'].get('leftcap')!='window'

def test_door_part_completion_is_unchanged_across_ownership_ablations():
    ps=frame()+[Primitive('arc','arc',((50,0),(40,10)),(40,0),10,90),line('leaf',(40,0),(50,0)),line('edge',(40,.03),(45,.03)),line('tick',(44,.3),(46,.3))]
    state=build_v6(ps);ref={e for e,v in state['door_reference']['entity_roles'].items() if v=='door'}
    assert 'edge' in ref and 'tick' not in ref
    for f in [Ownership(),Ownership(cap_policy='all'),Ownership(cap_policy='wall_evidence'),Ownership(outer_support=True)]:
        r=apply_v6(state,f);assert {e for e,v in r['entity_roles'].items() if v=='door'}==ref

def test_restore_only_preexisting_wall_proposals():
    ps=frame()+[line('nearcap',(-.2,-1),(-.2,1))];state=build_v6(ps)
    wall=dict(kind='wall',entity_ids=['leftcap','nearcap'],reason='preexisting_pair',status='candidate',evidence={'return_entity_ids':[]})
    state['door_reference']['candidates'].append(wall)
    restored=apply_v6(state,Ownership(cap_policy='shared',restore_walls=True));off=apply_v6(state,Ownership(cap_policy='shared',restore_walls=False))
    assert ['leftcap','nearcap'] in restored['restored_wall_pairs']
    assert restored['entity_roles']['leftcap']=='wall'
    assert off['entity_roles'].get('leftcap')!='wall'
    assert restored['entity_roles'].get('rightcap')!='wall'

def test_rail_evidence_prevents_global_cap_suppression():
    state=build_v6(frame());c=next(c for c in state['full']['candidates'] if c['kind']=='window')
    # Another observed directional proposal treats this source as a rail. Shared
    # ownership must preserve that competing geometric evidence rather than erase it.
    state['part_profiles'][tuple(sorted(c['entity_ids']))]['interior_ids'].append('leftcap')
    r=apply_v6(state,Ownership(cap_policy='all'))
    assert 'leftcap' not in r['support_only_ids']

def test_scale_rotation_translation_id_invariance():
    ps=frame();ang=.63;scale=23
    def xy(pt):return (scale*(pt[0]*math.cos(ang)-pt[1]*math.sin(ang))+51,scale*(pt[0]*math.sin(ang)+pt[1]*math.cos(ang))-38)
    moved=[replace(p,entity_id=f'opaque-{i}',points=tuple(xy(pt) for pt in p.points)) for i,p in enumerate(ps)]
    a=recognize_vectors_v6(ps);b=recognize_vectors_v6(moved)
    assert [a['entity_roles'].get(p.entity_id) for p in ps]==[b['entity_roles'].get(p.entity_id) for p in moved]

def test_no_silent_success_on_budget_failure():
    assert not recognize_vectors_v6(frame(),RecognitionOptions(max_primitives=1))['complete']

def test_unknown_ownership_policy_rejected():
    import pytest
    with pytest.raises(ValueError):recognize_vectors_v6(frame(),features=Ownership(cap_policy='file_specific'))

def test_roster_excludes_all_previously_used_448_families():
    used=set(json.loads((OUT/'excluded_used_families.json').read_text()));fresh=roster('fresh_checks')
    assert len(used)==448 and len(fresh)==96 and len({r['group_id'] for r in fresh})==96
    assert used.isdisjoint(r['group_id'] for r in fresh) and len(roster('analysis'))==154

def test_guard_blocks_all_three_previous_check_batches():
    allowed={roster('analysis')[0]['path']}
    for p in [OLD/'refined/checks_results.json',PREV/'fresh_checks_results.json',PREV5/'fresh_checks_results.json',BASE/'dataset/svg/validation/x.svg','/tmp/test-00.tar.xz',roster('fresh_checks')[0]['path']]:assert forbidden(p,allowed)

def test_actual_guard_denies_reads_before_io():
    code="""
from pathlib import Path
from scripts.floorplancad_dev_v6_data import roster,install_guard,OLD,PREV,PREV5
analysis=roster('analysis');fresh=roster('fresh_checks');install_guard([analysis[0]])
assert Path(analysis[0]['path']).read_bytes().startswith(b'<')
for p in [OLD/'refined/checks_results.json',PREV/'fresh_checks_results.json',PREV5/'fresh_checks_results.json',fresh[0]['path']]:
 try:Path(p).read_bytes()
 except PermissionError:pass
 else:raise AssertionError('Isolation failed')
"""
    r=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True);assert r.returncode==0,r.stderr
