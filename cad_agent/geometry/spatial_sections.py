"""Source-bound local cross sections and conservative elevation annotation pairs.

The axis is a parameterization validated against selected native edges. Samples
are never promoted to a global minimum, and selected faces never certify a wall,
finish, complete obstacle census or engineering clearance.
"""
from copy import deepcopy
from decimal import Decimal
import math
import re

from pydantic import Field, FiniteFloat
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from cad_agent.contracts import Record
from cad_agent.analysis.unit_policy import user_unit_policy
from cad_agent.geometry.source import UNIT_TO_M
from cad_agent.geometry.spatial_relations import _context, _pair, _shape, _physical

VERSION = 'selected-source-sections-v1'


class MeasureSpatialSections(Record):
    object_id: str = Field(min_length=1)
    other_object_id: str | None = Field(default=None, description='Second explicitly selected opposing face. Omit for a closed corridor region.')
    axis_origin: list[FiniteFloat] = Field(min_length=2, max_length=2)
    axis_direction: list[FiniteFloat] = Field(min_length=2, max_length=2)
    stations: list[FiniteFloat] | None = Field(default=None, min_length=1, max_length=256,
        description='Drawing-unit distances from origin along the normalized axis. Omit to sample vertex-event cell midpoints.')
    contour_id: str | None = None
    obstacle_object_ids: list[str] = Field(default_factory=list, max_length=32)


class MeasureElevationRelation(Record):
    object_id: str = Field(min_length=1)
    lower_label_region_id: str = Field(min_length=1)
    upper_label_region_id: str = Field(min_length=1)
    user_datum_quote: str = Field(default='', max_length=300,
        description='Exact current question clause explicitly assuming the same datum for these two selected elevations.')
    user_view_quote: str = Field(default='', max_length=300,
        description='Exact current question clause explicitly assuming the same section/view for these two selected elevations.')


def _parts(shape):
    return list(shape.geoms) if hasattr(shape, 'geoms') else ([] if shape.is_empty else [shape])


def _vertices(shape):
    if shape.geom_type == 'Polygon':
        return list(shape.exterior.coords) + [p for ring in shape.interiors for p in ring.coords]
    if hasattr(shape, 'geoms'):
        return [p for child in shape.geoms for p in _vertices(child)]
    return list(shape.coords)


def _axis(args, records):
    dx, dy = args.axis_direction
    length = math.hypot(dx, dy)
    if length <= 0:
        raise ValueError('SPATIAL_AXIS: direction must be nonzero.')
    ux, uy = dx / length, dy / length
    support = []
    for record in records:
        for pi, path in enumerate(record.get('paths', [])):
            for si, (a, b) in enumerate(zip(path, path[1:])):
                vx, vy = b[0] - a[0], b[1] - a[1]
                span = math.hypot(vx, vy)
                # Dimensionless numerical alignment only; no physical tolerance.
                if span and abs(vx * uy - vy * ux) <= span * 1e-10:
                    support.append(dict(entity_id=record['entity_id'], path_index=pi, segment_index=si))
    if not support:
        raise ValueError('SPATIAL_AXIS: direction is not parallel to any selected native boundary segment.')
    ox, oy = args.axis_origin
    return (ux, uy), [ux, uy, -uy, ux, -ox * ux - oy * uy, ox * uy - oy * ux], support


def _one_crossing(shape, station, reach):
    cross = shape.intersection(LineString([(station, -reach), (station, reach)]))
    parts = _parts(cross)
    if not parts or any(part.geom_type != 'Point' for part in parts):
        return None
    values = sorted(set(float(part.y) for part in parts))
    return values[0] if len(values) == 1 else None


