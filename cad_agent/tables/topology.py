"""Local planar line topology. No table names, legacy seeds or business values."""
from collections import defaultdict
from math import atan2, cos, sin, pi, hypot
from statistics import median
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union, polygonize
from shapely.strtree import STRtree
from cad_agent.contracts import stable_id
from .contracts import MAX_CELLS, MAX_SEGMENTS, MAX_CANDIDATES
from . import VERSION


def box(points):
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def transform_point(p, origin, angle, inverse=False):
    c, s = cos(angle), sin(angle)
    if inverse:
        return [origin[0] + c*p[0]-s*p[1], origin[1] + s*p[0]+c*p[1]]
    x, y = p[0]-origin[0], p[1]-origin[1]
    return [c*x+s*y, -s*x+c*y]


def normalize(lines, texts):
    """Use repeated orthogonal directions; preserve distinct block instances."""
    directions = defaultdict(float)
    for line in lines:
        if line.get('kind') not in ('LINE', 'LWPOLYLINE', 'POLYLINE') or line.get('exact_linear') is False:
            continue
        for p, q in zip(line['points'], line['points'][1:]):
            length = hypot(q[0]-p[0], q[1]-p[1])
            angle = atan2(q[1]-p[1], q[0]-p[0]) % (pi/2)
            if angle > pi/2-.001: angle = 0.
            directions[round(angle, 4)] += length
    # Multiple orientations are independent proposals, not a rotated bbox guess.
    angles = sorted(directions, key=directions.get, reverse=True)[:4]
    for angle in angles:
        points = [p for line in lines for p in line['points']]
        if not points: continue
        origin = [min(p[0] for p in points), min(p[1] for p in points)]
        height = median(t.get('height', 1) for t in texts) if texts else 1.
        tolerance = max(height*.002, 1e-7)
        segments = []
        for line in lines:
            if line.get('kind') not in ('LINE', 'LWPOLYLINE', 'POLYLINE') or line.get('exact_linear') is False: continue
            for p, q in zip(line['points'], line['points'][1:]):
                p, q = [transform_point(v, origin, angle) for v in (p,q)]
                if hypot(q[0]-p[0], q[1]-p[1]) < height*.2: continue
                if min(abs(p[0]-q[0]), abs(p[1]-q[1])) > tolerance: continue
                segments.append(dict(points=[p,q], entity_id=line['entity_id']))
        if len(segments) >= 4:
            yield origin, angle, height, tolerance, segments


def _anchors(values, tolerance):
    """Cluster close coordinates without cumulative gap bridging."""
    groups=[]
    for value in sorted(values):
        if groups and value-groups[-1][0] <= tolerance: groups[-1].append(value)
        else: groups.append([value])
    return {v:sum(g)/len(g) for g in groups for v in g}


