"""Trusted, bounded input builder; source geometry is never preview tessellation."""
import copy
import hashlib
import json
import math
from statistics import median

from shapely.geometry import box

from cad_agent.analysis.general import INDEX_VERSION
from cad_agent.contracts import stable_id
from cad_agent.geometry.source import native_source, GEOMETRY_VERSION
from cad_agent.semantics.tables import grid, TABLE_VERSION
from .contracts import VERSION, MAX_INPUT


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def verify_bundle(bundle):
    body = {k: v for k, v in bundle.items() if k not in ('input_id', 'bundle_sha256')}
    sha = digest(body)
    if bundle['bundle_sha256'] != sha or bundle['input_id'] != 'tri_' + sha:
        raise ValueError('INPUT_INTEGRITY_FAILED')


def prepare(index, table_id):
    table = next((t for t in index.tables if t['table_id'] == table_id), None)
    if table is None:
        raise ValueError('表格不属于当前快照。')
    return _prepare(index, table_id, table)


def prepare_region(index, layout_id, bounds):
    if layout_id not in {v['layout_id'] for v in index.drawing['layouts']}:
        raise ValueError('LAYOUT_NOT_IN_SNAPSHOT')
    if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
        or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise ValueError('INVALID_SCOPE_BOUNDS')
    scope_id = stable_id('adscope', index.snapshot, layout_id, digest(bounds))
    envelope = dict(table_id=scope_id, layout_id=layout_id, bbox=bounds,
                    rows=[dict(bbox=bounds, cells=[dict(bbox=bounds)])])
    return _prepare(index, scope_id, envelope, region=True)


def _prepare(index, table_id, table, region=False):
    source = index.w.store.artifact(index.snapshot, 'source.dxf')
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_sha != index.drawing['sha256']:
        raise ValueError('SOURCE_INTEGRITY_FAILED')
    try:
        if region:
            baseline = dict(status='not_applicable', reason='explicit_region_scope_not_table')
        else:
            cells, n, m, notes = grid(index, copy.deepcopy(table))
            baseline = dict(status='candidate', row_count=n, column_count=m,
                            output_sha256=digest([cells, n, m, notes]), diagnostics=notes)
    except ValueError as exc:
        message = str(exc)
        code = next((c for c in ('TABLE_GRAPHICS_DOMINATE', 'NON_RECTANGULAR_CELL_COMPONENT') if c in message), None)
        code = code or ('ANALYSIS_LIMIT_EXCEEDED' if '范围' in message else 'COLUMN_BOUNDARIES_INSUFFICIENT')
        baseline = dict(status='unresolved', code=code, message=message)
    a, b, c, d = table['bbox']
    lid = table['layout_id']
    texts = []
    for text in index.scene['texts']:
        if text['layout_id'] == lid and a <= text['origin'][0] < c and b <= text['origin'][1] < d:
            item = {k: copy.deepcopy(text[k]) for k in ('entity_id', 'entity_path', 'text', 'origin', 'bbox', 'height', 'rotation')}
            item['evidence'] = index.entity_evidence(text['entity_id'])
            texts.append(item)
    texts.sort(key=lambda t: t['entity_id'])
    if len(texts) > 5000:
        raise ValueError('INPUT_LIMIT_EXCEEDED: texts')
    xs = sorted({v for row in table['rows'] for cell in row['cells'] for v in (cell['bbox'][0], cell['bbox'][2])} | {a, c})
    ys = sorted({v for row in table['rows'] for v in (row['bbox'][1], row['bbox'][3])} | {b, d}, reverse=True)
    steps = [abs(v-u) for seq in (xs, ys) for u, v in zip(seq, seq[1:]) if v != u]
    tol = max(1e-7, min(median([t['height'] for t in texts]) * .02 if texts else .01, min(steps) * .01))
    native = native_source(index.w, index.snapshot)
    ids = sorted(native['ids'][int(i)] for i in native['tree'].query(box(a-tol,b-tol,c+tol,d+tol), predicate='intersects'))
    segments, excluded = [], []
    for eid in ids:
        entity, path, layout, bounds = native['entities'][eid]
        if layout != lid:
            continue
        kind = entity.dxftype()
        points = None
        if kind == 'LINE':
            points = [entity.dxf.start, entity.dxf.end]
        elif kind == 'LWPOLYLINE' and not entity.has_arc:
            points = list(entity.vertices_in_wcs())
            if entity.closed and points:
                points.append(points[0])
        elif kind == 'POLYLINE' and entity.is_2d_polyline and not any(v.dxf.get('bulge', 0) for v in entity.vertices):
            points = list(entity.points_in_wcs())
            if entity.is_closed and points:
                points.append(points[0])
        if not points or any(abs(p.z) > tol or not all(math.isfinite(v) for v in p) for p in points):
            excluded.append(dict(entity_id=eid, entity_path=path, kind=kind, reason='NOT_PLANAR_STRAIGHT_LINEWORK'))
            continue
        for number, (p, q) in enumerate(zip(points, points[1:])):
            if max(p.x,q.x)<a-tol or min(p.x,q.x)>c+tol or max(p.y,q.y)<b-tol or min(p.y,q.y)>d+tol:
                continue
            segments.append(dict(segment_id=stable_id('trseg', index.snapshot, eid, str(number)),
                                 entity_id=eid, entity_path=path, kind=kind, segment_index=number,
                                 a=[p.x,p.y], b=[q.x,q.y], geometry_basis='native_wcs_straight_segment'))
            if len(segments)>20000:
                raise ValueError('INPUT_LIMIT_EXCEEDED: segments')
    diagnostics = copy.deepcopy(index.drawing['diagnostics']) + [v for v in native['diagnostics'] if v.get('layout_id') == lid]
    blocking = [v for v in diagnostics if v.get('code') in {
        'ENTITY_LIMIT', 'BLOCK_CYCLE', 'XREF_UNRESOLVED', 'BLOCK_ENTITY_SKIPPED', 'TEXT_READ_FAILED', 'UNLOCATED_GEOMETRY'}]
    if (len(xs)-1)*(len(ys)-1)>4000:
        blocking.append(dict(code='ANALYSIS_LIMIT_EXCEEDED'))
    if not region and any(abs(t['rotation']) > .1 for t in texts):
        blocking.append(dict(code='UNSUPPORTED_TABLE_ORIENTATION'))
    body = dict(schema_version=VERSION, snapshot_id=index.snapshot, source_sha256=source_sha,
                parsed_artifact_sha256={name:hashlib.sha256(index.w.store.artifact(index.snapshot,name).read_bytes()).hexdigest()
                                        for name in ('entities.json','regions.json','scene.json')},
                parser_version=index.drawing['parser_version'], index_version=INDEX_VERSION,
                table_version=TABLE_VERSION, geometry_version=GEOMETRY_VERSION,
                table_id=table_id, layout_id=lid, bbox=table['bbox'], coordinate_space='dxf_wcs',
                unit=index.drawing['unit'], xs=xs, ys=ys, tolerance=tol, max_gap_ratio=.02,
                texts=texts, segments=segments, excluded_geometry=excluded, baseline=baseline,
                coverage=dict(scope='explicit_native_region' if region else 'existing_table_envelope', truncated=False,
                              blocking_diagnostics=blocking, source_diagnostics=diagnostics,
                              note='Only native planar straight linework; other geometry retained as exclusions.'))
    sha = digest(body)
    bundle = body | dict(input_id='tri_'+sha, bundle_sha256=sha)
    if len(encoded(bundle)) > MAX_INPUT:
        raise ValueError('INPUT_LIMIT_EXCEEDED: bytes')
    return bundle
