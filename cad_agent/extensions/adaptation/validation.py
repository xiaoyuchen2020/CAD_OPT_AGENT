"""Host-owned validation and input derivation. No generated imports or exec."""
import copy
import math
from collections import Counter
from shapely.geometry import LineString, box
from shapely.ops import polygonize, unary_union
from ..table_recovery.bundle import digest, verify_bundle, encoded
from ..table_recovery.contracts import Candidate, MAX_INPUT
from ..table_recovery.validator import validate as validate_grid
from .contracts import SPECS


def derive_grid(bundle, raw):
    candidate = Candidate.model_validate(raw, strict=True)
    if candidate.input_id != bundle['input_id']:
        raise ValueError('INPUT_ID_MISMATCH')
    if candidate.status != 'candidate':
        return bundle, raw
    xs, ys = candidate.xs, candidate.ys
    a, b, c, d = bundle['bbox']; tol = bundle['tolerance']
    if (len(xs) < 2 or len(ys) < 2 or (len(xs)-1)*(len(ys)-1) > 4000
        or xs[0] != a or xs[-1] != c or ys[0] != d or ys[-1] != b
        or any(v <= u for u, v in zip(xs, xs[1:]))
        or any(v >= u for u, v in zip(ys, ys[1:]))):
        raise ValueError('INVALID_GRID_AXES')
    proof = []
    for axis, anchors, original in ((1, xs, bundle['xs']), (0, ys, bundle['ys'])):
        other = 1-axis
        for coordinate in anchors:
            if coordinate in original:
                continue
            ids = [s['segment_id'] for s in bundle['segments']
                   if max(abs(s['a'][other]-coordinate), abs(s['b'][other]-coordinate)) <= tol]
            if not ids:
                raise ValueError('ANCHOR_WITHOUT_NATIVE_SOURCE')
            proof.append(dict(axis=axis, coordinate=coordinate, segment_ids=ids))
    derived = copy.deepcopy(bundle)
    derived.pop('input_id'); derived.pop('bundle_sha256')
    derived.update(xs=xs, ys=ys, derivation=dict(parent_input_id=bundle['input_id'],
        builder='native-anchor-derivation@1', anchors=proof))
    sha = digest(derived); derived.update(input_id='tri_'+sha, bundle_sha256=sha)
    if len(encoded(derived)) > MAX_INPUT:
        raise ValueError('INPUT_LIMIT_EXCEEDED')
    return derived, dict(raw, input_id=derived['input_id'])


