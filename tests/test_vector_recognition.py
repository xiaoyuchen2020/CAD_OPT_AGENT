from dataclasses import replace
import math
from pathlib import Path

import pytest

from cad_agent.geometry.vector_recognition import Primitive, RecognitionOptions, recognize_vectors
from scripts.floorplancad_svg import parse_svg, category
from scripts.floorplancad_prepare import partition
from scripts.floorplancad_benchmark import read_sample, counts, run


def line(eid,a,b):return Primitive(eid,'line',(a,b))


def test_swing_requires_observed_radial_leaf():
    arc=Primitive('arc','arc',((1.,0.),(0.,1.)),(0.,0.),1.,90.)
    assert not recognize_vectors([arc])['candidates']
    result=recognize_vectors([arc,line('leaf',(0.,0.),(1.,0.))])
    assert result['entity_roles']=={'arc':'door','leaf':'door'}
    assert not recognize_vectors([arc,line('chord',(1.,0.),(0.,1.))])['candidates']


def test_scale_rotation_translation_invariance():
    base=[Primitive('arc','arc',((1.,0.),(0.,1.)),(0.,0.),1.,90.),line('leaf',(0.,0.),(1.,0.))]
    def xf(p):return (1000*(-p[1])+5000,1000*p[0]-7000)
    transformed=[replace(p,points=tuple(xf(v) for v in p.points),center=xf(p.center) if p.center else None,
                         radius=p.radius*1000 if p.radius else None) for p in base]
    assert recognize_vectors(base)['entity_roles']==recognize_vectors(transformed)['entity_roles']


def test_parallel_lines_alone_do_not_establish_wall():
    rails=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1))]
    assert not recognize_vectors(rails)['candidates']
    result=recognize_vectors(rails+[line('cap',(0,0),(0,1))])
    assert result['entity_roles']=={'a':'wall','b':'wall'}


def test_window_requires_interior_rail_and_both_caps():
    rails=[line('a',(0,0),(20,0)),line('b',(0,1),(20,1)),line('c',(0,2),(20,2))]
    caps=[line('l',(0,0),(0,2)),line('r',(20,0),(20,2))]
    assert not any(c['kind']=='window' for c in recognize_vectors(rails+caps[:1])['candidates'])
    r=recognize_vectors(rails+caps)
    assert set(r['entity_roles'].values())=={'window'}


def test_annotation_color_layer_and_filename_independence():
    raw=b'<svg><g id="wall-layer"><path d="M 1,0 A 1,1 0 0,1 0,1" semantic-id="3" instance-id="42" stroke="red"/><path d="M 0,0 L 1,0" semantic-id="3"/></g></svg>'
    altered=raw.replace(b'wall-layer',b'anything').replace(b'"3"',b'"1"').replace(b'"42"',b'"5"').replace(b'red',b'blue')
    a,at,_,ah=parse_svg(raw);b,bt,_,bh=parse_svg(altered)
    assert at!=bt and a==b and ah==bh
    assert recognize_vectors(a)==recognize_vectors(b)


def test_svg_transform_and_circular_arc_geometry():
    raw=b'<svg><g transform="translate(10,20) rotate(90) scale(2)"><path d="M 1,0 A 1,1 0 0,1 0,1"/></g></svg>'
    p=parse_svg(raw)[0][0]
    assert p.center==pytest.approx((10,20))
    assert p.radius==pytest.approx(2)
    assert p.points[0]==pytest.approx((10,22))
    assert p.sweep_degrees==pytest.approx(90)


def test_nonuniform_arc_not_interpreted_as_circular_swing():
    p=parse_svg(b'<svg><path transform="scale(2,1)" d="M 1,0 A 1,1 0 0,1 0,1"/></svg>')[0][0]
    assert p.kind=='elliptic_arc' and p.radius is None


def test_unsupported_shape_stays_in_recall_denominator():
    p,t,d,h=parse_svg(b'<svg><path d="M 0,0 C 0,1 1,1 1,0" semantic-id="3"/></svg>')
    assert not p and d and len(t)==1
    assert counts(t,{})['door']=={'tp':0,'fp':0,'fn':1}


def test_raw_label_mapping_is_not_remapped_vocabulary():
    assert [category(i) for i in (1,2,3,8,9,11,12,33)]==['wall','other','door','door','window','window','other','other']


def test_test_archive_guard_before_any_file_access(tmp_path):
    row={'archive':'test-00.tar.xz','split':'development','path':str(tmp_path/'does-not-exist'),'sha256':'x'}
    with pytest.raises(ValueError,match='allowlisted'):read_sample(row,tmp_path)
    with pytest.raises(ValueError,match='Test evaluation'):run(tmp_path,tmp_path,'test')


def test_path_escape_refused(tmp_path):
    row={'archive':'train-00.tar.xz','split':'development','path':'/etc/passwd','sha256':'x'}
    with pytest.raises(ValueError,match='escapes'):read_sample(row,tmp_path)


def test_group_split_is_deterministic():
    assert partition('0034')==partition('0034')
    assert {partition(str(i)) for i in range(50)}=={'development','validation'}


def test_budget_is_explicit_incomplete_not_empty_success():
    result=recognize_vectors([line('a',(0,0),(1,0))],RecognitionOptions(max_primitives=0))
    assert result['complete'] is False and result['diagnostics']


def test_annotations_cannot_be_passed_as_recognizer_records():
    with pytest.raises(TypeError):recognize_vectors([{'semantic-id':3}])


def test_dtd_is_rejected():
    with pytest.raises(ValueError):parse_svg(b'<!DOCTYPE svg><svg/>')


def test_cad_adapter_preserves_polyline_source_without_layers():
    from cad_agent.geometry.vector_adapter import recognize_cad_records
    base=dict(layout_id='layout',coordinate_space='dxf_wcs',status='ready')
    arc=base|dict(entity_id='A',entity_path=['insert','arc'],kind='ARC',linear=False,
        paths=[[(1,0),(0,1)]],native=dict(center=[0,0,0],radius=1,start_angle=0,end_angle=90))
    leaf=base|dict(entity_id='L',entity_path=['insert','polyline'],kind='LWPOLYLINE',linear=True,
        layer='untrusted',paths=[[(0,0),(1,0)]])
    result=recognize_cad_records([arc,leaf])
    assert result['candidates'][0]['entity_ids']==['A','L']
    assert result['candidates'][0]['source_parts'][1]['entity_path']==['insert','polyline']
    assert result['automatic_room_closure'] is False
    assert result==recognize_cad_records([arc,leaf|{'layer':'different'}])
    with pytest.raises(ValueError,match='layouts'):
        recognize_cad_records([arc,leaf|{'layout_id':'other'}])


def test_modified_source_refuses_validation(tmp_path,monkeypatch):
    import json
    import scripts.floorplancad_benchmark as benchmark
    (tmp_path/'freeze.json').write_text(json.dumps({'source_hashes':{'algo':'old'}}))
    (tmp_path/'selection_seal.json').write_text('{}')
    monkeypatch.setattr(benchmark,'source_hashes',lambda:{'algo':'new'})
    with pytest.raises(ValueError,match='Implementation changed'):
        benchmark.check_frozen(tmp_path,tmp_path)
