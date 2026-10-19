"""Bounded, overlapping subdivision after a geometric complexity limit.

Never relies on room names. Layer hypotheses require visual screening.
Keeps unsuccessful and unvisited scope explicit.
The original full-scope result remains immutable and linked for audit.
"""
from copy import deepcopy
import json
from shapely.geometry import Polygon, LineString, box
from cad_agent.contracts import stable_id
from cad_agent.geometry.discovery import discover_enclosures
from cad_agent.geometry.source import scoped_geometry_index, UNIT_TO_M

LIMITS={'segment_limit_exceeded','face_limit_exceeded','intersection_limit_exceeded'}
VERSION='bounded-discovery-tiles-v5-structural-priority'


def discover_with_subdivision(service,args):
    original=service.discover(args)
    limited_scope=bool(LIMITS.intersection(original['diagnostics']))
    if {'unknown_units','non_plan_view','discovery_scope_too_large'}&set(original['diagnostics']):return original
    key=stable_id('measure',original['measurement_id'],VERSION)
    try:return service.get(key)
    except KeyError:pass
    factor=UNIT_TO_M.get(original['applied_unit'])
    if not factor:return original
    bounds=original['scope_bbox'];a,b,c,d=bounds
    index=scoped_geometry_index(service.w,service.sid,args.layout_id,bounds,original['applied_unit'])
    from cad_agent.geometry.source import source_descriptors
    from cad_agent.geometry.compute import compute
    records=index.query(args.layout_id,bounds)
    descriptors=source_descriptors(service.w,service.sid,records)
    records=[dict(r,effective_layer=descriptors['effective_layers'][r['entity_id']]) for r in records]
    payload=dict(original=original,key=key,snapshot_id=service.sid,records=records,
                 arc_sources=descriptors['arc_sources'],args=args.model_dump())
    out=compute(service.w,'tiled_discovery',payload)
    if out['measurement_id'] != original['measurement_id']:
        service.w.store.put('measurement',key,out)
    return out


