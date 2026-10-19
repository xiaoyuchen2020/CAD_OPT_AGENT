"""Geometry-only structural proposals. No labels, layers, files or model dependencies.

All thresholds are dimensionless drafting relationships, not drawing-specific
exceptions. Proposals are evidence for review, never confirmed room boundaries.
"""
from dataclasses import dataclass, asdict
import math

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

VERSION = 'vector-relations-v1'


@dataclass(frozen=True)
class Primitive:
    entity_id: str
    kind: str
    points: tuple
    center: tuple | None = None
    radius: float | None = None
    sweep_degrees: float | None = None


@dataclass(frozen=True)
class RecognitionOptions:
    angular_tolerance_degrees: float = 1.0
    radial_tolerance_ratio: float = .04
    swing_min_degrees: float = 80.0
    swing_max_degrees: float = 100.0
    strip_min_aspect: float = 8.0
    strip_min_overlap: float = .8
    frame_alignment_ratio: float = .02
    max_primitives: int = 12000
    max_pair_checks: int = 250000


def recognize_vectors(primitives, options=None):
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
            found.append(hit)
        return found

    adjacency={i:set() for i in range(len(lines))}
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
            ids=[p.entity_id,q.entity_id,*interior,*caps[0],*caps[1]]
            add('window', ids, 'observed_capped_strip_with_interior_parallel_rails',
                spacing=spacing, overlap=overlap, interior_rail_count=len(interior))
            window_ids.update(ids)
    door_ids={eid for c in candidates if c['kind']=='door' for eid in c['entity_ids']}
    for i,j,spacing,overlap in pairs:
        p,g=lines[i];q,h=lines[j]
        if {p.entity_id,q.entity_id} & (window_ids|door_ids):
            continue
        caps=cap_ids(p,q,spacing)
        if not any(caps):
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
    return result