def validate(method, bundle, raw):
    verify_bundle(bundle)
    try:
        candidate_sha = digest(raw)
    except (ValueError, TypeError):
        return dict(validator_version=SPECS[method]['validator'], validation_status='rejected',
                    candidate_sha256=None, errors=['INVALID_OUTPUT_SCHEMA'], warnings=[], cells=[], items=[],
                    review_status='pending', semantic_acceptance=False)
    report = dict(validator_version=SPECS[method]['validator'], input_id=bundle['input_id'],
        candidate_sha256=candidate_sha, validation_status='rejected', errors=[], warnings=[],
        review_status='pending', semantic_acceptance=False, cells=[], items=[])
    try:
        if bundle['coverage']['blocking_diagnostics']:
            raise ValueError('INPUT_INCOMPLETE_OR_UNSUPPORTED')
        if method == 'table_grid':
            derived, candidate = derive_grid(bundle, raw)
            validated = validate_grid(derived, candidate)
            validated.update(candidate_sha256=digest(raw), derived_input=derived,
                             source_candidate_sha256=digest(raw), validated_candidate_sha256=digest(candidate))
            return validated
        if not isinstance(raw, dict) or raw.get('input_id') != bundle['input_id']:
            raise ValueError('INPUT_ID_MISMATCH')
        if raw.keys() != ({'input_id', 'segment_ids', 'area'} if method == 'closed_contour' else {'input_id', 'items'}):
            raise ValueError('INVALID_OUTPUT_SCHEMA')
        texts = {t['entity_id']: t for t in bundle['texts']}
        segments = {s['segment_id']: s for s in bundle['segments']}
        if method in ('text_spans', 'segment_measurements', 'segment_distances'):
            items = raw['items']
            if not isinstance(items, list) or not 0 < len(items) <= 5000:
                raise ValueError('EMPTY_OR_EXCESSIVE_OUTPUT')
            seen = set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError('INVALID_OUTPUT_SCHEMA')
                if method == 'text_spans':
                    if item.keys() != {'entity_id', 'start', 'end'}:
                        raise ValueError('INVALID_OUTPUT_SCHEMA')
                    t = texts[item['entity_id']]; start, end = item['start'], item['end']
                    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(t['text']):
                        raise ValueError('INVALID_SOURCE_SPAN')
                    if not t['evidence']:
                        raise ValueError('SOURCE_EVIDENCE_MISSING')
                    key = (item['entity_id'], start, end)
                    result = dict(item, quote=t['text'][start:end], entity_path=t['entity_path'],
                                  bbox=t['bbox'], evidence=t['evidence'])
                elif method == 'segment_distances':
                    if item.keys() != {'segment_ids', 'distance'}:
                        raise ValueError('INVALID_OUTPUT_SCHEMA')
                    ids = item['segment_ids']
                    if not isinstance(ids, list) or len(ids) != 2 or any(type(i) is not str for i in ids) or ids[0] == ids[1]:
                        raise ValueError('INVALID_DISTANCE_PAIR')
                    selected = [segments[i] for i in ids]
                    distance = LineString([selected[0]['a'], selected[0]['b']]).distance(LineString([selected[1]['a'], selected[1]['b']]))
                    value = item['distance']
                    if type(value) not in (float, int) or not math.isfinite(value) or abs(distance-value) > max(1e-8, distance*1e-9):
                        raise ValueError('DISTANCE_MISMATCH')
                    key = tuple(sorted(ids))
                    result = dict(item, distance=distance, unit='drawing_unit', basis='whole_native_segments',
                        entity_paths=[s['entity_path'] for s in selected],
                        segments=[dict(segment_id=s['segment_id'],a=s['a'],b=s['b']) for s in selected])
                else:
                    if item.keys() != {'segment_id', 'length'}:
                        raise ValueError('INVALID_OUTPUT_SCHEMA')
                    s = segments[item['segment_id']]; value = item['length']
                    length = LineString([s['a'], s['b']]).length
                    if type(value) not in (float, int) or not math.isfinite(value) or abs(length-value) > max(1e-8, length*1e-9):
                        raise ValueError('MEASUREMENT_MISMATCH')
                    key = item['segment_id']
                    result = dict(item, length=length, unit='drawing_unit', basis='whole_native_segment', entity_path=s['entity_path'],
                                  a=s['a'], b=s['b'])
                if key in seen:
                    raise ValueError('DUPLICATE_OUTPUT')
                seen.add(key); report['items'].append(result)
        else:
            ids = raw['segment_ids']
            if not isinstance(ids, list) or not 3 <= len(ids) <= 2000 or len(set(ids)) != len(ids):
                raise ValueError('INVALID_CONTOUR_SEGMENTS')
            selected = [segments[sid] for sid in ids]
            lines = [LineString([s['a'], s['b']]) for s in selected]
            merged = unary_union(lines); polygons = list(polygonize(merged))
            if len(polygons) != 1 or polygons[0].interiors or not polygons[0].is_valid:
                raise ValueError('NOT_SINGLE_SIMPLE_CLOSED_CONTOUR')
            polygon = polygons[0]
            if merged.difference(polygon.boundary).length > bundle['tolerance']:
                raise ValueError('CONTOUR_HAS_EXTRA_GEOMETRY')
            if not box(*bundle['bbox']).buffer(bundle['tolerance']).covers(polygon):
                raise ValueError('CONTOUR_OUTSIDE_SCOPE')
            area = raw['area']
            if type(area) not in (float, int) or not math.isfinite(area) or abs(area-polygon.area) > max(1e-8, polygon.area*1e-9):
                raise ValueError('AREA_MISMATCH')
            report['items'] = [dict(segment_ids=ids, area=polygon.area, unit='drawing_unit_squared',
                bbox=list(polygon.bounds), vertices=list(map(list, polygon.exterior.coords)),
                entity_paths=[s['entity_path'] for s in selected], room_identity='unconfirmed')]
        report['validation_status'] = 'passed'
        report['warnings'] = ['SEMANTIC_COMPLETENESS_REQUIRES_HUMAN_REVIEW']
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        # Do not reflect arbitrary provider content or validation dumps.
        code = str(exc)
        report['errors'] = [code if code.isupper() and len(code) < 100 else 'INVALID_OUTPUT_SCHEMA']
        report['items'] = []; report['cells'] = []
    return report
