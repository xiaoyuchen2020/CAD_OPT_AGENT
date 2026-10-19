"""Host-owned experimental validator. Never imports or executes generated Python."""
import math
from shapely import set_precision
from shapely.geometry import LineString,Polygon,Point,box
from shapely.ops import unary_union,polygonize
from cad_agent.extensions.table_recovery.bundle import digest

VERSION='room-boundary-pilot-validator@2'
CONTRACT={
 'version':'room-boundary-pilot@1',
 'output':{'input_id':'unchanged','status':'candidate|unresolved','reason':'required for unresolved',
   'vertices':'ordered unclosed WCS ring, 3..512 points',
   'edge_segment_ids':'one nonempty native segment ID list per edge; clips at intersections allowed; union must cover entire edge',
   'excluded_crossing_ids':'all visible non-structural native segments intersecting polygon interior by > tolerance',
   'area':'local-origin area in native square units'},
 'rules':['Use only visible, visibility-supported WALL/COLU/WINDOW layer segments as boundary support. WINDOW is an observed opening closure, not proof of a room net measurement.',
          'Build a planar straight-edge graph, split crossings and overlapping endpoints; merge sub-tolerance numerical noise only. Select unique bounded minimal face strictly containing target seed.',
          'No inferred gap bridges, invented bounding box, fixed coordinates/IDs/area, model-selected units, or exclusions of structural partition walls.',
          'Axis/dimension/other non-structural segments may cross the interior, but must be declared as exclusions, never boundaries.',
          'If input units or visibility unsupported, target ambiguous, open boundary, or non-axis-aligned geometry unsupported by your algorithm, return unresolved.',
          'This is a source-bound observed room-envelope candidate, not automatic net-area acceptance.']}