def spatial_sections(obj, other, obstacles, args):
    result = _pair(obj, other) if other else _context(obj)
    result.update(version=VERSION, status='unsupported_sections', sections=[], operation_receipts=[],
        measurement_basis='native_linear_cross_sections_at_selected_stations',
        global_minimum_proven=False, obstacle_census='selected_objects_only',
        width_interpretation='sampled_source_bounded_axis_component_not_confirmed_structural_or_finish_width')
    if other and args.contour_id:
        raise ValueError('SPATIAL_CONTOUR: opposing source faces do not take a region contour.')
    primary, selection = _shape(obj, 'source_paths' if other else 'closed_region', args.contour_id)
    secondary = _shape(other, 'source_paths')[0] if other else None
    records = obj['geometry']['records'] + (other['geometry']['records'] if other else [])
    direction, matrix, support = _axis(args, records)
    primary = affine_transform(primary, matrix)
    secondary = affine_transform(secondary, matrix) if secondary is not None else None
    blocks = []
    for obstacle in obstacles:
        _pair(obj, obstacle)
        shape, source = _shape(obstacle, 'closed_region')
        blocks.append((obstacle, affine_transform(shape, matrix), source))
    low, high = primary.bounds[0], primary.bounds[2]
    if secondary is not None:
        low, high = max(low, secondary.bounds[0]), min(high, secondary.bounds[2])
    shapes = [primary] + ([secondary] if secondary is not None else []) + [b[1] for b in blocks]
    reach = max(1., max(abs(p[1]) for shape in shapes for p in _vertices(shape))) * 2
    result.update(axis=dict(origin=list(args.axis_origin), direction=list(direction),
        source_direction_support=support, origin_validation='pending'),
        geometry_selection=selection,
        selected_obstacles=[dict(object_id=o['object_id'], source_entity_ids=[r['entity_id'] for r in o['geometry']['records']],
                                 selection=s) for o, _, s in blocks],
        longitudinal_overlap_drawing_units=[low, high])
    result['spatial_fact_scope'].update(extent='sampled_sections_only', net_boundary='unconfirmed',
        whole_path_minimum='not_evaluated', obstacle_coverage='not_enumerated')
    def finish():
        result['operation_receipts'] = [dict(operation='geometry', fulfilled=bool(result['measurements']),
            basis='sampled_native_cross_sections', scope='selected_source_geometry',
            global_minimum_proven=False, engineering_confirmed=False)]
        return result
    if high <= low:
        result['diagnostics'].append(dict(code='no_longitudinal_overlap'))
        return finish()
    # The origin must be in the actual corridor, not merely a bounding box.
    if not low < 0 < high:
        result['diagnostics'].append(dict(code='axis_origin_outside_longitudinal_interior'))
        return finish()
    if secondary is not None:
        crossings = [_one_crossing(s, 0., reach) for s in (primary, secondary)]
        valid_origin = all(v is not None for v in crossings) and min(crossings) < 0 < max(crossings)
    else:
        valid_origin = primary.contains(Point(0., 0.))
    if not valid_origin:
        result['diagnostics'].append(dict(code='axis_origin_not_between_unique_source_faces'))
        return finish()
    result['axis']['origin_validation'] = 'inside_native_corridor_at_origin'
    if args.stations is None:
        events = sorted(set([low, high] + [p[0] for shape in shapes for p in _vertices(shape) if low < p[0] < high]))
        stations = [(a + b) / 2 for a, b in zip(events, events[1:]) if b > a]
        if len(stations) > 256:
            raise ValueError('SPATIAL_BUDGET: too many vertex events; explicitly select at most 256 stations.')
        result['station_basis'] = 'vertex_event_cell_midpoints_sampled_not_extrema'
    else:
        stations = sorted(set(args.stations))
        result['station_basis'] = 'explicit_sample_stations'
    all_blocks = unary_union([b[1] for b in blocks]) if blocks else None
    ux, uy = direction
    ox, oy = args.axis_origin
    world = lambda station, across: [ox + ux * station - uy * across, oy + uy * station + ux * across]
    measured = []
    for station in stations:
        row = dict(station_drawing_units=station, status='unsupported', source_entity_ids=result['source_entity_ids'])
        result['sections'].append(row)
        if not low < station < high:
            row['reason'] = 'outside_common_longitudinal_interior'
            continue
        cut = LineString([(station, -reach), (station, reach)])
        if secondary is not None:
            ys = [_one_crossing(s, station, reach) for s in (primary, secondary)]
            if any(v is None for v in ys) or not min(ys) < 0 < max(ys):
                row['reason'] = 'missing_ambiguous_or_nonopposing_source_faces'
                continue
            baseline = LineString([(station, min(ys)), (station, max(ys))])
            row['other_source_entity_ids'] = result['other_source_entity_ids']
        else:
            components = [s for s in _parts(primary.intersection(cut)) if s.geom_type == 'LineString' and s.bounds[1] < 0 < s.bounds[3]]
            if len(components) != 1:
                row['reason'] = 'no_unique_corridor_component_through_axis'
                continue
            baseline = components[0]
        active = [o['object_id'] for o, s, _ in blocks if baseline.intersects(s)]
        row['active_obstacle_object_ids'] = active
        if all_blocks is not None and all_blocks.covers(Point(station, 0)):
            row['reason'] = 'selected_obstacle_blocks_axis'
            continue
        free = baseline.difference(all_blocks) if all_blocks is not None else baseline
        components = [s for s in _parts(free) if s.geom_type == 'LineString' and s.bounds[1] < 0 < s.bounds[3]]
        if len(components) != 1:
            row['reason'] = 'no_unique_free_component_through_axis'
            continue
        component = components[0]
        a, b = component.bounds[1], component.bounds[3]
        row.update(status='source_geometry_sampled', endpoints=[world(station, a), world(station, b)],
            measurements=_physical(dict(width_drawing_units=b-a, baseline_width_drawing_units=baseline.length,
                selected_obstacle_reduction_drawing_units=baseline.length-(b-a)), result['unit']))
        measured.append(dict(station_drawing_units=station, **row['measurements']))
    result['measurements'] = dict(samples=measured) if measured else {}
    result['sample_coverage'] = dict(requested=len(stations), measured=len(measured),
        unsupported=len(stations)-len(measured), whole_path_enumerated=False)
    if measured:
        result.update(status='source_geometry_sampled',
            sampled_minimum=_physical(dict(width_drawing_units=min(s['width_drawing_units'] for s in measured)), result['unit']))
    return finish()


