"""Second vector baseline: connected geometry support, opt-in and ablatable.

No labels or metadata are available to this module. Original v1 remains frozen.
"""
from dataclasses import dataclass, asdict, replace
import math
from statistics import median
from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely.strtree import STRtree
from .vector_recognition import Primitive, RecognitionOptions

VERSION = 'vector-relations-v2'

@dataclass(frozen=True)
class Refinements:
    join_segments: bool = True
    door_completion: bool = True
    window_support: bool = True
    wall_context: bool = True

def _recognize_core(primitives, options, features):
    """Propose swing doors, capped multi-rail windows and connected paired walls.

    Coordinates may have any consistent unit. Classification is intentionally
    conservative about unsupported geometry and reports truncation explicitly.
    Entity IDs are opaque provenance and are never used to select a rule.
    """
    options = options or RecognitionOptions()
    if any(not isinstance(p, Primitive) for p in primitives):
        raise TypeError('Only typed geometry primitives are accepted')
    if len({p.entity_id for p in primitives}) != len(primitives):
        raise ValueError('Primitive IDs must be unique within the scope')
    if any(not math.isfinite(v) for p in primitives for pt in p.points for v in pt):
        raise ValueError('Non-finite geometry')
    result = dict(version=VERSION, status='needs_review', options=asdict(options),
        candidates=[], entity_roles={}, diagnostics=[], complete=True,
        semantics='Geometry proposals only; no confirmed door/window/wall or room/net-area assertion.')
    if len(primitives) > options.max_primitives:
        result.update(complete=False, diagnostics=['primitive_budget_exceeded'])
        return result
    lines = [(p, LineString(p.points)) for p in primitives if p.kind == 'line'
             and len(p.points) == 2 and math.dist(*p.points) > 0]
    tree = STRtree([g for _, g in lines])
    candidates = []; keys = set(); sin_tol = math.sin(math.radians(options.angular_tolerance_degrees))

    def add(kind, ids, reason, **evidence):
        key = (kind, tuple(sorted(set(ids))))
        if key in keys:
            return
        keys.add(key)
        candidates.append(dict(kind=kind, entity_ids=list(key[1]), reason=reason,
                               status='candidate', evidence=evidence))

    for arc in primitives:
        if (arc.kind != 'arc' or arc.center is None or not arc.radius or not arc.sweep_degrees
                or not options.swing_min_degrees <= abs(arc.sweep_degrees) <= options.swing_max_degrees):
            continue
        radius = arc.radius; tol = radius * options.radial_tolerance_ratio
        for tip, other in (arc.points, arc.points[::-1]):
            radial = LineString([arc.center, tip]); ids = []
            for raw in tree.query(radial.buffer(tol), predicate='intersects'):
                p, g = lines[int(raw)]
                if (g.length >= radius * .8 and g.length <= radius * 1.2
                        and radial.buffer(tol).covers(g)
                        and g.distance(Point(arc.center)) <= tol and g.distance(Point(tip)) <= tol):
                    ids.append(p.entity_id)
            if ids:
                add('door', [arc.entity_id, *ids], 'circular_swing_with_observed_radial_leaf',
                    hinge=arc.center, open_tip=tip, closed_tip=other, radius=radius,
                    sweep_degrees=arc.sweep_degrees)

    if features.door_completion:
        # A swing identifies only part of a symbol. Grow along observed connected
        # leaf/frame segments inside bounded hinge/leaf/end neighborhoods.
        for candidate in list(candidates):
            if candidate['kind']!='door':continue
            ev=candidate['evidence'];r=ev['radius'];h=ev['hinge'];o=ev['open_tip'];c=ev['closed_tip']
            band=LineString([h,o]).buffer(r*.12)
            zones=unary_union([band,Point(h).buffer(r*.22),Point(c).buffer(r*.22)])
            local=[]
            for idx in tree.query(zones,predicate='intersects'):
                p,g=lines[int(idx)]
                if zones.covers(g) and (band.covers(g) or g.length<=r*.3):local.append((p,g))
            connected=unary_union([g.buffer(r*.01) for _,g in local])
            parts=list(connected.geoms) if hasattr(connected,'geoms') else [connected]
            seeds=[Point(h),Point(c),Point(o)]
            accepted=[part for part in parts if any(part.distance(seed)<=r*.02 for seed in seeds)]
            extra=[p.entity_id for p,g in local if any(part.intersects(g) for part in accepted)]
            candidate['entity_ids']=sorted(set(candidate['entity_ids']+extra))
            ev['completion']='observed connected leaf and local jamb segments; no inferred closure'

    pairs = []; checked = 0
    for i, (p, g) in enumerate(lines):
        a, b = p.points; length = g.length
        u = ((b[0]-a[0])/length, (b[1]-a[1])/length)
        for raw in tree.query(g.buffer(length / options.strip_min_aspect), predicate='intersects'):
            j = int(raw)
            if j <= i:
                continue
            checked += 1
            if checked > options.max_pair_checks:
                result.update(complete=False, diagnostics=['pair_budget_exceeded'])
                break
            q, h = lines[j]; c, d = q.points
            v = ((d[0]-c[0])/h.length, (d[1]-c[1])/h.length)
            if abs(u[0]*v[1]-u[1]*v[0]) > sin_tol:
                continue
            offsets = [(x[0]-a[0])*(-u[1])+(x[1]-a[1])*u[0] for x in (c,d)]
            spacing = abs(sum(offsets)/2)
            if spacing <= min(length, h.length)*1e-6:
                continue
            projected = sorted((x[0]-a[0])*u[0]+(x[1]-a[1])*u[1] for x in (c,d))
            overlap = min(length,projected[1])-max(0,projected[0])
            if overlap < options.strip_min_aspect*spacing or overlap < options.strip_min_overlap*min(length,h.length):
                continue
            # Both lines must remain parallel across their actual overlap.
            if abs(offsets[0]-offsets[1]) > spacing*.1:
                continue
            pairs.append((i,j,spacing,overlap))
        if not result['complete']:
            break
    if not result['complete']:
        # Partial scans are not silently treated as complete semantic output.
        result['candidates'] = candidates
        return result

    def cap_ids(p, q, spacing):
        # Direct observed end caps only. Never invent a segment between endpoints.
        a,b=p.points; c,d=q.points
        if math.dist(a,d)+math.dist(b,c) < math.dist(a,c)+math.dist(b,d):
            c,d=d,c
        found=[]
        for x,y in ((a,c),(b,d)):
            cap=LineString([x,y]);tol=spacing*options.frame_alignment_ratio
            hit=[]
            for raw in tree.query(cap.buffer(tol), predicate='intersects'):
                r,g=lines[int(raw)]
                if r.entity_id in (p.entity_id,q.entity_id):
                    continue
                if g.distance(Point(x)) <= tol and g.distance(Point(y)) <= tol and cap.buffer(tol).covers(g):
                    hit.append(r.entity_id)
            if not hit and features.window_support and cap.length>0:
                # Split or extended crossbars may cover a cap without any one
                # primitive matching its exact endpoints. Use observed coverage.
                cu=((y[0]-x[0])/cap.length,(y[1]-x[1])/cap.length)
                support=[]
                for raw in tree.query(cap.buffer(tol),predicate='intersects'):
                    r,g=lines[int(raw)]
                    if r.entity_id in (p.entity_id,q.entity_id):continue
                    ga,gb=g.coords;gu=((gb[0]-ga[0])/g.length,(gb[1]-ga[1])/g.length)
                    if abs(cu[0]*gu[1]-cu[1]*gu[0])>sin_tol:continue
                    support.append((r.entity_id,g))
                if support and unary_union([g.buffer(tol) for _,g in support]).covers(cap):
                    hit=[eid for eid,g in support]
            found.append(hit)
        return found

    adjacency={i:set() for i in range(len(lines))}
    swing_radii=[c['evidence']['radius'] for c in candidates if c['kind']=='door']
    reference_radius=median(swing_radii) if len(swing_radii)>=3 else None
    wall_rejections=[]
    for i,j,spacing,overlap in pairs:
        adjacency[i].add(j);adjacency[j].add(i)
    window_ids=set()
    for i,j,spacing,overlap in pairs:
        p,g=lines[i];q,h=lines[j];caps=cap_ids(p,q,spacing)
        if not all(caps):
            continue
        interior=[]
        strip=LineString(p.points).union(LineString(q.points)).convex_hull
        for k in adjacency[i] & adjacency[j]:
            r,line=lines[k]
            if strip.buffer(spacing*options.frame_alignment_ratio).covers(line) and line.length>=overlap*.95:
                interior.append(r.entity_id)
        if interior:
            cap_roles=caps[0]+caps[1]
            if features.window_support:
                lengths={r.entity_id:seg.length for r,seg in lines}
                cap_roles=[eid for eid in cap_roles if lengths[eid]<=spacing*1.1]
            ids=[p.entity_id,q.entity_id,*interior,*cap_roles]
            add('window', ids, 'observed_capped_strip_with_interior_parallel_rails',
                spacing=spacing, overlap=overlap, interior_rail_count=len(interior),cap_support_ids=sorted(set(caps[0]+caps[1])))
            window_ids.update(ids)
    door_ids={eid for c in candidates if c['kind']=='door' for eid in c['entity_ids']}
    for i,j,spacing,overlap in pairs:
        p,g=lines[i];q,h=lines[j]
        if {p.entity_id,q.entity_id} & (window_ids|door_ids):
            continue
        caps=cap_ids(p,q,spacing)
        if not any(caps):
            continue
        if features.wall_context and reference_radius and spacing<reference_radius*.1:
            # Thin double lines alone also describe cabinet and door panels.
            # Keep a thin wall hypothesis only with two observed outside links;
            # absent a reliable multi-swing scale, do not impose a size prior.
            strip=g.union(h).convex_hull;support=[]
            a,b=p.points;u=((b[0]-a[0])/g.length,(b[1]-a[1])/g.length)
            for raw in tree.query(strip.buffer(spacing*.05),predicate='intersects'):
                other,seg=lines[int(raw)]
                if other.entity_id in (p.entity_id,q.entity_id):continue
                sa,sb=seg.coords;v=((sb[0]-sa[0])/seg.length,(sb[1]-sa[1])/seg.length)
                if abs(u[0]*v[0]+u[1]*v[1])>sin_tol:continue
                if seg.difference(strip.buffer(spacing*.05)).length>=spacing*2:
                    support.append(other.entity_id)
            if len(set(support))<2:
                wall_rejections.append(dict(entity_ids=[p.entity_id,q.entity_id],reason='thin_strip_without_outside_context',
                    spacing_to_swing_radius=spacing/reference_radius,outside_support_ids=sorted(set(support))))
                continue
        add('wall', [p.entity_id,q.entity_id], 'parallel_faces_with_observed_return',
            spacing=spacing, overlap=overlap, return_entity_ids=sorted(set(caps[0]+caps[1])))
    # Different semantic proposals on one entity remain ambiguous, not priority wins.
    roles={}
    for c in candidates:
        for eid in c['entity_ids']:
            roles.setdefault(eid,set()).add(c['kind'])
    result['entity_roles']={eid:next(iter(v)) if len(v)==1 else 'ambiguous' for eid,v in roles.items()}
    result['candidates']=candidates
    result['pair_checks']=checked
    result['wall_rejections']=wall_rejections
    return result


