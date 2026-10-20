"""Selected-source geometric relations, independent of building-role acceptance.

Linear regions are exact native polygons, never bounding rectangles. Circular
sweeps are native ARC candidates with observed radial leaf support. Their curved
relations use explicit inscribed/circumscribed bounds, never unlabelled chords.
"""
from copy import deepcopy
import math
from typing import Literal

from ezdxf.math import OCS
from pydantic import Field
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union

from cad_agent.contracts import Record
from cad_agent.analysis.unit_policy import geometry_unit_resolution
from cad_agent.geometry.source import UNIT_TO_M
from cad_agent.geometry.spatial_boundaries import boundary_topology

VERSION = 'selected-source-relations-v1'


class MeasureSpatialRelation(Record):
    object_id: str = Field(min_length=1)
    other_object_id: str = Field(min_length=1)
    basis: Literal['closed_region', 'source_paths'] = 'closed_region'
    other_basis: Literal['closed_region', 'source_paths'] = 'closed_region'
    contour_id: str | None = None
    other_contour_id: str | None = None


class InspectDoorSweep(Record):
    object_id: str = Field(min_length=1)
    arc_entity_id: str | None = None


class MeasureDoorSweepRelation(InspectDoorSweep):
    other_object_id: str = Field(min_length=1)
    other_contour_id: str | None = None
    arc_segments: int = Field(default=256, ge=16, le=4096)


def _context(obj):
    if obj['role'].get('reference_only'):
        raise ValueError('SPATIAL_REFERENCE: reference-only legend/sample geometry cannot support placed-object relations.')
    return dict(kind='spatial_method', version=VERSION, object_id=obj['object_id'],
        **{k: obj[k] for k in ('source_sha256', 'snapshot_id', 'layout_id', 'frame_id', 'scope_id')},
        spatial_fact_scope=dict(basis='selected_source_geometry', identity_confirmed=False,
            engineering_confirmed=False, object_extent='unconfirmed', census='not_evaluated',
            role_status=obj['role']['status']), role=deepcopy(obj['role']),
        unit=geometry_unit_resolution(obj['unit']['value'], obj['unit'].get('policy')),
        source_entity_ids=[r['entity_id'] for r in obj['geometry']['records']],
        coverage=dict(selected_entities_only=True, whole_scope_enumerated=False,
            same_name_objects_merged=False), measurements={}, diagnostics=[],
        operation_receipts=[], next_calls=[], engineering_confirmed=False)


def _receipt(result, operation, fulfilled, basis):
    """Fulfil source-geometry operations without promoting semantic identity."""
    return dict(operation=operation, fulfilled=bool(fulfilled), basis=basis,
        scope='selected_source_geometry', identity_confirmed=False,
        engineering_confirmed=False, unit_status=result['unit']['unit_status'],
        object_id=result['object_id'], other_object_id=result.get('other_object_id'))


def _pair(a, b):
    result = _context(a); _context(b)
    for key in ('source_sha256', 'snapshot_id', 'layout_id', 'frame_id', 'scope_id'):
        if a[key] != b[key]:
            raise ValueError(f'SPATIAL_SCOPE: relation objects differ in {key}.')
    if a['object_id'] == b['object_id']:
        raise ValueError('SPATIAL_OBJECT: select two distinct source-bound objects.')
    if a['unit'] != b['unit']:
        raise ValueError('SPATIAL_UNIT: relation objects have inconsistent source units/policies.')
    result.update(other_object_id=b['object_id'], other_role=deepcopy(b['role']),
        other_source_entity_ids=[r['entity_id'] for r in b['geometry']['records']])
    return result


