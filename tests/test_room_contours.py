import io
import json
import math

import ezdxf
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.contracts import RoomDiscover, RoomContoursRead, RoomAreaMeasure
from cad_agent.geometry.service import GeometryService


def fixture(workspace, *, scale=1, origin=(0, 0), angle=0, units=6, crossing=False, curved=False, touching=False):
    doc = ezdxf.new('R2018'); doc.units = units
    m = doc.modelspace()
    def pt(x, y):
        return (origin[0]+scale*(x*math.cos(angle)-y*math.sin(angle)),
                origin[1]+scale*(x*math.sin(angle)+y*math.cos(angle)))
    rectangles = [(-.2, -.2, 10.2, 10.2), (0, 0, 10, 10), (2, 2, 5, 5),
                  (3, 3, 4, 4), (7, 7, 9, 9)]
    if crossing:
        rectangles.append((4, 2, 6, 5))  # union with 3x3 is 12, not 15
    if touching:
        rectangles.append((0, 6, 2, 8))
    for x, y, xx, yy in rectangles:
        m.add_lwpolyline([pt(x, y), pt(xx, y), pt(xx, yy), pt(x, yy)], close=True)
    if curved:
        m.add_circle(pt(7, 3), scale)
        m.add_arc(pt(7, 3), scale*1.5, 0, 90)
    m.add_text('Room', dxfattribs={'insert': pt(1, 1), 'height': scale*.1})
    m.add_text('Inner object', dxfattribs={'insert': pt(2.5, 2.5), 'height': scale*.1})
    stream = io.StringIO(); doc.write(stream)
    d = workspace.ingest('nested-contours.dxf', stream.getvalue().encode())
    points = [pt(x, y) for x, y in [(-1, -1), (-1, 11), (11, -1), (11, 11)]]
    bounds = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
    service = GeometryService(workspace, d['id'])
    record = service.discover(RoomDiscover(layout_id=d['layouts'][0]['layout_id'], bbox=bounds, view_kind='floor_plan'))
    candidate = min(record['candidates'], key=lambda c: abs(c['candidate_shell_area_m2']-100))
    return service, record, candidate


def request(record, c, **kwargs):
    return RoomAreaMeasure(discovery_id=record['measurement_id'], candidate_id=c['candidate_id'], **kwargs)


@pytest.mark.parametrize('scale,origin,angle,units', [(1, (0, 0), 0, 6), (1000, (1e9, -2e9), .37, 4)])
def test_nested_contours_and_three_independent_area_bases(workspace, scale, origin, angle, units):
    service, r, c = fixture(workspace, scale=scale, origin=origin, angle=angle, units=units)
    model = c['geometry_model']; by_id = {o['contour_id']: o for o in r['contours']}
    inner = sorted((by_id[cid] for cid in model['interior_contour_ids']), key=lambda o: o['candidate_area_m2'])
    assert [o['candidate_area_m2'] for o in inner] == pytest.approx([1, 4, 9], abs=.005)
    assert inner[0]['parent_contour_ids'] == [inner[2]['contour_id']]
    assert set(model['topological_hole_ids']) == {inner[1]['contour_id'], inner[2]['contour_id']}
    assert 'alternative_inner_topology' in model['diagnostics']
    assert all(o['semantic_role'] == 'unclassified' and o['entity_ids'] and o['edge_evidence'] for o in inner)
    assert {t['text'] for t in c['text_regions']} == {'Room'}
    assert Polygon(c['boundary'], c['internal_rings']).contains(Point(c['seed']))
    for basis, exclusions, expected in [('outer_shell', [], 100), ('topological_face', [], 87),
        ('selected_exclusions', [inner[1]['contour_id']], 96),
        ('selected_exclusions', [o['contour_id'] for o in inner], 87)]:
        args = request(r, c, basis=basis, exclusion_contour_ids=exclusions)
        measured = service.room_area(args)
        assert measured['basis_area_m2'] == pytest.approx(expected, abs=.015)
        assert measured['area_m2'] is None and measured['status'] == 'needs_review'
        assert measured['shell_area_m2']-measured['excluded_union_area_m2'] == pytest.approx(measured['basis_area_m2'], abs=1e-6)
        assert service.room_area(args) == measured
        assert measured['source_sha256'] == r['source_sha256']
        assert measured['discovery_id'] == r['measurement_id']
        assert basis in service.svg(measured['measurement_id'])
    page = service.room_contours(RoomContoursRead(discovery_id=r['measurement_id'], candidate_id=c['candidate_id'], limit=1))
    assert page['total'] == 3 and page['has_more'] and len(page['items']) == 1