def join_collinear(primitives):
    """Merge touching collinear pieces with bounded numerical tolerance.

    Every resulting primitive carries all original contributors. Complete-line
    alignment is checked against the group anchor to prevent bent chain drift.
    """
    linear=[p for p in primitives if p.kind=='line' and len(p.points)==2 and math.dist(*p.points)>0]
    shapes=[LineString(p.points) for p in linear];tree=STRtree(shapes);seen=set();out=[];mapping={}
    order=sorted(range(len(linear)),key=lambda i:tuple(sorted(linear[i].points)))
    for i in order:
        if i in seen:continue
        anchor=linear[i];a,b=anchor.points;length=shapes[i].length
        u=((b[0]-a[0])/length,(b[1]-a[1])/length);group={i};stack=[i];seen.add(i)
        while stack:
            j=stack.pop();g=shapes[j]
            for raw in tree.query(g.buffer(g.length*.001),predicate='intersects'):
                k=int(raw)
                if k in seen:continue
                h=shapes[k];tol=min(length,h.length,g.length)*.001
                if g.distance(h)>tol:continue
                c,d=linear[k].points
                offsets=[abs((p[0]-a[0])*(-u[1])+(p[1]-a[1])*u[0]) for p in (c,d)]
                if max(offsets)>tol:continue
                seen.add(k);group.add(k);stack.append(k)
        vertices=[v for k in group for v in linear[k].points]
        positions=[(p[0]-a[0])*u[0]+(p[1]-a[1])*u[1] for p in vertices]
        lo,hi=min(positions),max(positions)
        pid=f'joined{len(out)}'
        out.append(Primitive(pid,'line',((a[0]+lo*u[0],a[1]+lo*u[1]),(a[0]+hi*u[0],a[1]+hi*u[1]))))
        mapping[pid]=sorted(linear[k].entity_id for k in group)
    for p in primitives:
        if p.kind!='line' or math.dist(*p.points)==0:
            pid=f'joined{len(out)}';out.append(replace(p,entity_id=pid));mapping[pid]=[p.entity_id]
    return out,mapping


