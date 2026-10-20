"""Exact selected-source line topology, with independently unconfirmed semantics.

No gap closing, wall-layer heuristic, bbox enclosure, unit guess, or largest-face
promotion is performed. All derived vertices are native vertices/intersections.
"""
from copy import deepcopy
import math
from typing import Literal

from pydantic import Field
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize_full, unary_union
from shapely.strtree import STRtree

from cad_agent.contracts import Record, stable_id
from cad_agent.analysis.unit_policy import geometry_unit_resolution
from cad_agent.geometry.source import UNIT_TO_M

VERSION = 'selected-source-boundaries-v1'
MAX_SEGMENTS = 10000
MAX_CONTOURS = 256


class InspectSpatialBoundary(Record):
    object_id: str = Field(min_length=1)


class MeasureSpatialArea(Record):
    object_id: str = Field(min_length=1)
    contour_id: str = Field(min_length=1)
    basis: Literal['topological_face', 'outer_shell', 'selected_exclusions'] = 'topological_face'


def _parts(g):
    return list(g.geoms) if hasattr(g, 'geoms') else ([] if g.is_empty else [g])


def _extract(records, layout_id, origin):
    segments, closed, diagnostics = [], [], []
    for rec in records:
        eid = rec['entity_id']
        if rec.get('layout_id') != layout_id:
            raise ValueError('SPATIAL_LAYOUT: selected paths cross layouts.')
        if rec.get('status') != 'ready' or not rec.get('linear') or not rec.get('paths') or rec.get('kind') in ('DIMENSION', 'HATCH'):
            diagnostics.append(dict(code='unsupported_native_geometry', entity_id=eid))
            continue
        for pi, path in enumerate(rec.get('paths', [])):
            if len(path) < 2:
                diagnostics.append(dict(code='degenerate_path', entity_id=eid, path_index=pi))
                continue
            if any(len(p) < 2 or not all(math.isfinite(v) for v in p[:2]) for p in path):
                raise ValueError('SPATIAL_COORDINATES: non-finite source coordinate.')
            pts = [(p[0]-origin[0], p[1]-origin[1]) for p in path]
            if pts[0] == pts[-1]:
                poly = Polygon(pts)
                if not poly.is_valid or poly.area <= 0:
                    diagnostics.append(dict(code='invalid_closed_path', entity_id=eid, path_index=pi))
                    continue
                closed.append(poly)
            for si, (a, b) in enumerate(zip(pts, pts[1:])):
                if a != b:
                    segments.append(dict(line=LineString([a,b]), entity_id=eid,
                        entity_path=rec.get('entity_path', []), path_index=pi, segment_index=si, native_path_closed=pts[0]==pts[-1]))
                if len(segments) > MAX_SEGMENTS:
                    raise ValueError('SPATIAL_BUDGET: select fewer source paths; no partial polygonization performed.')
    return segments, closed, diagnostics


def _topology(segments):
    if not segments:
        return [], [], [], []
    graph = unary_union([s['line'] for s in segments])
    faces, cuts, dangles, invalid = polygonize_full(graph)
    return _parts(faces), _parts(cuts), _parts(dangles), _parts(invalid)


def _world(points, origin):
    return [[float(p[0]+origin[0]), float(p[1]+origin[1])] for p in points]


def _polygon_json(poly, origin):
    return dict(boundary=_world(poly.exterior.coords, origin),
        holes=[_world(r.coords, origin) for r in poly.interiors])


def _edge_support(ring, segments, tree, origin, epsilon):
    edges = []
    for a,b in zip(ring.coords, list(ring.coords)[1:]):
        line = LineString([a,b]); length = line.length
        ux,uy = (b[0]-a[0])/length,(b[1]-a[1])/length
        spans, sources = [], []
        for j in tree.query(line.buffer(epsilon, cap_style=2)):
            source = segments[int(j)]; c,d = source['line'].coords
            offsets = [abs((p[0]-a[0])*uy-(p[1]-a[1])*ux) for p in (c,d)]
            if max(offsets) > epsilon:
                continue
            lo,hi = sorted((p[0]-a[0])*ux+(p[1]-a[1])*uy for p in (c,d))
            lo,hi = max(0.,lo),min(length,hi)
            if hi > lo:
                spans.append((lo,hi)); sources.append({k:v for k,v in source.items() if k!='line'})
        merged=[]
        for lo,hi in sorted(spans):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1],hi)
            else:
                merged.append([lo,hi])
        observed = sum(hi-lo for lo,hi in merged)
        gaps=[]; cursor=0.
        for lo,hi in merged:
            if lo-cursor > epsilon:
                gaps.append([cursor,lo])
            cursor=max(cursor,hi)
        if length-cursor > epsilon:
            gaps.append([cursor,length])
        edges.append(dict(points=_world([a,b],origin), length_drawing_units=length,
            observed_fraction=min(1.,observed/length), source_segments=sources,
            source_entity_ids=sorted({s['entity_id'] for s in sources}),
            unobserved_intervals_drawing_units=gaps, inferred=False))
    return edges