def _shape(obj, basis, contour_id=None):
    """Return source geometry and the precise selection basis, no guessed closure."""
    records = obj['geometry']['records']
    if any(r.get('layout_id') != obj['layout_id'] for r in records):
        raise ValueError('SPATIAL_LAYOUT: foreign-layout geometry.')
    if obj.get('exclusions', {}).get('records'):
        raise ValueError('SPATIAL_EXCLUSIONS: relation methods require an explicit region without unapplied exclusions.')
    if basis == 'source_paths':
        if contour_id:
            raise ValueError('SPATIAL_CONTOUR: contour selection is incompatible with source_paths.')
        if any(r.get('status') != 'ready' or not r.get('linear') or r.get('kind') in ('DIMENSION','HATCH') for r in records):
            raise ValueError('SPATIAL_GEOMETRY: exact source_paths requires complete native linear paths.')
        paths = [LineString(p) for r in records for p in r['paths'] if len(p) >= 2]
        if not paths or any(not g.is_valid or g.is_empty for g in paths):
            raise ValueError('SPATIAL_GEOMETRY: missing or invalid source paths.')
        return unary_union(paths), dict(basis=basis, extent='selected_source_paths')
    inspection, models = boundary_topology(obj)
    if not models or not inspection.get('source_geometry_complete'):
        raise ValueError('SPATIAL_BOUNDARY: selected entities do not form a supported closed region.')
    if contour_id is None:
        if len(models) != 1 or not inspection.get('selected_graph_fully_polygonized'):
            raise ValueError('SPATIAL_CONTOUR: ambiguous or partially open source graph; inspect and explicitly select a contour.')
        contour_id = next(iter(models))
    if contour_id not in models:
        raise ValueError('SPATIAL_CONTOUR: absent or stale contour.')
    contour = next(c for c in inspection['contours'] if c['contour_id'] == contour_id)
    if not contour['all_edges_source_supported']:
        raise ValueError('SPATIAL_SUPPORT: contour has unsupported intervals.')
    origin = inspection['origin']
    return translate(models[contour_id], xoff=origin[0], yoff=origin[1]), dict(
        basis='selected_topological_face', contour_id=contour_id,
        extent='selected_contour_only', hole_semantics='geometric_only',
        boundary_support=contour['boundary_support'])


def _physical(measurements, unit):
    factor = UNIT_TO_M.get(unit['unit'])
    measurements['conditional'] = unit['unit_status'] == 'assumption'
    for key, val in list(measurements.items()):
        if key.endswith('_drawing_units_squared'):
            measurements[key.replace('_drawing_units_squared', '_m2')] = None if factor is None else val * factor * factor
        elif key.endswith('_drawing_units'):
            measurements[key.replace('_drawing_units', '_m')] = None if factor is None else val * factor
    return measurements


def spatial_relation(a, b, args):
    result = _pair(a, b)
    ga, sa = _shape(a, args.basis, args.contour_id)
    gb, sb = _shape(b, args.other_basis, args.other_contour_id)
    # Translation reduces floating-point loss without changing relative positions.
    ox, oy = ga.bounds[:2]
    x, y = translate(ga, -ox, -oy), translate(gb, -ox, -oy)
    cross = x.intersection(y)
    p, q = nearest_points(x, y)
    regions = args.basis == args.other_basis == 'closed_region'
    result.update(status='source_geometry_measured', measurement_basis='native_linear_geometry_exact_topology',
        geometry_selections=[sa, sb],
        relations=dict(intersects=x.intersects(y), disjoint=x.disjoint(y), touches=x.touches(y),
            overlaps=x.overlaps(y), equals=x.equals(y), a_covers_b=x.covers(y), b_covers_a=y.covers(x),
            a_contains_b_strictly=x.contains_properly(y), b_contains_a_strictly=y.contains_properly(x),
            positive_area_overlap=cross.area > 0 if regions else None,
            bbox_intersects_broadphase_only=box(*x.bounds).intersects(box(*y.bounds))),
        measurements=_physical(dict(minimum_distance_drawing_units=x.distance(y),
            intersection_area_drawing_units_squared=cross.area,
            intersection_length_drawing_units=cross.length), result['unit']),
        nearest_source_points=[[p.x+ox, p.y+oy], [q.x+ox, q.y+oy]],
        distance_interpretation='selected_region_clearance' if regions else 'selected_source_path_distance_not_object_clearance')
    result['operation_receipts']=[
        _receipt(result, op, True,
            'exact_selected_source_region_relation' if regions else 'exact_selected_source_path_relation_not_object_clearance')
        for op in ('geometry', 'relations')]
    return result


