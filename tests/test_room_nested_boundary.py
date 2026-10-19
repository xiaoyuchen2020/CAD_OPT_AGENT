"""Independent geometry oracles; never import or execute generated candidates."""
from shapely import set_precision
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from scripts.nested_room_boundary import cases, base

def faces(b):
    layers=set(b['policy']['wall_layers']+b['policy']['opening_layers'])
    segments=[set_precision(LineString([s['a'],s['b']]),1e-5) for s in b['segments']
              if s['visibility']['visible'] and s['visibility']['effective_layer'] in layers]
    return [p for p in polygonize(unary_union(segments)) if p.contains(Point(b['target']['seed']))]

def test_positive_oracles_are_unique_hole_free_native_faces():
    positives=[c for c in cases() if c['expected_pass']]
    assert len(positives)==9
    for case in positives:
        found=faces(case['bundle']);assert len(found)==1,case['name']
        assert not found[0].interiors,case['name']
        assert found[0].symmetric_difference(Polygon(case['expected_polygon'])).area<1,case['name']

def test_annulus_and_interior_island_require_holes():
    for case in cases():
        if case['name'] in ['seed_in_annulus','interior_hole']:
            found=faces(case['bundle']);assert len(found)==1
            assert len(found[0].interiors)==1 and not case['expected_pass']

def test_target_is_not_global_smallest_ring_and_can_move_to_sibling():
    c={r['name']:r for r in cases()}
    assert Polygon(c['smaller_non_target_ring']['expected_polygon']).area==2000000
    assert Polygon(c['target_in_sibling']['expected_polygon']).area==600000
    assert c['target_in_sibling']['bundle']['target']['seed']==[1900,500]

def test_source_seals_and_validator_remain_frozen():
    for c in cases():
        b=c['bundle'];body={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']}
        assert base.digest(body)==b['bundle_sha256']
        assert len({s['segment_id'] for s in b['segments']})==len(b['segments'])
    assert base.sha(base.OLD/'validator-v2.py')==base.VALIDATOR_SHA
