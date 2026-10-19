import math
from dataclasses import replace
from pathlib import Path
import pytest
from cad_agent.geometry.vector_recognition import Primitive,RecognitionOptions
from cad_agent.geometry.vector_recognition_v4 import build_relations,apply_relations,recognize_vectors_v4,Relations
from scripts.floorplancad_dev_v4_data import forbidden,OUT,OLD,BASE,roster

def line(e,a,b):return Primitive(e,'line',(a,b))
def frame(host=True,caps=True):
    ps=[line('r0',(0,-1),(10,-1)),line('r1',(0,0),(10,0)),line('r2',(0,1),(10,1))]
    if caps:ps += [line('c0',(0,-1),(0,0)),line('c1',(0,0),(0,1)),line('c2',(10,-1),(10,1))]
    if host:
        for x,a,b in [('left',-8,0),('right',10,18)]:
            for y in [-1,1]:ps.append(line(f'{x}{y}',(a,y),(b,y)))
    return ps

def test_external_host_is_evidence_not_window_geometry():
    r=recognize_vectors_v4(frame());cs=[c for c in r['candidates'] if c['kind']=='window' and c['evidence'].get('host_supported')]
    assert cs
    for c in cs:
        assert set(c['entity_ids']).isdisjoint(c['evidence']['host_support_ids'])
        assert len(c['evidence']['host_support_ids'])==4
    assert all(r['entity_roles'].get(e)!='window' for e in ['left-1','left1','right-1','right1'])

def test_missing_caps_requires_explicit_ablation():
    b=build_relations(frame(caps=False));strict=apply_relations(b);loose=apply_relations(b,Relations(window_caps=False))
    assert not any(c['kind']=='window' for c in strict['candidates'])
    assert any(c['kind']=='window' for c in loose['candidates'])

def test_frame_cannot_witness_its_own_host():
    b=build_relations(frame(host=False));assert all(not p['evidence']['host_supported'] for p in b['proposals'])
    assert not any(c['kind']=='window' for c in apply_relations(b)['candidates'])
    assert any(c['kind']=='window' for c in apply_relations(b,Relations(host_wall=False))['candidates'])

def test_hosts_must_continue_on_both_sides():
    ps=[p for p in frame() if not p.entity_id.startswith('right')];b=build_relations(ps)
    assert all(not p['evidence']['host_supported'] for p in b['proposals'])

def test_door_parts_stay_in_observed_connected_leaf_corridor():
    ps=[Primitive('arc','arc',((10,0),(0,10)),(0,0),10,90),line('leaf',(0,0),(10,0)),line('edge',(0,.03),(10,.03)),line('cap',(10,0),(10,.03)),line('unconnected',(3,.3),(5,.3)),line('outside',(5,0),(5,4))]
    r=recognize_vectors_v4(ps,features=Relations(host_wall=False))
    assert r['entity_roles'].get('edge')=='door'
    assert r['entity_roles'].get('unconnected')!='door'
    assert r['entity_roles'].get('outside')!='door'

def test_uniform_scale_rotation_translation_and_opaque_ids():
    ps=frame();a=.73;scale=17
    def xy(p):return (scale*(p[0]*math.cos(a)-p[1]*math.sin(a))+41,scale*(p[0]*math.sin(a)+p[1]*math.cos(a))-13)
    changed=[replace(p,entity_id='opaque'+str(i),points=tuple(xy(q) for q in p.points)) for i,p in enumerate(ps)]
    x=recognize_vectors_v4(ps);y=recognize_vectors_v4(changed)
    assert [x['entity_roles'].get(p.entity_id) for p in ps]==[y['entity_roles'].get(p.entity_id) for p in changed]

def test_budget_does_not_silently_promote_partial_scan():
    r=recognize_vectors_v4(frame(),RecognitionOptions(max_primitives=1));assert not r['complete']

def test_all_previous_families_excluded_from_fresh_check():
    import json
    a=roster('analysis');b=roster('fresh_checks');used=set(json.loads((OUT/'excluded_used_families.json').read_text()))
    assert len(a)==154 and len(b)==96 and len({r['group_id'] for r in b})==96
    assert not used&{r['group_id'] for r in b}

def test_read_guard_denies_test_validation_old_checks_and_unlisted_svg():
    row=roster('analysis')[0];allowed={row['path']}
    assert not forbidden(row['path'],allowed)
    for path in [BASE/'dataset/svg/validation/x.svg',Path('/tmp/test-00.tar.xz'),OLD/'refined/checks_results.json',roster('fresh_checks')[0]['path'],BASE/'baseline/validation/metrics.json']:
        assert forbidden(path,allowed)


def test_real_audit_hook_blocks_before_open_in_child_process():
    import subprocess,sys
    code="""
from scripts.floorplancad_dev_v4_data import roster,install_guard,OLD
from pathlib import Path
analysis=roster('analysis');fresh=roster('fresh_checks')
install_guard([analysis[0]])
assert Path(analysis[0]['path']).read_bytes().startswith(b'<')
for path in [fresh[0]['path'],OLD/'refined/checks_results.json']:
    try:Path(path).read_bytes()
    except PermissionError:pass
    else:raise AssertionError('Unauthorized read succeeded')
"""
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_source_and_selection_seals_are_still_valid():
    import json
    from scripts.floorplancad_dev_v4_eval import check_freeze
    from scripts.floorplancad_dev_v4_data import sha
    check_freeze()
    seal=json.loads((OUT/'selection_seal.json').read_text())
    assert seal['analysis_sha256']==sha((OUT/'analysis_results.json').read_bytes())
    assert seal['freeze_sha256']==sha((OUT/'candidate_freeze.json').read_bytes())