def test_intersecting_objects_union_and_original_paths_retained(workspace):
    service, r, c = fixture(workspace, crossing=True)
    interiors = [o for o in r['contours'] if o['contour_id'] in c['geometry_model']['interior_contour_ids']]
    originals = [o for o in interiors if 'source_closed_path' in o['methods'] and o['candidate_area_m2'] in (9, 6)]
    assert len(originals) == 2
    assert all(o['intersecting_boundary_ids'] for o in originals)
    measured = service.room_area(request(r, c, basis='selected_exclusions', exclusion_contour_ids=[o['contour_id'] for o in originals]))
    assert measured['excluded_union_area_m2'] == 12
    assert measured['basis_area_m2'] == 88


def test_scope_ids_invalid_selection_and_empty_exclusion(workspace):
    service, r, c = fixture(workspace)
    outer = c['geometry_model']['outer_contour_id']
    for cid in [outer, 'invented-contour', next(o['contour_id'] for o in r['contours'] if o['candidate_area_m2'] > 100)]:
        with pytest.raises(ValueError):
            service.room_area(request(r, c, basis='selected_exclusions', exclusion_contour_ids=[cid]))
    assert service.room_area(request(r, c, basis='selected_exclusions'))['basis_area_m2'] == 100
    with pytest.raises(ValueError):
        service.room_area(request(r, dict(candidate_id='foreign'), basis='outer_shell'))
    for basis, ids in [('outer_shell', [outer]), ('selected_exclusions', [outer, outer])]:
        with pytest.raises(ValidationError): request(r, c, basis=basis, exclusion_contour_ids=ids)
    other, _, _ = fixture(workspace, origin=(30, 0))
    with pytest.raises(KeyError): other.room_area(request(r, c, basis='outer_shell'))


def test_closed_curve_keeps_approximation_and_open_arc_is_not_a_hole(workspace):
    service, r, c = fixture(workspace, curved=True)
    inner = [o for o in r['contours'] if o['contour_id'] in c['geometry_model']['interior_contour_ids']]
    curved = [o for o in inner if o['approximate']]
    assert len(curved) == 1 and len(inner) == 4
    assert curved[0]['curve_approximation_m'] == .001
    assert curved[0]['entity_ids'] and all(e['approximate'] for e in curved[0]['edge_evidence'])
    measured = service.room_area(request(r, c, basis='selected_exclusions', exclusion_contour_ids=[curved[0]['contour_id']]))
    assert measured['basis_area_m2'] == pytest.approx(100-math.pi, abs=.01)
    assert measured['approximate'] and 'tessellated_exclusion_contours' in measured['diagnostics']
    assert 'curved_boundaries_not_discovered' in r['diagnostics']


def test_touching_contour_is_relation_not_deductible_internal_hole(workspace):
    service, r, c = fixture(workspace, touching=True)
    touching = [o['contour_id'] for o in r['contours'] if o['candidate_area_m2'] == 4 and
                any(p[0] == 0 for p in o['boundary'])]
    assert touching and touching[0] in c['geometry_model']['boundary_intersection_contour_ids']
    assert touching[0] not in c['geometry_model']['interior_contour_ids']
    with pytest.raises(ValueError):
        service.room_area(request(r, c, basis='selected_exclusions', exclusion_contour_ids=touching))


def test_http_agent_and_reproducible_evidence(workspace):
    service, r, c = fixture(workspace)
    args = request(r, c, basis='topological_face')
    session = CadAgentSession(workspace, ChatRequest(snapshot_id=service.sid, question='按不同口径计算面积'))
    read = json.loads(session.invoke_tool('get_room_contours', dict(discovery_id=r['measurement_id'], candidate_id=c['candidate_id'])))
    assert read['observation_id'] and read['total'] == 3
    area = json.loads(session.invoke_tool('measure_room_area', args.model_dump()))
    assert area['observation_id'] and area['basis_area_m2'] == 87 and area['area_m2'] is None
    def claim(state):
        return json.dumps({'claims':[dict(text='拓扑面候选面积87平方米，净面积待确认。', state=state,
            evidence_basis='tool_observation', observation_ids=[area['observation_id']], observation_kind='measurement_result')]})
    with pytest.raises(ValueError): session.validate_answer(claim('observed'))
    assert session.validate_answer(claim('unknown'))['claims'][0]['state'] == 'unknown'
    with TestClient(create_app(workspace.settings)) as client:
        base = f'/api/drawings/{service.sid}/rooms'
        response = client.post(base+'/area', json=args.model_dump())
        assert response.status_code == 200
        assert response.json()['measurement_id'] == area['measurement_id']
        assert client.get(area['evidence_url']).json()['result_polygons'][0]['holes']
        assert 'topological_face' in client.get(area['preview_url']).text
        assert client.post(base+'/contours', json=dict(discovery_id=r['measurement_id'], candidate_id=c['candidate_id'])).json()['total'] == 3
