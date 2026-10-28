"""Source-preserving primitive decomposition and explicit endpoint connectivity.

The graph describes supplied geometry, never an engineering route census. No
coordinate is moved to close a gap. Tolerances are supplied in drawing units.
"""
import math
from collections import defaultdict, deque
from copy import deepcopy
from hashlib import sha256

def stable_id(prefix, *parts):
    # Dependency-free IDs keep the same kernel usable inside the minimal sandbox.
    return prefix + "_" + sha256("\x1f".join(parts).encode()).hexdigest()[:24]

VERSION = 'investigation-topology-v2'


def xy_ocs(native):
    from ezdxf.math import OCS
    normal=native.get('extrusion',[0,0,1])
    if len(normal)!=3 or not all(math.isfinite(v) for v in normal) or normal[0]!=0 or normal[1]!=0 or normal[2]==0:
        raise ValueError('TILTED_OR_INVALID_OCS_UNSUPPORTED')
    return OCS(normal),1 if normal[2]>0 else -1


def normalize_arc(native):
    n=deepcopy(native);ocs,sign=xy_ocs(n);center=n['center'];radius=n['radius']
    points=[ocs.to_wcs([center[0]+radius*math.cos(math.radians(t)),center[1]+radius*math.sin(math.radians(t)),center[2]]) for t in (n['start_angle'],n['end_angle'])]
    c=ocs.to_wcs(center);angles=[math.degrees(math.atan2(p.y-c.y,p.x-c.x)) for p in points]
    if sign<0:angles.reverse()
    n.update(center=list(c),start_angle=angles[0],end_angle=angles[1],extrusion=[0,0,1])
    return n


def primitives(records):
    out, diagnostics = [], []
    for record in records:
        native = record.get('native', {})
        try:
            if record.get('status') != 'ready':
                raise ValueError('SOURCE_NOT_READY')
            kind = record['kind']
            if kind in ('LINE', 'ARC'):
                rows = [(kind, normalize_arc(native) if kind=='ARC' else deepcopy(native), 0)]
            elif kind in ('LWPOLYLINE', 'POLYLINE'):
                ocs,orientation=xy_ocs(native)
                vertices = native.get('vertices', [])
                rows = []
                for i in range(len(vertices) if native.get('closed') else len(vertices)-1):
                    a, b = vertices[i], vertices[(i+1) % len(vertices)]
                    z = native.get('elevation', 0) if kind == 'LWPOLYLINE' else a[2]
                    if isinstance(z, list): z = z[2]
                    if kind == 'POLYLINE' and b[2] != z:
                        raise ValueError('NONPLANAR_POLYLINE')
                    if kind=='POLYLINE' and native.get('elevation'):
                        elevation=native['elevation'];z+=elevation[2] if isinstance(elevation,list) else elevation
                    bulge = (a[4] if kind == 'LWPOLYLINE' else a[3])*orientation
                    a,b=ocs.to_wcs([*a[:2],z]),ocs.to_wcs([*b[:2],z]);z=a.z
                    a,b=list(a),list(b)
                    if abs(bulge) < 1e-15:
                        rows.append(('LINE', dict(start=[*a[:2], z], end=[*b[:2], z]), i))
                    else:
                        from ezdxf.math import bulge_to_arc
                        center, start, end, radius = bulge_to_arc(a[:2], b[:2], bulge)
                        rows.append(('ARC', dict(center=[*center, z], radius=radius,
                            start_angle=math.degrees(start), end_angle=math.degrees(end), extrusion=[0,0,1]), i))
            else:
                raise ValueError('UNSUPPORTED_PRIMITIVE_KIND')
            prepared = []
            for kind, n, i in rows:
                if kind == 'LINE':
                    ends = [n['start'], n['end']]
                    length = math.dist(*ends)
                else:
                    if list(n.get('extrusion', [0,0,1])) != [0,0,1]:
                        raise ValueError('ARC_OCS_UNSUPPORTED')
                    center, radius = n['center'], n['radius']
                    sweep = (n['end_angle']-n['start_angle']) % 360
                    ends = [[center[0]+radius*math.cos(math.radians(t)),
                             center[1]+radius*math.sin(math.radians(t)), center[2]]
                            for t in (n['start_angle'], n['end_angle'])]
                    length = radius*math.radians(sweep)
                if not math.isfinite(length) or length <= 0 or not all(math.isfinite(v) for p in ends for v in p):
                    raise ValueError('DEGENERATE_OR_NONFINITE_PRIMITIVE')
                prepared.append(dict(entity_id=stable_id('gp', VERSION, record['entity_id'], str(i)),
                    source_entity_id=record['entity_id'], source_segment_index=i,
                    entity_path=record.get('entity_path', []), layout_id=record['layout_id'],
                    layer=record.get('layer', ''), kind=kind, native=n, status='ready',
                    endpoints=ends, length_drawing_units=length,
                    source_kind=record['kind'], approximation='native_exact'))
            out.extend(prepared)
        except (KeyError, ValueError, IndexError, TypeError, ZeroDivisionError) as exc:
            diagnostics.append(dict(entity_id=record['entity_id'], code=str(exc)))
    return out, diagnostics



def endpoint_tree(points,depth=0):
    if not points:return None
    axis=depth%3;points=sorted(points,key=lambda row:(row[2][axis],row[0],row[1]));mid=len(points)//2
    return (points[mid],axis,endpoint_tree(points[:mid],depth+1),endpoint_tree(points[mid+1:],depth+1))