def validate(b,o):
    checks={};errors=[]
    def require(ok,code):
        if not ok:raise ValueError(code)
    try:
        body={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']}
        require(digest(body)==b['bundle_sha256'] and b['input_id']=='room_'+b['bundle_sha256'],'INPUT_INTEGRITY_FAILED')
        require(o.get('input_id')==b['input_id'],'WRONG_INPUT_ID')
        u=b['unit_evidence'];require(u['insunits']==4 and u['effective_unit']=='mm' and u['metres_per_unit']==.001 and not u['override_used'] and u['source_sha256']==b['source_sha256'],'UNIT_EVIDENCE_INVALID')
        require(not b['coverage']['blocking_diagnostics'],'INCOMPLETE_SOURCE')
        require(all(s['visibility']['supported'] for s in b['segments']),'UNSUPPORTED_VISIBILITY')
        if o.get('status')=='unresolved':return dict(validation_status='unresolved',errors=[o.get('reason','ABSTAIN')],validator=VERSION)
        require(set(o)=={'input_id','status','vertices','edge_segment_ids','excluded_crossing_ids','area'} and o['status']=='candidate','OUTPUT_SCHEMA_INVALID')
        tol=b['policy']['tolerance_units'];allowed=set(b['policy']['wall_layers']+b['policy']['opening_layers']);segs={s['segment_id']:s for s in b['segments']}
        visible={i:s for i,s in segs.items() if s['visibility']['visible']}
        structural={i:s for i,s in visible.items() if s['visibility']['effective_layer'] in allowed}
        require(3<=len(o['vertices'])<=512 and len(o['vertices'])==len(o['edge_segment_ids']),'RING_SIZE_INVALID')
        require(all(len(p)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in p) for p in o['vertices']),'NONFINITE_VERTEX')
        p=Polygon(o['vertices']);require(p.is_valid and p.area>tol*tol and not p.interiors,'NOT_SIMPLE_RING')
        require(box(*b['bbox']).buffer(tol).covers(p),'OUTSIDE_SCOPE')
        target=next(t for t in b['texts'] if t['entity_id']==b['target']['label_entity_id'])
        require(target['text']==b['target']['label'] and target['origin']==b['target']['seed'] and target['visibility']['visible'] and target['visibility']['supported'],'TARGET_EVIDENCE_INVALID')
        require(p.contains(Point(b['target']['seed'])),'TARGET_OUTSIDE')
        edges=list(zip(o['vertices'],o['vertices'][1:]+o['vertices'][:1]));support=[]
        for (a,c),ids in zip(edges,o['edge_segment_ids']):
            require(isinstance(ids,list) and ids and len(ids)==len(set(ids)) and set(ids)<=segs.keys(),'FOREIGN_OR_DUPLICATE_SEGMENT')
            require(all(i in visible for i in ids),'HIDDEN_BOUNDARY_SOURCE')
            require(all(i in structural for i in ids),'NONSTRUCTURAL_BOUNDARY')
            line=LineString([a,c]);require(line.length>1e-7,'ZERO_EDGE')
            sources=[LineString([segs[i]['a'],segs[i]['b']]) for i in ids]
            require(line.difference(unary_union(sources).buffer(tol,cap_style=2)).length<=tol,'UNSUPPORTED_EDGE_OR_BRIDGE')
            require(all(s.intersection(line.buffer(tol,cap_style=2)).length>tol/10 for s in sources),'IRRELEVANT_EDGE_SOURCE')
            support.append(dict(a=a,b=c,segment_ids=ids,entity_paths=[segs[i]['entity_path'] for i in ids]))
        # Independent host graph construction from all eligible source, not candidate-selected edges.
        # Snap at 1e-5 of declared tolerance: native binary roundoff must not create
        # 60 mm zero-width slits. Reject a candidate whose raw boundary hides such a slit.
        precision=tol*1e-5
        network=unary_union([set_precision(LineString([s['a'],s['b']]),precision) for s in structural.values()])
        faces=[f for f in polygonize(network) if f.contains(Point(b['target']['seed']))]
        require(len(faces)==1,'TARGET_FACE_NOT_UNIQUE_OR_OPEN')
        face=faces[0];require(not face.interiors,'TARGET_FACE_HAS_HOLES')
        canonical=set_precision(p,precision)
        require(p.boundary.hausdorff_distance(canonical.boundary)<=tol*2,'BOUNDARY_NUMERICAL_BACKTRACK')
        require(p.symmetric_difference(face).area<=tol*p.length and p.boundary.hausdorff_distance(face.boundary)<=tol*2,'NOT_COMPLETE_MINIMAL_STRUCTURAL_FACE')
        internal=p.buffer(-tol)
        crossings=sorted(i for i,s in visible.items() if i not in structural and LineString([s['a'],s['b']]).intersection(internal).length>tol)
        require(isinstance(o['excluded_crossing_ids'],list) and sorted(o['excluded_crossing_ids'])==crossings,'CROSSING_EXCLUSIONS_INCOMPLETE_OR_FALSE')
        ox,oy=o['vertices'][0];local=[(x-ox,y-oy) for x,y in o['vertices']]
        area=abs(sum(a[0]*c[1]-c[0]*a[1] for a,c in zip(local,local[1:]+local[:1])))/2
        require(type(o['area']) in (int,float) and math.isfinite(o['area']) and abs(area-o['area'])<=max(1e-5,area*1e-9),'AREA_MISMATCH')
        # Report outside parallel evidence; thresholds are diagnostic, not a net-area certificate.
        supported=0.;pair_details=[]
        for a,c in edges:
            l=LineString([a,c]);dx=c[0]-a[0];dy=c[1]-a[1];n=math.hypot(dx,dy);intervals=[]
            for s in structural.values():
                q=LineString([s['a'],s['b']]);ex=s['b'][0]-s['a'][0];ey=s['b'][1]-s['a'][1]
                if abs(dx*ey-dy*ex)>1e-5*n*max(q.length,1):continue
                distance=l.distance(q)
                if not 60-tol<=distance<=500+tol or internal.intersection(q).length>tol:continue
                start,end=sorted(((v[0]-a[0])*dx/n+(v[1]-a[1])*dy/n for v in [s['a'],s['b']]))
                start=max(0,start);end=min(n,end)
                if end>start:intervals.append(LineString([(start,0),(end,0)]))
            length=unary_union(intervals).length if intervals else 0;supported+=length;pair_details.append(length/n)
        fraction=supported/p.length
        require(fraction>=b['policy']['minimum_parallel_support_fraction'],'INSUFFICIENT_PARALLEL_FACE_SUPPORT')
        checks=dict(area_native=area,observed_envelope_area_m2=area*u['metres_per_unit']**2,edges=len(edges),edge_support=support,
            excluded_crossing_ids=crossings,excluded_crossing_layers=sorted({segs[i]['visibility']['effective_layer'] for i in crossings}),
            parallel_support_fraction=fraction,per_edge_parallel_support=pair_details,
            target_contained=True,complete_structural_face=True,semantic_acceptance=False,net_area_accepted=False)
    except (ValueError,KeyError,TypeError,StopIteration,IndexError) as exc:errors=[str(exc) or type(exc).__name__]
    return dict(validation_status='rejected' if errors else 'passed',errors=errors,validator=VERSION,checks=checks)
