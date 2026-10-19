"""Contour replay evidence. No optimization rules or external CAD writes."""
import json
import logging
import runpy
from hashlib import sha256
from pathlib import Path

from shapely.geometry import Polygon

from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.geometry.contracts import RoomDiscover, RoomAreaMeasure
from cad_agent.geometry.service import GeometryService
from scripts.validate_room_discovery import render

ROOT = Path('artifacts/room-contours-20261015')
ARCHIVE = Path('artifacts/ar030-real-room-validation-20261015')


def dump(name, data):
    (ROOT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def save_case(name, w, service, record, candidate, exclusion_ids):
    dump(name+'-discovery.json', record)
    model = candidate['geometry_model']
    by_id = {c['contour_id']: c for c in record['contours']}
    measurements = []
    for basis in ('outer_shell', 'topological_face', 'selected_exclusions'):
        area = service.room_area(RoomAreaMeasure(discovery_id=record['measurement_id'], candidate_id=candidate['candidate_id'],
            basis=basis, exclusion_contour_ids=exclusion_ids if basis == 'selected_exclusions' else []))
        dump(name+'-'+basis+'.json', area)
        (ROOT/(name+'-'+basis+'.svg')).write_text(service.svg(area['measurement_id']))
        # Independent arithmetic: signed shoelace over exported WCS rings,
        # translated before summation to avoid cancellation at large coordinates.
        def ring_area(points):
            x, y = points[0]
            return abs(sum((a[0]-x)*(b[1]-y)-(b[0]-x)*(a[1]-y) for a, b in zip(points, points[1:])))/2
        factor = .001 if record['applied_unit'] == 'mm' else 1
        independent = sum(ring_area(p['boundary'])-sum(ring_area(h) for h in p['holes']) for p in area['result_polygons'])*factor**2
        assert abs(area['basis_area_m2']-independent) < 1e-5
        measurements.append(dict(basis=basis, area_m2=area['basis_area_m2'], excluded_union_area_m2=area['excluded_union_area_m2'],
            shoelace_area_m2=independent, status=area['status'], selected_contour_ids=area['selected_contour_ids']))
    # Original DXF rendering plus all internal contours of this one candidate;
    # room names are not used by the discovery algorithm.
    rings = [by_id[cid] for cid in model['interior_contour_ids']]
    overlay = record | {'candidates':[candidate]+[dict(boundary=c['boundary'],
        seed=list(Polygon(c['boundary']).representative_point().coords[0]),candidate_role='enclosure_unconfirmed') for c in rings]}
    image = render(w, service.sid, overlay, ROOT/(name+'-original-overlay.png'), contour_mode=True)
    result = dict(case=name, source_name=service.drawing['name'], source_sha256=record['source_sha256'],
        snapshot_id=service.sid, discovery_id=record['measurement_id'], candidate_id=candidate['candidate_id'],
        interior_contour_count=len(rings), topological_hole_count=len(model['topological_hole_ids']),
        diagnostics=record['diagnostics']+candidate['diagnostics'], measurements=measurements, image=image,
        evidence_scope='candidate_geometry_replay_not_confirmed_net_area',
        overlay_legend='green: selected outer contour; orange: all unclassified internal contours; numbers are contour indexes')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    ROOT.mkdir(exist_ok=True)
    results = []
    w = Workspace(Settings(data_dir=ROOT/'data'))
    try:
        if (ROOT/'nested-fixture-discovery.json').exists():
            previous = json.loads((ROOT/'nested-fixture-discovery.json').read_text())
            s = GeometryService(w, previous['snapshot_id'])
            r = s.discover(RoomDiscover.model_validate(previous['request']))
            c = min(r['candidates'], key=lambda c: abs(c['candidate_shell_area_m2']-100))
        else:
            fixture = runpy.run_path('tests/test_room_contours.py')['fixture']
            s, r, c = fixture(w)
        ids = [o['contour_id'] for o in r['contours'] if o['candidate_area_m2'] == 4]
        results.append(save_case('nested-fixture', w, s, r, c, ids))
    finally:
        w.close()
    # Reuse frozen original DXF, bounding box only. The former measured outline
    # is an independent comparison target, never passed to recognition.
    prior = json.loads((ARCHIVE/'explicit-column/measurement.json').read_text())
    w = Workspace(Settings(data_dir=ARCHIVE/'data'))
    try:
        s = GeometryService(w, prior['snapshot_id'])
        assert sha256(w.store.artifact(s.sid, 'source.dxf').read_bytes()).hexdigest() == prior['source_sha256']
        target = Polygon(prior['selected']['boundary'])
        b = target.bounds
        r = s.discover(RoomDiscover(layout_id=prior['layout_id'], bbox=[b[0]-1000,b[1]-1000,b[2]+1000,b[3]+1000],
            unit_override='mm', view_kind='floor_plan'))
        c = min(r['candidates'], key=lambda c: Polygon(c['boundary']).symmetric_difference(target).area)
        delta = Polygon(c['boundary']).symmetric_difference(target).area*1e-6
        assert delta < .001, delta
        known_eid = prior['request']['void_entity_ids'][0]
        ids = [o['contour_id'] for o in r['contours'] if known_eid in o['entity_ids'] and abs(o['candidate_area_m2']-.25)<1e-6
               and o['contour_id'] in c['geometry_model']['interior_contour_ids']]
        assert len(ids) == 1
        result = save_case('real-3554-pump', w, s, r, c, ids)
        result.update(original_outline_symmetric_difference_m2=delta,
            selected_exclusion_basis='prior explicit source entity, selected for comparison only; recognition has no layer/name filter',
            selected_source_entity_id=known_eid,
            unit_note='DXF header unknown; mm is explicit replay assumption')
        manifest = json.loads((ARCHIVE/'input_manifest.json').read_text())
        source = next(item for item in manifest if item['dxf_sha256'] == r['source_sha256'])
        result.update(original_dwg=source['source']['name'], group=source['group'], paper_id=source['paper_id'])
        assert result['measurements'][0]['area_m2'] == 63.145
        assert result['measurements'][2]['area_m2'] == 62.895
        results.append(result)
    finally:
        w.close()
    refs = json.loads((ARCHIVE/'reference-hashes.json').read_text())
    checks = {p:sha256(Path(p).read_bytes()).hexdigest() == h for p,h in refs.items()}
    assert all(checks.values())
    dump('reference-verification.json', checks)
    dump('review.json', results)


if __name__ == '__main__':
    main()
