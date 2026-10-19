from dataclasses import replace
import json,math,subprocess,sys
from pathlib import Path
from cad_agent.geometry.vector_recognition import Primitive,RecognitionOptions
from cad_agent.geometry.vector_recognition_v5 import WindowContext,recognize_vectors_v5,RefinementsV5
from cad_agent.geometry.vector_recognition_v4 import recognize_vectors_v4
from scripts.floorplancad_dev_v5_data import OUT,PREV,OLD,BASE,roster,forbidden

def line(e,a,b):return Primitive(e,'line',(a,b))
def frame(inner=(0,10),duplicate=False):
    y=-1 if duplicate else 0
    ps=[line('low',(0,-1),(10,-1)),line('inner',(inner[0],y),(inner[1],y)),line('high',(0,1),(10,1)),line('leftcap',(0,-1),(0,1)),line('rightcap',(10,-1),(10,1))]
    for side,a,b in [('l',-8,0),('r',10,18)]:
        for y in (-1,1):ps.append(line(f'{side}{y}',(a,y),(b,y)))
    return ps

def test_duplicate_outer_face_is_not_independent_window_rail():
    ps=frame(duplicate=True)
    assert any(c['kind']=='window' for c in recognize_vectors_v4(ps)['candidates'])
    r=recognize_vectors_v5(ps)
    assert not any(c['kind']=='window' for c in r['candidates'])
    assert any('no_independent_interior_rail_spanning_frame' in c['reasons'] for c in r['window_rejections'])

def test_valid_interior_rail_and_host_provenance():
    r=recognize_vectors_v5(frame());cs=[c for c in r['candidates'] if c['kind']=='window'];assert cs
    assert any('inner' in c['evidence']['wall_conflict_profile']['independent_interior_ids'] for c in cs)
    for c in cs:assert set(c['entity_ids']).isdisjoint(c['evidence'].get('host_support_ids',[]))

def test_short_internal_tick_does_not_make_a_window():
    r=recognize_vectors_v5(frame(inner=(2.5,7.5)))
    assert not any(c['kind']=='window' for c in r['candidates'])

def test_vetoes_are_independently_switchable():
    ps=frame()+[line('through_left',(-5,0),(0,0)),line('through_right',(10,0),(15,0)),line('cross',(5,-4),(5,4))]
    r=recognize_vectors_v5(ps)
    assert any(c['kind']=='window' for c in r['candidates'])
    for config in [RefinementsV5(continuation_veto=True),RefinementsV5(crossing_veto=True)]:
        assert not any(c['kind']=='window' for c in recognize_vectors_v5(ps,features=config)['candidates'])

def door():
    return [Primitive('arc','arc',((10,0),(0,10)),(0,0),10,90),line('leaf',(0,0),(10,0)),line('edge',(0,.03),(5,.03)),line('cap',(10,0),(10,.03)),line('unconnected',(3,.3),(5,.3)),line('outside',(5,0),(5,4))]

def test_door_completion_is_independent_from_hard_host():
    ps=door();free=recognize_vectors_v5(ps,features=RefinementsV5(windows=False));hard=recognize_vectors_v5(ps,features=RefinementsV5(windows=False,door_host_hard=True));disabled=recognize_vectors_v5(ps,features=RefinementsV5(windows=False,door_parts=False))
    assert free['entity_roles']['edge']=='door'
    assert hard['entity_roles'].get('edge')!='door' and disabled['entity_roles'].get('edge')!='door'
    assert free['entity_roles'].get('unconnected')!='door' and free['entity_roles'].get('outside')!='door'

def test_door_completion_cannot_start_without_observed_leaf_seed():
    ps=[p for p in door() if p.entity_id not in ('leaf','edge','cap')]
    assert not any(c['kind']=='door' for c in recognize_vectors_v5(ps)['candidates'])

def test_scale_rotation_translation_and_opaque_ids():
    ps=frame(duplicate=True)+[line('distractor',(50,10),(54,12))];angle=.43;scale=13
    def xy(pt):return (scale*(pt[0]*math.cos(angle)-pt[1]*math.sin(angle))+91,scale*(pt[0]*math.sin(angle)+pt[1]*math.cos(angle))-72)
    moved=[replace(p,entity_id=f'opaque{i}',points=tuple(xy(pt) for pt in p.points)) for i,p in enumerate(ps)]
    a=recognize_vectors_v5(ps);b=recognize_vectors_v5(moved)
    assert [a['entity_roles'].get(p.entity_id) for p in ps]==[b['entity_roles'].get(p.entity_id) for p in moved]

def test_incomplete_input_not_promoted():
    assert not recognize_vectors_v5(frame(),RecognitionOptions(max_primitives=1))['complete']

def test_new_roster_excludes_all_352_used_families():
    used=set(json.loads((OUT/'excluded_used_families.json').read_text()));fresh=roster('fresh_checks')
    assert len(used)==352 and len(fresh)==96
    assert used.isdisjoint(r['group_id'] for r in fresh)
    assert len(roster('analysis'))==154

def test_guard_blocks_both_old_check_batches_and_validation_test():
    allowed={roster('analysis')[0]['path']}
    for p in [OLD/'refined/checks_results.json',PREV/'fresh_checks_results.json',PREV/'predictions/fresh_checks/full/x.json',BASE/'dataset/svg/validation/x.svg','/tmp/test-00.tar.xz',roster('fresh_checks')[0]['path']]:assert forbidden(p,allowed)

def test_actual_read_guard_blocks_before_io():
    code="""
from pathlib import Path
from scripts.floorplancad_dev_v5_data import roster,install_guard,OLD,PREV
analysis=roster('analysis');fresh=roster('fresh_checks')
install_guard([analysis[0]])
assert Path(analysis[0]['path']).read_bytes().startswith(b'<')
for p in [OLD/'refined/checks_results.json',PREV/'fresh_checks_results.json',fresh[0]['path']]:
 try:Path(p).read_bytes()
 except PermissionError:pass
 else:raise AssertionError('Isolation failed')
"""
    r=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True);assert r.returncode==0,r.stderr