def _arc_geometry(rec):
    native = rec.get('native_parameters', rec.get('native', {}))
    try:
        radius = float(native['radius']); start = math.radians(float(native['start_angle']))
        sweep = math.radians((float(native['end_angle']) - float(native['start_angle'])) % 360)
        extrusion = native.get('extrusion', [0, 0, 1]); ocs = OCS(extrusion)
        if abs(ocs.uz.x) > 1e-12 or abs(ocs.uz.y) > 1e-12:
            raise ValueError('nonplanar_arc')
        center = ocs.to_wcs(native['center'])
        # OCS vectors preserve mirrored ARC directions, including extrusion -Z.
        ux, uy = ocs.ux, ocs.uy
        if radius <= 0 or not 0 < sweep < 2*math.pi or not all(math.isfinite(v) for v in (*center, radius, start, sweep)):
            raise ValueError('degenerate_arc')
        def point(t, r=radius):
            return [center.x+r*(ux.x*math.cos(t)+uy.x*math.sin(t)),
                    center.y+r*(ux.y*math.cos(t)+uy.y*math.sin(t))]
        return dict(center=[center.x, center.y], radius=radius, start=start, sweep=sweep, point=point)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError('SPATIAL_ARC: incomplete, nonplanar or invalid native ARC parameters.') from exc


def door_sweep(obj, args):
    result = _context(obj)
    result.update(status='unsupported_door_sweep', measurement_basis='native_arc_with_source_radial_leaf',
        door_identity='unconfirmed', sweep_candidates=[],
        operation_receipts=[_receipt(result, 'geometry', False, 'missing_supported_native_sweep')])
    records = obj['geometry']['records']
    if obj['role']['value'] not in ('door', 'door_swing', 'door_sweep', 'opening'):
        result['diagnostics'].append(dict(code='door_role_not_asserted')); return result, []
    if any(r.get('layout_id') != obj['layout_id'] or r.get('status') != 'ready' for r in records):
        raise ValueError('SPATIAL_SOURCE: foreign-layout or incomplete door geometry.')
    arcs = [r for r in records if r.get('kind') == 'ARC' and (not args.arc_entity_id or r['entity_id'] == args.arc_entity_id)]
    lines = [(r['entity_id'], LineString([a, b])) for r in records if r.get('linear') and r.get('kind') not in ('DIMENSION', 'HATCH')
             for path in r['paths'] for a, b in zip(path, path[1:]) if a != b]
    supported = []
    for rec in arcs:
        try: arc = _arc_geometry(rec)
        except ValueError:
            result['diagnostics'].append(dict(code='unsupported_native_arc', entity_id=rec['entity_id'])); continue
        center, radius = arc['center'], arc['radius']
        epsilon = max(math.ulp(max(abs(v) for v in center) or 1.)*64, radius*1e-12)
        evidence=[]
        for t in [arc['start'], arc['start']+arc['sweep']]:
            tip=arc['point'](t); radial=LineString([center, tip])
            # Full observed radius coverage, possibly assembled from several native lines.
            parts=[(eid,line) for eid,line in lines if radial.buffer(epsilon, cap_style=3).covers(line)]
            if parts and unary_union([line.buffer(epsilon, cap_style=3) for _,line in parts]).covers(radial):
                evidence.append(dict(tip=tip, leaf_entity_ids=sorted({eid for eid,_ in parts})))
        if not evidence:
            result['diagnostics'].append(dict(code='native_arc_without_supported_hinge_and_radial_leaf', entity_id=rec['entity_id'])); continue
        public=dict(arc_entity_id=rec['entity_id'], hinge=center, endpoints=[arc['point'](arc['start']), arc['point'](arc['start']+arc['sweep'])],
            radial_leaf_support=evidence, identity='native_arc_radial_leaf_candidate_not_confirmed_door',
            measurements=_physical(dict(radius_drawing_units=radius, sweep_angle_degrees=math.degrees(arc['sweep']),
                sector_area_drawing_units_squared=radius*radius*arc['sweep']/2,
                numeric_support_epsilon_drawing_units=epsilon), result['unit']))
        result['sweep_candidates'].append(public); supported.append((public, arc))
    if supported:
        result['status']='source_sweep_candidate_measured'
        result['measurements']=[dict(arc_entity_id=p['arc_entity_id'], **p['measurements']) for p,_ in supported]
        result['operation_receipts']=[_receipt(result, 'geometry', True,
            'native_arc_radial_leaf_geometry_candidate_not_confirmed_door')]
        if len(supported) > 1:
            result['next_calls']=[dict(tool='inspect_door_sweep',
                arguments=dict(object_id=obj['object_id'], arc_entity_id=p['arc_entity_id']),
                purpose='Inspect one exact supported ARC candidate; semantic door identity remains unconfirmed.')
                for p,_ in supported]
    if not arcs: result['diagnostics'].append(dict(code='no_selected_native_arc'))
    return result, supported