def nearest_other(tree,point,owner,best=None):
    if tree is None:return best
    (i,end,p),axis,left,right=tree
    if i!=owner:
        candidate=(math.dist(point,p),i,end)
        if best is None or candidate<best:best=candidate
    delta=point[axis]-p[axis]
    near,far=(left,right) if delta<0 else (right,left)
    best=nearest_other(near,point,owner,best)
    if best is None or abs(delta)<=best[0]:best=nearest_other(far,point,owner,best)
    return best

def network(records, tolerance, seed_entity_ids=(), census_entity_ids=()):
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('POSITIVE_FINITE_CONNECTION_TOLERANCE_REQUIRED')
    edges, diagnostics = primitives(records)
    # Direct endpoint adjacency avoids transitive snapping of a chain of small gaps.
    buckets, endpoints, neighbors, connections = defaultdict(list), [], defaultdict(set), []
    for i, edge in enumerate(edges):
        for end, point in enumerate(edge['endpoints']):
            cell = tuple(math.floor(v/tolerance) for v in point)
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    for dz in (-1,0,1):
                        for j, other_end, other in buckets[tuple(cell[k]+d for k,d in enumerate((dx,dy,dz)))]:
                            distance = math.dist(point, other)
                            if i != j and distance <= tolerance:
                                neighbors[(i,end)].add((j,other_end)); neighbors[(j,other_end)].add((i,end))
                                connections.append(dict(a=[edges[j]['entity_id'],other_end], b=[edge['entity_id'],end], distance=distance))
            buckets[cell].append((i,end,point)); endpoints.append((i,end,point))
    starts = {i for i,e in enumerate(edges) if e['source_entity_id'] in seed_entity_ids}
    if seed_entity_ids and not starts: raise ValueError('NO_SUPPORTED_SEED_PRIMITIVE')
    visited = set(starts); todo = deque(starts)
    while todo:
        i = todo.popleft()
        for end in (0,1):
            for j,_ in neighbors[(i,end)]:
                if j not in visited: visited.add(j); todo.append(j)
    active = visited if seed_entity_ids else set(range(len(edges)))
    unresolved, branches = [], []
    search_tree=endpoint_tree(endpoints)
    for i,end,p in endpoints:
        if i not in active: continue
        linked = neighbors[(i,end)]
        if len(linked)>1:
            branches.append(dict(primitive_id=edges[i]['entity_id'],end=end,
                alternatives=[edges[j]['entity_id'] for j,_ in sorted(linked)]))
        if not linked:
            near=nearest_other(search_tree,p,i)
            unresolved.append(dict(primitive_id=edges[i]['entity_id'],end=end,point=p,
                nearest_other_endpoint=None if near is None else dict(primitive_id=edges[near[1]]['entity_id'],end=near[2],distance=near[0]),
                state='open_endpoint_not_auto_closed'))
    by_id={e['entity_id']:e for e in edges}
    def outward(edge,end):
        n=edge['native']
        if edge['kind']=='LINE':
            a,b=edge['endpoints'];v=[b[k]-a[k] for k in (0,1)];return [t*(1 if end==0 else -1) for t in v]
        angle=math.radians(n['start_angle'] if end==0 else n['end_angle'])
        return [-math.sin(angle)*(1 if end==0 else -1),math.cos(angle)*(1 if end==0 else -1)]
    for c in connections:
        va=outward(by_id[c['a'][0]],c['a'][1]);vb=outward(by_id[c['b'][0]],c['b'][1])
        cosine=sum(a*b for a,b in zip(va,vb))/(math.hypot(*va)*math.hypot(*vb))
        c['turn_degrees']=180-math.degrees(math.acos(max(-1,min(1,cosine))))
        c['transition']='tangent_continuation_candidate' if c['turn_degrees']<1e-6 else 'corner_or_branch_candidate'
    # Coverage denominator is caller-declared independently of the reachable set.
    census = set(census_entity_ids)
    census_edges = [e for e in edges if e['source_entity_id'] in census]
    total = sum(e['length_drawing_units'] for e in census_edges)
    covered = sum(edges[i]['length_drawing_units'] for i in active if edges[i]['source_entity_id'] in census)
    missing = sorted(census-{e['source_entity_id'] for e in edges})
    return dict(primitives=edges, connections=connections,
        graph_scope='all_selected_source_primitives',
        traversal_scope='seed_reachable_component' if seed_entity_ids else 'all_selected_source_primitives',
        endpoint_counts=dict(input_endpoints=len(endpoints),traversed_endpoints=2*len(active),
            open_traversed_endpoints=len(unresolved),open_input_endpoints=sum(not neighbors[(i,end)] for i,end,_ in endpoints)),
        reachable_primitive_ids=[edges[i]['entity_id'] for i in sorted(active)],
        branches=branches, open_endpoints=unresolved, diagnostics=diagnostics,
        connection_tolerance_drawing_units=tolerance, coordinates_modified=False,
        coverage=dict(basis='caller_declared_source_census',denominator_length=total if census else None,
            reached_length=covered if census else None,
            fraction=covered/total if total and not missing else None,
            unsupported_census_entity_ids=missing,engineering_route_complete=False),
        unresolved=['interior_intersections_not_split','opposing_side_correspondence_unconfirmed',
                    'endpoint_and_transition_extrema_unproven'], global_minimum_proven=False)