def _numeric(area, perimeter, unit):
    result = dict(area_drawing_units_squared=float(area), perimeter_drawing_units=float(perimeter),
        area_m2=None, perimeter_m=None, conditional=unit['unit_status']=='assumption')
    factor=UNIT_TO_M.get(unit['unit'])
    if factor is not None:
        result.update(area_m2=float(area*factor*factor), perimeter_m=float(perimeter*factor))
    return result


def _context(obj):
    return dict(kind='spatial_method', version=VERSION, object_id=obj['object_id'],
        **{k:obj[k] for k in ('source_sha256','snapshot_id','layout_id','frame_id','scope_id')},
        spatial_fact_scope=dict(basis='exact_selected_source_geometry', identity_confirmed=False, role_status=obj['role']['status'], engineering_confirmed=False),
        role=deepcopy(obj['role']), engineering_confirmed=False,
        coverage=dict(selected_entities_complete=obj['coverage'].get('selected_entities_complete',False),
            object_extent='unconfirmed', main_outline='unconfirmed', net_boundary='unconfirmed', census='not_evaluated'),
        measurement_basis='native_linear_paths_exact_noding_no_gap_closure',
        unit=geometry_unit_resolution(obj['unit']['value'],obj['unit'].get('policy')),
        measurements=[], diagnostics=[], operation_receipts=[], next_calls=[])


def boundary_topology(obj):
    """Pure object-level inspection; session entry points revalidate source binding."""
    result=_context(obj)
    if obj['role'].get('reference_only') and obj['role']['value'] != 'source_geometry':
        raise ValueError('SPATIAL_REFERENCE: reference-only geometry cannot support a placed room/main-boundary role.')
    records=obj['geometry']['records']
    origin=next((p[:2] for r in records for path in r.get('paths',[]) for p in path),[0,0])
    segments,closed,issues=_extract(records,obj['layout_id'],origin)
    result['diagnostics'].extend(issues)
    result['contours']=[]
    # Do not quietly omit an unparsed source component and claim a complete face.
    if issues or not segments:
        result.update(status='unsupported_source_geometry', source_geometry_complete=False)
        return result, {}
    faces,cuts,dangles,invalid=_topology(segments)
    if len(faces)+len(closed)>MAX_CONTOURS:
        raise ValueError('SPATIAL_BUDGET: too many contours; select a smaller source object.')
    extent=max(abs(v) for s in segments for p in s['line'].coords for v in p)
    epsilon=max(math.ulp(max(extent,1.))*64,extent*1e-12)
    tree=STRtree([s['line'] for s in segments])
    # Preserve original closed paths and polygonized concave faces. A native loop
    # crossing other loops remains an explicitly selectable source shell.
    candidates={}
    for poly,method in [(p,'polygonized_native_face') for p in faces]+[(p,'native_closed_path') for p in closed]:
        poly=poly.normalize()
        key=poly.wkb_hex
        if key not in candidates:
            candidates[key]=[poly,set()]
        candidates[key][1].add(method)
    models={}
    for key,(poly,methods) in sorted(candidates.items()):
        cid=stable_id('source_contour',VERSION,obj['object_id'],key)
        models[cid]=poly
        rings=[dict(kind='exterior',edge_evidence=_edge_support(poly.exterior,segments,tree,origin,epsilon))]
        rings.extend(dict(kind='interior',edge_evidence=_edge_support(r,segments,tree,origin,epsilon)) for r in poly.interiors)
        supported=all(e['source_entity_ids'] and not e['unobserved_intervals_drawing_units'] for r in rings for e in r['edge_evidence'])
        facts=_numeric(poly.area,poly.length,result['unit'])
        result['contours'].append(dict(contour_id=cid,methods=sorted(methods),**_polygon_json(poly,origin),
            boundary_support=rings, all_edges_source_supported=supported,
            geometric_role='selected_source_enclosure', semantic_role='unconfirmed',
            hole_semantics='geometric_topology_only',
            strictly_contained_label_region_ids=[l['region_id'] for l in obj.get('label',[]) if l.get('points') and all(poly.contains(Point(p[0]-origin[0],p[1]-origin[1])) for p in l['points'])],
            measurements=facts))
        if supported:
            result['measurements'].append(dict(contour_id=cid,basis='topological_face',**facts))
    for item in result['contours']:
        p=Polygon(models[item['contour_id']].exterior)
        item['contained_by_contour_ids']=[cid for cid,q in models.items() if cid!=item['contour_id'] and Polygon(q.exterior).contains_properly(p)]
        item['boundary_intersection_contour_ids']=[cid for cid,q in models.items() if cid!=item['contour_id'] and p.boundary.intersects(q.boundary)]
    for kind,parts in [('cut_edges',cuts),('dangling_edges',dangles),('invalid_rings',invalid)]:
        if parts:
            result['diagnostics'].append(dict(code=kind, count=len(parts),
                paths=[_world(p.coords,origin) for p in parts],
                interpretation='unclosed_or_unused_source_edges_not_automatically_door_openings'))
    endpoints={}
    for s in segments:
        for p in [tuple(s['line'].coords[0]),tuple(s['line'].coords[-1])]:
            endpoints.setdefault(p,set()).add(s['entity_id'])
    result['open_endpoints']=[dict(point=_world([p],origin)[0],source_entity_ids=sorted(ids))
        for p,ids in endpoints.items() if len(tree.query(Point(p), predicate='intersects'))==1]
    # Topological facts are complete for selected source paths, even if those
    # paths include unused branches. Whole-object or whole-floor closure is not.
    result.update(status='source_geometry_measured' if result['measurements'] else 'open_boundary',
        source_geometry_complete=True, selected_graph_fully_polygonized=not(cuts or dangles or invalid),
        origin=origin, numeric_support_epsilon_drawing_units=epsilon,
        automatic_closure_segments=[], main_outline_selection='not_inferred_from_area_or_bbox')
    result['operation_receipts']=[dict(operation='contours',fulfilled=bool(result['measurements']),basis='source_supported_selected_path_topology',scope='selected_source_geometry',engineering_confirmed=False)]
    result['next_calls']=[dict(tool='measure_spatial_area',arguments=dict(object_id=obj['object_id'],contour_id=c['contour_id'],basis='topological_face'),purpose='Measure this exact source-supported face; choose deliberately, no main-outline or net-area identity is inferred.') for c in result['contours'] if c['all_edges_source_supported']]
    return result,models