def door_sweep_relation(a, b, args):
    result = _pair(a, b)
    inspection, candidates = door_sweep(a, args)
    result.update(measurement_basis='native_circular_sector_inscribed_circumscribed_bounds',
        door_identity='unconfirmed', diagnostics=inspection['diagnostics'])
    if len(candidates) != 1:
        result.update(status='unsupported_door_sweep', reason='Select one source-supported native ARC; no door arc is inferred.',
            operation_receipts=[_receipt(result, op, False, 'no_unique_supported_native_sweep') for op in ('geometry', 'relations')],
            next_calls=inspection['next_calls'])
        return result
    target, selection = _shape(b, 'closed_region', args.other_contour_id)
    public, arc = candidates[0]; n=args.arc_segments
    dt=arc['sweep']/n; start=arc['start']; center=arc['center']
    inscribed=Polygon([center]+[arc['point'](start+i*dt) for i in range(n+1)]+[center])
    # Each tangent intersection lies outside the circle, yielding a true outer
    # envelope without assuming an arbitrary physical tessellation tolerance.
    outer_pts=[center,arc['point'](start)]
    outer_pts += [arc['point'](start+(i+.5)*dt, arc['radius']/math.cos(dt/2)) for i in range(n)]
    outer_pts += [arc['point'](start+arc['sweep']),center]
    circumscribed=Polygon(outer_pts)
    low=inscribed.intersection(target).area; high=circumscribed.intersection(target).area
    relation = 'positive_area_intersection' if low > 0 else 'disjoint' if circumscribed.disjoint(target) else 'boundary_uncertain_within_arc_bounds'
    result.update(status='source_geometry_bounded', sweep_candidate=public, target_selection=selection,
        relation=relation, measurement_exact=False,
        measurements=_physical(dict(intersection_area_lower_drawing_units_squared=low,
            intersection_area_upper_drawing_units_squared=high,
            minimum_distance_lower_drawing_units=circumscribed.distance(target),
            minimum_distance_upper_drawing_units=inscribed.distance(target),
            arc_radial_bound_drawing_units=arc['radius']*(1/math.cos(dt/2)-1),
            sector_area_drawing_units_squared=arc['radius']**2*arc['sweep']/2), result['unit']),
        containment=dict(sweep_contains_target_proven=inscribed.contains_properly(target),
            target_contains_sweep_proven=target.contains_properly(circumscribed)),
        curve_bounds=dict(segments=n, inner='inscribed_chords', outer='tangent_intersections',
            touching='not_asserted_from_tessellation'))
    result['operation_receipts']=[
        _receipt(result, 'geometry', True, 'native_circular_sector_measurement_bounds'),
        _receipt(result, 'relations', relation in ('positive_area_intersection', 'disjoint'),
            'bounded_sector_relation_proven' if relation in ('positive_area_intersection', 'disjoint')
            else 'curved_boundary_contact_unresolved')]
    if relation == 'boundary_uncertain_within_arc_bounds' and n < 4096:
        refined=args.model_dump()
        refined.update(arc_entity_id=public['arc_entity_id'], arc_segments=min(n*2,4096))
        result['next_calls']=[dict(tool='measure_door_sweep_relation', arguments=refined,
            purpose='Refine the same source-supported curved bounds; exact contact may remain unresolved.')]
    return result


def measure_spatial_relation(session, args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement
    a=resolve_object_input(session,args.object_id); b=resolve_object_input(session,args.other_object_id)
    result=spatial_relation(a,b,args)
    record_measurement(session,args.object_id,result); record_measurement(session,args.other_object_id,result)
    return result


def inspect_door_sweep(session, args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement
    obj=resolve_object_input(session,args.object_id)
    result,_=door_sweep(obj,args); record_measurement(session,args.object_id,result)
    return result


def measure_door_sweep_relation(session, args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement
    a=resolve_object_input(session,args.object_id); b=resolve_object_input(session,args.other_object_id)
    result=door_sweep_relation(a,b,args)
    record_measurement(session,args.object_id,result); record_measurement(session,args.other_object_id,result)
    return result