def recognize_vectors_v2(primitives, options=None, features=None):
    options=options or RecognitionOptions();features=features or Refinements()
    if any(not isinstance(p,Primitive) for p in primitives):raise TypeError('Geometry primitives required')
    if len({p.entity_id for p in primitives})!=len(primitives):raise ValueError('Duplicate primitive IDs')
    if len(primitives)>options.max_primitives:
        return _recognize_core(primitives,options,features)
    if features.door_completion:
        options=replace(options,swing_min_degrees=20.,swing_max_degrees=160.)
    if features.join_segments:
        prepared,mapping=join_collinear(primitives)
    else:prepared=primitives;mapping={p.entity_id:[p.entity_id] for p in primitives}
    result=_recognize_core(prepared,options,features)
    for c in result['candidates']:
        c['entity_ids']=sorted({eid for pid in c['entity_ids'] for eid in mapping[pid]})
        if 'return_entity_ids' in c['evidence']:
            c['evidence']['return_entity_ids']=sorted({eid for pid in c['evidence']['return_entity_ids'] for eid in mapping[pid]})
        if 'cap_support_ids' in c['evidence']:
            c['evidence']['cap_support_ids']=sorted({eid for pid in c['evidence']['cap_support_ids'] for eid in mapping[pid]})
    for c in result.get('wall_rejections',[]):
        for key in ('entity_ids','outside_support_ids'):
            c[key]=sorted({eid for pid in c[key] for eid in mapping[pid]})
    roles={}
    for c in result['candidates']:
        for eid in c['entity_ids']:roles.setdefault(eid,set()).add(c['kind'])
    result['entity_roles']={eid:next(iter(v)) if len(v)==1 else 'ambiguous' for eid,v in roles.items()}
    result['refinements']=asdict(features)
    result['preparation']=dict(original_count=len(primitives),joined_count=len(prepared),tolerance='0.001 of smaller participating segment')
    return result