def compute_subdivision(payload):
    """Pure tile/structural topology calculation; no nested worker or Store."""
    from cad_agent.geometry.contracts import RoomDiscover
    from cad_agent.geometry.source import GeometryIndex
    from cad_agent.geometry.room_boundary import WALL, FURNITURE
    from cad_agent.components.openings import stepped_jamb_portals, supported_symbol_portals, swing_symbols_from_sources
    args=RoomDiscover.model_validate(payload['args'])
    original=payload['original'];key=payload['key'];sid=payload['snapshot_id']
    factor=UNIT_TO_M[original['applied_unit']]
    bounds=original['scope_bbox'];a,b,c,d=bounds
    limited_scope=bool(LIMITS.intersection(original['diagnostics']))
    index=GeometryIndex({'entities':payload['records']})
    candidates={};contours={};attempts=[];covered=[];occupied=0
    def structural_pass(records,tile):
        wall=[r for r in records if WALL.search(r['effective_layer']) and not FURNITURE.search(r['effective_layer'])]
        if not wall:return None
        # Alternative source selection is explicit and never a room identity
        # certificate. Keep all-linework hypotheses alongside it for review.
        portals,issues=stepped_jamb_portals(wall,factor,args) if args.close_openings else ([],[])
        if args.close_openings:portals+=supported_symbol_portals(wall,swing_symbols_from_sources(records,payload['arc_sources'],factor),factor,args)
        bridges=[dict(points=f,entity_ids=p['entity_ids'],method=p['method']) for p in portals for f in p['faces']]
        r=discover_enclosures(wall,tile,factor,args,extra_bridges=bridges)
        r['diagnostics']+=issues
        for c in r['candidates']:
            c['linework_basis']='structural_layer_hypothesis'
            c['diagnostics'].append('layer_hints_require_visual_review')
        return r
    if not limited_scope:
        structural=structural_pass(index.query(args.layout_id,bounds),bounds)
        if not structural or LIMITS.intersection(structural['diagnostics']):return original
        for candidate in original['candidates']+structural['candidates']:
            identity=Polygon(candidate['boundary']).normalize().wkb_hex
            if identity not in candidates:candidates[identity]=candidate
        for r in (original,structural):contours.update({v['contour_id']:v for v in r.get('contours',[])})
        out=deepcopy(original)
        out.update(measurement_id=key,algorithm_version=VERSION,baseline_discovery_id=original['measurement_id'],
            candidates=sorted(candidates.values(),key=lambda c:(-c['candidate_shell_area_m2'],c['candidate_id'])),
            contours=list(contours.values()),status='needs_review' if candidates else 'unresolved',
            diagnostics=list(dict.fromkeys(original['diagnostics']+['structural_layer_hypothesis_requires_review'])),
            evidence_url=f'/api/drawings/{sid}/measurements/{key}',
            preview_url=f'/api/drawings/{sid}/measurements/{key}/preview.svg')
        return out
    # Prioritize occupied structural linework, not scan order (which favored
    # title blocks below plans). Layers are a traversal hint, not room identity.
    tiles=[]
    for row in range(4):
        for col in range(4):
            dx=(c-a)/4;dy=(d-b)/4
            tile=[max(a,a+(col-.15)*dx),max(b,b+(row-.15)*dy),min(c,a+(col+1.15)*dx),min(d,b+(row+1.15)*dy)]
            records=index.query(args.layout_id,tile);window=box(*tile)
            weight=sum(LineString(path).intersection(window).length*factor for r in records
                       if WALL.search(r['effective_layer']) and not FURNITURE.search(r['effective_layer'])
                       for path in r['paths'] if len(path)>1)
            tiles.append((weight,row,col,tile,records))
    for weight,row,col,tile,records in sorted(tiles,key=lambda t:(-t[0],t[1],t[2])):
        result=discover_enclosures(records,tile,factor,args)
        limited=bool(LIMITS.intersection(result['diagnostics']))
        attempts.append(dict(bbox=tile,structural_line_length_m=round(weight,3),diagnostics=result['diagnostics'],candidate_count=len(result['candidates']),completed=not limited))
        if limited:continue
        covered.append(tile)
        structural=structural_pass(records,tile)
        alternatives=[result]+([structural] if structural and not LIMITS.intersection(structural['diagnostics']) else [])
        merged_candidates=[c for r in alternatives for c in r['candidates']]
        occupied+=bool(merged_candidates)
        for candidate in merged_candidates:
            if 'boundary_reaches_search_or_frame_limit' in candidate['diagnostics']:continue
            poly=Polygon(candidate['boundary']);identity=poly.normalize().wkb_hex
            if identity not in candidates:candidates[identity]=candidate
        for r in alternatives:contours.update({v['contour_id']:v for v in r.get('contours',[])})
        if occupied>=4:break
    out=deepcopy(original)
    out.update(measurement_id=key,algorithm_version=VERSION,baseline_discovery_id=original['measurement_id'],
        candidates=sorted(candidates.values(),key=lambda c:(-c['candidate_shell_area_m2'],c['candidate_id'])),
        contours=list(contours.values()),complete=False,status='needs_review' if candidates else 'unresolved',
        diagnostics=list(dict.fromkeys(original['diagnostics']+['subdivided_partial_coverage','room_identity_unconfirmed'])),
        subdivision=dict(attempted=attempts,checked_bboxes=covered,requested_bbox=bounds,
                         full_scope_checked=False,max_tiles=16,priority_basis="structural_source_line_length",note='只对成功子范围内不触边候选排序；其余区域、跨子区大空间和曲线均不保证覆盖。'),
        evidence_url=f'/api/drawings/{sid}/measurements/{key}',
        preview_url=f'/api/drawings/{sid}/measurements/{key}/preview.svg')
    return out