# Strict self-contained annotations only. A drawing-header length unit does not
# establish the unit of elevation text, nor does proximity establish its datum.
_NUMBER = re.compile(r'(?<![A-Za-z0-9_.+\-−±])([+\-−]?\d+(?:\.\d+)?|±0(?:\.0+)?)(?![\d.]|[²³])')
_UNIT = re.compile(r'^\s*(mm|cm|m|毫米|厘米|米)(?![a-zA-Z²³23])')
_UNIT_NAMES = {'毫米': 'mm', '厘米': 'cm', '米': 'm', 'mm': 'mm', 'cm': 'cm', 'm': 'm'}


def _annotation(label):
    text = label['text']
    metadata_matches = list(re.finditer(
        r'(基准|datum|视图|剖面|view)\s*[:：=]\s*([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]*)(?=\s|[;；,，。]|$)', text, re.I))
    # Titles, dimensions, ranges, and multi-value annotations require further
    # source disambiguation. No selection of a convenient number is performed.
    tokens = [t for t in _NUMBER.finditer(text) if not any(m.start() <= t.start() < m.end() for m in metadata_matches)]
    if len(tokens) != 1 or re.search(r'平面图|剖面图|详图|[:：]\s*\d+\s*[/：:]\s*\d+', text):
        return dict(status='ambiguous_annotation', text=text, region_id=label['region_id'])
    token = tokens[0]
    # Bare signed decimals are retained as candidates; unsigned bare integers
    # are not sufficient evidence of elevation notation.
    if not re.search('标高|elevation', text, re.I) and not re.fullmatch(r'\s*[+\-−±]\d+(?:\.\d+)?\s*(?:mm|cm|m|毫米|厘米|米)?\s*', text):
        return dict(status='not_explicit_elevation', text=text, region_id=label['region_id'])
    value = Decimal(token.group(1).replace('−', '-').replace('±', ''))
    suffix = _UNIT.match(text[token.end():])
    roles = [role for role, pattern in [('structural', r'结构'), ('finished', r'完成面|建筑标高'), ('outdoor_ground', r'室外地坪')] if re.search(pattern, text)]
    def metadata(key):
        matches = [m.group(2) for m in metadata_matches if re.fullmatch(key, m.group(1), re.I)]
        return matches[0] if len(set(matches)) == 1 else None
    datum = metadata(r'(?:基准|datum)')
    if '绝对标高' in text:
        datum = datum or 'absolute_vertical_reference_unspecified'
    return dict(status='source_annotation_candidate', region_id=label['region_id'], entity_ids=label.get('entity_ids', []),
        text=text, number_span=list(token.span(1)), raw=token.group(1), value=str(value),
        unit=_UNIT_NAMES[suffix.group(1)] if suffix else 'unknown', datum=datum,
        view=metadata(r'(?:视图|剖面|view)'), role=roles[0] if len(roles) == 1 else 'unknown',
        role_basis='source_wording_only', same_location='unconfirmed')


