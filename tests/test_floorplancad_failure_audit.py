import json,subprocess,sys
from pathlib import Path
from scripts.floorplancad_failure_audit import OUT,ROOT,BASE,PREV,stage,roster,forbidden

def test_stage_accounting_keeps_generation_filtering_conflict_separate():
    assert stage([],[],0,4)=='generation_no_target_candidate'
    assert stage([1],[],0,4)=='filtering_candidate_lost'
    assert stage([1],[2],0,4)=='accepted_role_conflict'
    assert stage([1],[2],1,4)=='partial_part_assignment'
    assert stage([1],[2],2,4)=='half_coverage_or_more'

def test_fixed_roster_has_no_new_check_path():
    a=roster();b=set(json.loads((OUT/'excluded_check_families.json').read_text()))
    assert len(a)==154 and len({r['group_id'] for r in a})==154 and len(b)==486
    assert b.isdisjoint(r['group_id'] for r in a)
    assert all(r['split']=='development' for r in a)

def test_v7_checks_and_unknown_development_paths_are_blocked():
    allowed={roster()[0]['path']}
    assert not forbidden(roster()[0]['path'],allowed)
    for p in [PREV/'fresh_checks_results.json',PREV/'predictions/fresh_checks/v5_full/a.json',BASE/'dataset/svg/development/unused.svg',BASE/'dataset/svg/validation/a.svg','/tmp/test-00.tar.xz']:
        assert forbidden(p,allowed)

def test_actual_guard_denies_before_io():
    code="""
from pathlib import Path
from scripts.floorplancad_failure_audit import *
a=roster();guard([a[0]])
assert Path(a[0]['path']).read_bytes().startswith(b'<')
for p in [PREV/'fresh_checks_results.json',BASE/'dataset/svg/validation/a.svg',BASE/'dataset/svg/development/unused.svg']:
 try:Path(p).read_bytes()
 except PermissionError:pass
 else:raise AssertionError('Isolation failed')
"""
    r=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True);assert r.returncode==0,r.stderr

def test_topology_diagnostic_does_not_close_unobserved_gap():
    from cad_agent.geometry.vector_recognition import Primitive
    from scripts.floorplancad_failure_audit_cases import linework
    ps=[Primitive(str(i),'line',(a,b)) for i,(a,b) in enumerate([((0,0),(1,0)),((1,0),(1,1)),((1,1),(0,1)),((0,1),(0,.001))])]
    r=linework(ps,{p.entity_id for p in ps},[-1,-1,2,2])
    assert r['polygon_faces']==0 and r['synthetic_edges']==0 and r['snapping'] is False

def test_topology_counts_coincident_sources_without_duplicate_faces():
    from cad_agent.geometry.vector_recognition import Primitive
    from scripts.floorplancad_failure_audit_cases import linework
    pairs=[((0,0),(1,0)),((1,0),(1,1)),((1,1),(0,1)),((0,1),(0,0))]
    ps=[Primitive(str(i),'line',(a,b)) for i,(a,b) in enumerate(pairs+pairs)]
    r=linework(ps,{p.entity_id for p in ps},[-1,-1,2,2])
    assert r['source_lines']==8 and r['unique_clipped_lines']==4 and r['polygon_faces']==1