def recover(scene, snapshot_id, layout_id='', bbox_filter=None, check=lambda: None, signature=''):
    groups=defaultdict(lambda:dict(lines=[],texts=[]))
    for kind in ('lines','texts'):
        for item in scene[kind]:
            if layout_id and item['layout_id']!=layout_id: continue
            if bbox_filter:
                b=item['bbox'];a=bbox_filter
                if b[2]<a[0] or b[0]>a[2] or b[3]<a[1] or b[1]>a[3]:continue
            path=tuple(item.get('entity_path',[])[:-1])
            groups[item['layout_id'],path][kind].append(item)
    candidates=[]; diagnostics=[]
    for (lid,path),group in groups.items():
        check()
        if len(group['texts'])<3:continue
        for origin,angle,height,tol,source in normalize(group['lines'],group['texts']):
            if len(source)>MAX_SEGMENTS:
                diagnostics.append(dict(code='TABLE_SEGMENT_LIMIT',layout_id=lid,instance_path=list(path)));continue
            xa=_anchors([p[0] for s in source for p in s['points']],tol)
            ya=_anchors([p[1] for s in source for p in s['points']],tol)
            geometries=[]
            for s in source:
                s['snapped']=[[xa[p[0]],ya[p[1]]] for p in s['points']]
                geometries.append(LineString(s['snapped']))
            if not geometries:continue
            check()
            faces=[]
            for shape in polygonize(unary_union(geometries)):
                if shape.interiors:continue
                a,b,c,d=shape.bounds
                if min(c-a,d-b)<height*.25:continue
                if abs(shape.area-(c-a)*(d-b))>max(tol*shape.length,shape.area*1e-7):continue
                faces.append(shape)
                if len(faces)>MAX_CELLS:
                    diagnostics.append(dict(code='TABLE_CELL_LIMIT',layout_id=lid));break
            if not faces or len(faces)>MAX_CELLS:continue
            merged=unary_union(faces)
            envelopes=list(merged.geoms) if hasattr(merged,'geoms') else [merged]
            line_tree=STRtree(geometries)
            clutter_lines=[LineString([transform_point(v,origin,angle) for v in l['points']]) for l in group['lines'] if len(l['points'])>=2]
            clutter_tree=STRtree(clutter_lines)
            for envelope in envelopes:
                check()
                a,b,c,d=envelope.bounds
                if envelope.interiors or abs(envelope.area-(c-a)*(d-b))>tol*envelope.length:continue
                members=[f for f in faces if envelope.covers(f.representative_point())]
                if not 4<=len(members)<=MAX_CELLS:continue
                ts=[]
                for t in group['texts']:
                    p=transform_point(t['origin'],origin,angle)
                    if a-height*.15<=p[0]<=c+height*.15 and b-tol<=p[1]<=d+tol:
                        ts.append(t|{'local_origin':p})
                if len(ts)<4:continue
                # A repeated subdivision and text population are needed. Keep
                # sparse plans / symbols out; unknown cases remain candidates.
                populated=sum(any(f.buffer(tol).covers(Point(t['local_origin'])) for t in ts) for f in members)
                if populated<3 or populated/len(members)<.25:continue
                xs=sorted({v for f in members for v in (f.bounds[0],f.bounds[2])})
                ys=sorted({v for f in members for v in (f.bounds[1],f.bounds[3])},reverse=True)
                if len(xs)<3 or len(ys)<3:continue
                # Native diagonal/curved graphics in many cells indicate a plan.
                clutter=0
                for f in members:
                    count=0
                    for i in clutter_tree.query(f):
                        line=clutter_lines[i]
                        if f.contains(line) and line.length>height*2: count+=1
                    clutter+=count>=5
                if clutter>=3 and clutter>=len(members)*.2:continue
                boundary=[transform_point(p,origin,angle,True) for p in [(a,b),(c,b),(c,d),(a,d)]]
                table_id=stable_id('tablev2',VERSION,signature,snapshot_id,lid,str(path),str(round(angle,4)),str([round(v,5) for v in (a,b,c,d)]))
                cells=[]
                for f in sorted(members,key=lambda v:(-v.bounds[3],v.bounds[0])):
                    x,y,z,w=f.bounds;corners=[[x,y],[z,y],[z,w],[x,w]]
                    edges=[]
                    for p,q in zip(corners,corners[1:]+corners[:1]):
                        edge=LineString([p,q]);ids=[]
                        for i in line_tree.query(edge.buffer(tol)):
                            if edge.intersection(geometries[i].buffer(tol)).length>=min(edge.length,geometries[i].length)*.9:
                                ids.append(source[i]['entity_id'])
                        edges.append(sorted(set(ids)))
                    world=[transform_point(p,origin,angle,True) for p in corners]
                    cells.append(dict(cell_id=stable_id('tcell',table_id,str([round(v,5) for v in f.bounds])),
                        row=ys.index(w),column=xs.index(x),row_span=ys.index(y)-ys.index(w),
                        column_span=xs.index(z)-xs.index(x),local_bbox=[x,y,z,w],bbox=box(world),
                        boundary=world,edge_sources=edges))
                candidates.append(dict(table_id=table_id,layout_id=lid,instance_path=list(path),
                    bbox=box(boundary),boundary=boundary,local_bbox=[a,b,c,d],cells=cells,texts=ts,
                    transform=dict(origin=origin,angle=angle,tolerance=tol,height=height),source_kind='ruled',
                    source_segments=[s for s in source if envelope.intersects(LineString(s['snapped']))]))
                if len(candidates)>=MAX_CANDIDATES:
                    diagnostics.append(dict(code='TABLE_CANDIDATE_LIMIT'));return candidates,diagnostics
    # The same source instance can produce a perpendicular equivalent proposal.
    unique=[]
    for c in candidates:
        ids={t['entity_id'] for t in c['texts']}
        if any(ids=={t['entity_id'] for t in k['texts']} and c['layout_id']==k['layout_id'] for k in unique):continue
        unique.append(c)
    return unique,diagnostics
