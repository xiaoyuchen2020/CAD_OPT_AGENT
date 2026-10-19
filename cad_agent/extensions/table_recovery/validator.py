"""Independent host validator: does not import or call the recovery method."""
from collections import Counter

from pydantic import ValidationError
from shapely.geometry import LineString, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id
from .bundle import verify_bundle, digest
from .contracts import Candidate, VALIDATOR
from .native_rails import rails


def validate(bundle, raw):
    verify_bundle(bundle)
    errors=[];warnings=[]
    try:
        candidate_sha=digest(raw)
    except (ValueError,TypeError):
        return dict(validator_version=VALIDATOR,input_id=bundle['input_id'],candidate_sha256=None,
                    validation_status='rejected',review_status='pending',state='candidate',
                    errors=['INVALID_OUTPUT_SCHEMA'],warnings=[],cells=[],semantic_acceptance=False)
    def finish(cells=None, status=None):
        return dict(validator_version=VALIDATOR,input_id=bundle['input_id'],
                    candidate_sha256=candidate_sha,validation_status=status or ('rejected' if errors else 'passed'),
                    review_status='pending',state='candidate',errors=sorted(set(errors)),
                    warnings=sorted(set(warnings)),cells=cells or [],
                    semantic_acceptance=False)
    try:
        result=Candidate.model_validate(raw,strict=True)
    except ValidationError:
        errors.append('INVALID_OUTPUT_SCHEMA');return finish()
    if result.input_id != bundle['input_id']:
        errors.append('INPUT_ID_MISMATCH');return finish()
    if result.status=='unresolved':
        if result.cells or result.xs or result.ys:
            errors.append('UNRESOLVED_WITH_GRID');return finish()
        warnings.append(result.reason or 'METHOD_UNRESOLVED');return finish(status='needs_review')
    if bundle['coverage']['blocking_diagnostics']:
        errors.append('INPUT_INCOMPLETE_OR_UNSUPPORTED')
    if bundle['baseline'].get('code')=='TABLE_GRAPHICS_DOMINATE':
        errors.append('TABLE_IDENTITY_NOT_ESTABLISHED')
    xs,ys=result.xs,result.ys
    if xs!=bundle['xs'] or ys!=bundle['ys']:
        errors.append('GRID_ANCHOR_MISMATCH');return finish()
    n,m=len(ys)-1,len(xs)-1
    if n<1 or m<1 or n*m>4000 or any(v<=u for u,v in zip(xs,xs[1:])) or any(v>=u for u,v in zip(ys,ys[1:])):
        errors.append('INVALID_GRID_AXES');return finish()
    segments={s['segment_id']:s for s in bundle['segments']}
    segment_keys=list(segments)
    tree=STRtree([LineString([s['a'],s['b']]) for s in segments.values()])
    text_by_id={t['entity_id']:t for t in bundle['texts']}
    tol=bundle['tolerance'];slots={};shapes=[];bindings=[];assigned=[]
    # Shapely union/difference supplies an independent edge-coverage calculation.
    def edge_check(p,q,ids):
        target=LineString([p,q]);horizontal=p[1]==q[1];axis=0 if horizontal else 1
        lo,hi=sorted((p[axis],q[axis]));other=1-axis
        parts=[]
        for sid in ids:
            s=segments.get(sid)
            if s is None:
                errors.append('UNKNOWN_SEGMENT');continue
            a,b=s['a'],s['b']
            if max(abs(a[other]-p[other]),abs(b[other]-p[other]))>tol:
                errors.append('EDGE_SOURCE_OFF_LINE');continue
            left,right=max(lo,min(a[axis],b[axis])),min(hi,max(a[axis],b[axis]))
            if right<=left:
                errors.append('EDGE_SOURCE_OUTSIDE_EDGE');continue
            parts.append(LineString([(left,p[other]),(right,p[other])]) if horizontal else LineString([(p[other],left),(p[other],right)]))
        covered=unary_union(parts) if parts else LineString()
        missing=target.difference(covered)
        allowance=max(tol,target.length*bundle['max_gap_ratio'])
        return missing.length<=allowance+1e-8
    def source_ids(p,q):
        axis=0 if p[1]==q[1] else 1;other=1-axis;lo,hi=sorted((p[axis],q[axis]))
        nearby=[segment_keys[int(i)] for i in tree.query(LineString([p,q]).buffer(tol))]
        return [sid for sid in nearby for s in [segments[sid]]
                if max(abs(s['a'][other]-p[other]),abs(s['b'][other]-p[other]))<=tol
                and min(hi,max(s['a'][axis],s['b'][axis]))>max(lo,min(s['a'][axis],s['b'][axis]))]
    for number,cell in enumerate(result.cells):
        r,c=cell.row_index,cell.column_index;rr,cc=r+cell.row_span,c+cell.column_span
        if rr>n or cc>m:
            errors.append('CELL_SPAN_OUT_OF_RANGE');continue
        bounds=[xs[c],ys[rr],xs[cc],ys[r]];a,b,x,y=bounds
        polygon=box(*bounds);shapes.append(polygon)
        for row in range(r,rr):
            for col in range(c,cc):
                if (row,col) in slots:errors.append('OVERLAPPING_CELLS')
                slots[row,col]=number
        for (p,q),ids in zip((([a,b],[x,b]),([x,b],[x,y]),([x,y],[a,y]),([a,y],[a,b])),cell.edge_segment_ids):
            if not edge_check(p,q,ids):errors.append('UNSUPPORTED_CELL_EDGE')
        expected={eid for eid,t in text_by_id.items() if a<=t['origin'][0]<x and b<=t['origin'][1]<y}
        if set(cell.text_entity_ids)!=expected:errors.append('TEXT_ASSIGNMENT_MISMATCH')
        assigned.extend(cell.text_entity_ids)
        if any(eid not in text_by_id for eid in cell.text_entity_ids):
            errors.append('UNKNOWN_TEXT_ENTITY');continue
        chosen=sorted((text_by_id[eid] for eid in cell.text_entity_ids),key=lambda t:(-t['origin'][1],t['origin'][0],t['entity_id']))
        for t in chosen:
            if not polygon.buffer(tol).covers(box(*t['bbox'])):
                warnings.append('ESTIMATED_TEXT_BOX_CROSSES_CELL')
            if not t['evidence']:errors.append('TEXT_SOURCE_EVIDENCE_MISSING')
        bindings.append(dict(cell.model_dump(),cell_id=stable_id('trcell',bundle['input_id'],str(number),candidate_sha),
                             bbox=bounds,text='\n'.join(t['text'] for t in chosen),
                             evidence=[e for t in chosen for e in t['evidence']],state='derived'))
    if Counter(assigned)!=Counter(text_by_id.keys()):errors.append('TEXT_LOSS_OR_DUPLICATION')
    if len(slots)!=n*m:errors.append('GRID_SLOT_COVERAGE_INCOMPLETE')
    if not shapes or abs(unary_union(shapes).area-box(*bundle['bbox']).area)>max(tol*tol,1e-8):
        errors.append('GRID_AREA_COVERAGE_INCOMPLETE')
    # A full native separator must never disappear inside a merged cell.
    for r in range(n):
        for c in range(m):
            for neighbor,p,q in (
                ((r,c+1),[xs[c+1],ys[r+1]],[xs[c+1],ys[r]]),
                ((r+1,c),[xs[c],ys[r+1]],[xs[c+1],ys[r+1]])):
                if (r,c) in slots and neighbor in slots and slots[r,c]==slots[neighbor]:
                    ids=source_ids(p,q)
                    if edge_check(p,q,ids):errors.append('MERGE_ERASES_SOURCE_SEPARATOR')
    # Search every native coordinate, including anchors missing from the input.
    # Otherwise a geometrically valid 3x3 grid can erase a real 5x3 table.
    for cell in bindings:
        if rails(bundle, cell['bbox'], 0) or rails(bundle, cell['bbox'], 1):
            errors.append('CELL_ERASES_NATIVE_SEPARATOR')
    if bundle['excluded_geometry']:warnings.append('EXCLUDED_GEOMETRY_REQUIRES_REVIEW')
    return finish([] if errors else bindings)