def _assumption(quote, question, role):
    if not quote:
        return None
    patterns = {'datum': r'(?:同一|共同|相同)基准', 'view': r'同一(?:剖面|视图)'}
    if quote not in question or not re.search(r'假设|假定|暂按', quote) or not re.search(patterns[role], quote) or re.search(r'不要|不应|不得|不能|不假设|不假定', quote):
        raise ValueError(f'SPATIAL_USER_CONDITION: {role} needs a literal current-question assumption, not an instruction to verify.')
    return dict(quote=quote, origin='user_question', status='conditional', value='same_' + role)


def elevation_relation(obj, args, question=''):
    result = _context(obj)
    result.update(version=VERSION, status='missing_elevation_basis', measurement_basis='source_annotation_difference',
        operation_receipts=[], measurements={}, diagnostics=[], height_interpretation='annotation_difference_only',
        same_location='unconfirmed', layer_height=None, clear_height=None, cover_depth=None)
    labels = {l['region_id']: l for l in obj.get('label', []) if 'region_id' in l}
    if args.lower_label_region_id == args.upper_label_region_id:
        raise ValueError('SPATIAL_ANNOTATION: select two distinct source annotations.')
    if not all(r in labels for r in (args.lower_label_region_id, args.upper_label_region_id)):
        raise ValueError('SPATIAL_ANNOTATION: both labels must be bound to this object.')
    raw_labels = [labels[r] for r in (args.lower_label_region_id, args.upper_label_region_id)]
    for label in raw_labels:
        if any(label.get(k, obj[k]) != obj[k] for k in ('layout_id', 'frame_id')):
            raise ValueError('SPATIAL_SCOPE: annotation crosses layout/frame.')
    a, b = [_annotation(l) for l in raw_labels]
    result['annotations'] = [a, b]
    result['source_label_evidence'] = deepcopy(raw_labels)
    datum_assumption = _assumption(args.user_datum_quote, question, 'datum')
    view_assumption = _assumption(args.user_view_quote, question, 'view')
    result['user_conditions'] = [r for r in (datum_assumption, view_assumption) if r]
    def finish():
        result['operation_receipts'] = [dict(operation='relations', fulfilled=bool(result['measurements']),
            basis='source_annotation_difference', scope='selected_source_annotations', engineering_confirmed=False),
            dict(operation='arithmetic', fulfilled=bool(result['measurements']),
                 conditional=result['measurements'].get('conditional', False), scope='selected_source_annotation_difference')]
        return result
    if any(a['status'] != 'source_annotation_candidate' for a in (a, b)):
        result['diagnostics'].append(dict(code='missing_or_ambiguous_elevation_value'))
        return finish()
    conditional = False
    for key, assumption in [('datum', datum_assumption), ('view', view_assumption)]:
        known = [a.get(key), b.get(key)]
        if all(known) and known[0] != known[1]:
            result.update(status='conflicting_elevation_basis')
            result['diagnostics'].append(dict(code='different_' + key))
            return finish()
        # An absolute reference without its geodetic identity is not a datum.
        established = all(known) and known[0] != 'absolute_vertical_reference_unspecified'
        if not established and not assumption:
            result['diagnostics'].append(dict(code='missing_common_' + key))
        elif not established:
            conditional = True
        result[key + '_status'] = 'source_explicit' if established else 'conditional' if assumption else 'missing'
    # A separate explicit elevation-unit clause may be a user assumption. Generic
    # drawing coordinate units cannot establish annotation units.
    annotation_policy = user_unit_policy('。'.join(c for c in re.split(r'[。；;\n]', question)
        if re.search(r'标高(?:数值|数|的)?(?:单位|.*?单位)|标高(?:以|按)', c)))
    units = [a['unit'], b['unit']]
    for index, unit in enumerate(units):
        if unit == 'unknown' and annotation_policy and annotation_policy['condition'] == 'explicit':
            units[index] = annotation_policy['unit']
            conditional = True
    result['annotation_unit_policy'] = annotation_policy
    if 'unknown' in units:
        result['diagnostics'].append(dict(code='missing_annotation_unit'))
    if result['diagnostics']:
        return finish()
    lower = Decimal(a['value']) * Decimal(str(UNIT_TO_M[units[0]]))
    upper = Decimal(b['value']) * Decimal(str(UNIT_TO_M[units[1]]))
    if upper < lower:
        result.update(status='conflicting_elevation_basis')
        result['diagnostics'].append(dict(code='upper_value_below_lower_value'))
        return finish()
    result.update(status='conditional_annotation_difference' if conditional else 'source_annotation_difference',
        measurements=dict(lower_elevation_m=float(lower), upper_elevation_m=float(upper),
            elevation_difference_m=float(upper-lower), conditional=conditional),
        formula=dict(operation='upper_minus_lower', operand_region_ids=[b['region_id'], a['region_id']]))
    result['spatial_fact_scope'].update(same_location='unconfirmed', building_role='unconfirmed',
        elevation_semantics='source_wording_only', physical_height='not_confirmed')
    return finish()