def spatial_area(obj, args):
    inspection,models=boundary_topology(obj)
    if args.contour_id not in models:
        raise ValueError('SPATIAL_CONTOUR: inspect this exact object first; contour is absent or stale.')
    selected=next(c for c in inspection['contours'] if c['contour_id']==args.contour_id)
    if not selected['all_edges_source_supported']:
        raise ValueError('SPATIAL_SUPPORT: contour has unsupported boundary intervals.')
    face=models[args.contour_id]; shell=Polygon(face.exterior)
    geometry=face if args.basis=='topological_face' else shell
    excluded=Polygon(); exclusions=obj['exclusions']['records']
    diagnostics=[]
    if args.basis=='selected_exclusions':
        if not exclusions:
            raise ValueError('SPATIAL_EXCLUSIONS: explicitly bind exclusion entities before choosing this basis.')
        segs,closed,issues=_extract(exclusions,obj['layout_id'],inspection['origin'])
        open_segs=[s for s in segs if not s['native_path_closed']]
        faces,cuts,dangles,invalid=_topology(open_segs)
        if any(p.boundary.intersection(q.boundary).length>0 for i,p in enumerate(faces) for q in faces[i+1:]):
            raise ValueError('SPATIAL_EXCLUSIONS: intersecting segmented exclusion faces are ambiguous; bind explicit closed paths.')
        if issues or cuts or dangles or invalid or not (faces or closed):
            raise ValueError('SPATIAL_EXCLUSIONS: selected exclusions must all form supported closed source regions.')
        excluded=unary_union(faces+closed)
        if not shell.contains_properly(excluded):
            raise ValueError('SPATIAL_EXCLUSIONS: exclusions must lie strictly inside the selected outer shell.')
        geometry=shell.difference(excluded)
    elif exclusions:
        diagnostics.append(dict(code='bound_exclusions_not_applied',applied_basis=args.basis))
    result=_context(obj)
    result.update(status='source_geometry_measured',contour_id=args.contour_id,
        measurement_basis=args.basis,diagnostics=diagnostics,
        measurements=dict(**_numeric(geometry.area,geometry.length,result['unit']),
            shell_area_drawing_units_squared=shell.area, excluded_union_area_drawing_units_squared=excluded.area),
        result_polygons=[_polygon_json(p,inspection['origin']) for p in _parts(geometry)],
        boundary_support=selected['boundary_support'],
        exclusion_entity_ids=obj['exclusions']['entity_ids'] if args.basis=='selected_exclusions' else [],
        ignored_topological_holes=args.basis!='topological_face' and bool(face.interiors),
        engineering_area_basis='unconfirmed', source_geometry_complete=True,
        operation_receipts=[dict(operation=op,fulfilled=True,basis=args.basis,scope='selected_source_geometry',engineering_confirmed=False,unit_status=result['unit']['unit_status']) for op in ('area','perimeter')])
    if args.basis=='selected_exclusions':
        tree=STRtree([s['line'] for s in segs])
        result['exclusion_boundary_support']=[dict(kind=kind, edge_evidence=_edge_support(ring,segs,tree,inspection['origin'],inspection['numeric_support_epsilon_drawing_units'])) for p in _parts(excluded) for kind,ring in [('exterior',p.exterior)]+[('interior',r) for r in p.interiors]]
    return result


def inspect_spatial_boundary(session,args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement
    obj=resolve_object_input(session,args.object_id)
    result,_=boundary_topology(obj)
    record_measurement(session,args.object_id,result)
    return result


def measure_spatial_area(session,args):
    from cad_agent.analysis.spatial_evidence import resolve_object_input, record_measurement
    obj=resolve_object_input(session,args.object_id)
    result=spatial_area(obj,args)
    record_measurement(session,args.object_id,result)
    return result