def measure_spatial_sections(session, args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement
    obj = resolve_object_input(session, args.object_id)
    other = resolve_object_input(session, args.other_object_id) if args.other_object_id else None
    if len(set(args.obstacle_object_ids)) != len(args.obstacle_object_ids) or set(args.obstacle_object_ids) & {args.object_id, args.other_object_id}:
        raise ValueError('SPATIAL_OBJECT: obstacle objects must be distinct from faces and one another.')
    obstacles = [resolve_object_input(session, oid) for oid in args.obstacle_object_ids]
    result = spatial_sections(obj, other, obstacles, args)
    for source in [obj] + ([other] if other else []) + obstacles:
        record_measurement(session, source['object_id'], result)
    return result


def measure_elevation_relation(session, args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement, _labels, _scope
    obj = resolve_object_input(session, args.object_id)
    # resolve_object_input rechecks paths. Recheck native label text as well so a
    # cached or edited object registry cannot fabricate an annotation source.
    scope = _scope(session, obj['scope_id'])
    current = _labels(session, scope, [l['region_id'] for l in obj['label']], obj['geometry']['records'])
    if current != obj['label']:
        raise ValueError('SPATIAL_STALE: native annotation labels changed; rebind the object.')
    result = elevation_relation(obj, args, session.request.question)
    record_measurement(session, args.object_id, result)
    return result
