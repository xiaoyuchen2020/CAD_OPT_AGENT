"""Room enclosure pilot, independent of AR030 business rules and platform APIs.

Adapted design ideas: paired wall faces, observed jambs, seed containment and
explicit rejection diagnostics. No room-type area limits or optimization logic.
"""
from collections import defaultdict
import math

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

ROOM_VERSION = "room-net-v1.4"
ROOM_VOID_VERSION = "room-net-void-v1.5"
MAX_SEGMENTS = 5000
MAX_FACES = 2000


def merged(spans, lo, hi):
    out = []
    for a, b in sorted((max(lo, a), min(hi, b)) for a, b in spans if b > lo and a < hi):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        elif b > a:
            out.append([a, b])
    return out


def coverage(spans, length):
    return min(1., sum(b-a for a, b in merged(spans, 0, length)) / length)


class Segments:
    def __init__(self, segments, tolerance):
        self.segments = segments
        self.tol = tolerance
        self.lines = [LineString(s["points"]) for s in segments]
        self.tree = STRtree(self.lines)

    def near(self, line, margin):
        return [int(i) for i in self.tree.query(line.buffer(margin), predicate="intersects")]

    def support(self, a, b, lower, upper, outside=None):
        length = math.dist(a, b)
        if length <= self.tol:
            return 0., []
        ux, uy = (b[0]-a[0])/length, (b[1]-a[1])/length
        spans, ids = [], []
        for i in self.near(LineString([a, b]), upper + self.tol):
            c, d = self.segments[i]["points"]
            offsets = [(p[0]-a[0])*(-uy)+(p[1]-a[1])*ux for p in (c, d)]
            off = sum(offsets)/2
            if abs(offsets[0]-offsets[1]) > self.tol * 2 or not lower <= abs(off) <= upper:
                continue
            lo, hi = sorted((p[0]-a[0])*ux+(p[1]-a[1])*uy for p in (c, d))
            if hi <= 0 or lo >= length:
                continue
            mid = (max(0, lo)+min(length, hi))/2
            pt = Point(a[0]+mid*ux-off*uy, a[1]+mid*uy+off*ux)
            if outside is not None and outside.buffer(self.tol).covers(pt):
                continue
            spans.append((lo, hi))
            ids.append(i)
        return coverage(spans, length), ids


def close_supported_openings(context, opts):
    """Bridge matching gaps only with observed transverse jambs at both ends."""
    groups = defaultdict(list)
    tol = opts.tolerance_m
    for i, s in enumerate(context.segments):
        a, b = s["points"]
        length = math.dist(a, b)
        if length < opts.wall_min_m:
            continue
        angle = math.atan2(b[1]-a[1], b[0]-a[0]) % math.pi
        ux, uy = math.cos(angle), math.sin(angle)
        off = (a[0]*(-uy)+a[1]*ux+b[0]*(-uy)+b[1]*ux)/2
        lo, hi = sorted(p[0]*ux+p[1]*uy for p in (a, b))
        groups[(round(angle, 6), round(off/tol)*tol)].append((lo, hi, i))
    gaps = defaultdict(list)
    for (angle, off), lines in groups.items():
        spans = merged([(a, b) for a, b, _ in lines], -1e9, 1e9)
        for (_, lo), (hi, _) in zip(spans, spans[1:]):
            if .3 <= hi-lo <= opts.max_opening_m:
                source = [i for a, b, i in lines if abs(b-lo) <= tol*2 or abs(a-hi) <= tol*2]
                gaps[angle].append((off, lo, hi, source))
    bridges, seen = [], set()
    # Abort closure rather than truncate potential matches invisibly.
    if sum(len(v) for v in gaps.values()) > 500:
        return [], ["opening_candidate_limit"]
    for angle, candidates in gaps.items():
        ux, uy = math.cos(angle), math.sin(angle)
        def point(at, off):
            return (at*ux-off*uy, at*uy+off*ux)
        for i, (off, lo, hi, sources) in enumerate(candidates):
            for mate, ml, mh, ms in candidates[i+1:]:
                if not opts.wall_min_m <= abs(mate-off) <= opts.wall_max_m:
                    continue
                if max(abs(lo-ml), abs(hi-mh)) > tol*2:
                    continue
                jambs = []
                for at in (lo, hi):
                    a, b = point(at, off), point(at, mate)
                    fraction, indices = context.support(a, b, 0, tol*2)
                    if fraction < .98:
                        break
                    jambs.extend(indices)
                else:
                    support_ids = sorted({eid for j in sources+ms+jambs for eid in context.segments[j]["entity_ids"]})
                    for coordinate in (off, mate):
                        key = angle, coordinate, lo, hi
                        if key not in seen:
                            seen.add(key)
                            bridges.append(dict(points=[point(lo, coordinate), point(hi, coordinate)],
                                                entity_ids=support_ids, synthetic=True,
                                                method="paired_gap_with_two_observed_jambs"))
    return bridges, []


def analyze_room(records, seed, unit_to_m, opts, scope_bounds=None, other_seeds=()):
    """All numerical predicates use local metres; output geometry returns WCS."""
    origin = seed
    def local(p):
        return ((p[0]-origin[0])*unit_to_m, (p[1]-origin[1])*unit_to_m)
    def world(p):
        return [p[0]/unit_to_m+origin[0], p[1]/unit_to_m+origin[1]]
    segments, curved_ids = [], []
    segment_keys = {}
    by_id = {r["entity_id"]: r for r in records}
    local_window = box(-opts.radius_m, -opts.radius_m, opts.radius_m, opts.radius_m)
    for r in records:
        if not r.get("linear"):
            if r["paths"]:
                curved_ids.append(r["entity_id"])
            continue
        for path in r["paths"]:
            for a, b in zip(path, path[1:]):
                a, b = local(a), local(b)
                if math.dist(a, b) <= opts.tolerance_m:
                    continue
                clipped = LineString([a,b]).intersection(local_window)
                if clipped.geom_type != "LineString" or clipped.length <= opts.tolerance_m:
                    continue
                a,b = clipped.coords[0],clipped.coords[-1]
                key = tuple(sorted((tuple(round(v/opts.tolerance_m) for v in p) for p in (a,b))))
                if key in segment_keys:
                    segment_keys[key]["entity_ids"].append(r["entity_id"])
                else:
                    segment = dict(points=[a,b],entity_ids=[r["entity_id"]],synthetic=False)
                    segment_keys[key] = segment
                    segments.append(segment)
    diagnostics = []
    result = dict(status="unresolved", area_m2=None, candidate_area_m2=None,
                  candidates=[], diagnostics=diagnostics, input_segment_count=len(segments),
                  curved_entity_count=len(curved_ids), coverage="local_seed_region_only")
    if len(segments) > MAX_SEGMENTS:
        diagnostics.append("segment_limit_exceeded")
        return result
    if not segments:
        diagnostics.append("no_supported_linework")
        return result
    tol = opts.tolerance_m
    context = Segments(segments, tol)
    bridges = []
    if opts.close_openings:
        bridges, issues = close_supported_openings(context, opts)
        diagnostics.extend(issues)
    context = Segments(segments+bridges, tol)
    # Bound noding complexity before unary_union: a dense crossing grid can
    # create millions of faces even with a bounded number of input segments.
    intersections = 0
    for i, line in enumerate(context.lines):
        intersections += sum(int(j)>i for j in context.tree.query(line,predicate="intersects"))
        if intersections > 20000:
            diagnostics.append("intersection_limit_exceeded")
            return result
    paired = []
    for i, s in enumerate(context.segments):
        fraction, _ = context.support(*s["points"], opts.wall_min_m, opts.wall_max_m)
        if fraction >= .9 - 1e-9:
            paired.append(i)
    # Paired linework excludes unsupported axes/annotation chords. Full linework
    # is retained as an independent candidate path and interior-object diagnostic.
    def faces(indices):
        lines = [LineString([(round(x/tol)*tol, round(y/tol)*tol) for x, y in context.segments[i]["points"]]) for i in indices]
        return list(polygonize(unary_union(lines))) if lines else []
    raw_faces = faces(range(len(context.segments)))
    wall_faces = faces(paired)
    if len(raw_faces) + len(wall_faces) > MAX_FACES:
        diagnostics.append("face_limit_exceeded")
        return result
    point = Point(0, 0)
    limit = box(-opts.radius_m, -opts.radius_m, opts.radius_m, opts.radius_m)
    if scope_bounds:
        limit = limit.intersection(box(*local(scope_bounds[:2]), *local(scope_bounds[2:])))
    candidates, seen = [], set()
    void_excluded_candidates = 0
    for face, method in [(p, "paired_wall_faces") for p in wall_faces] + [(p, "observed_closed_face") for p in raw_faces]:
        # Interior objects are classified separately; no automatic equipment deduction.
        shape = Polygon(face.exterior)
        if not shape.is_valid or not shape.contains(point) or shape.boundary.distance(point) <= tol*2:
            continue
        key = shape.normalize().wkb
        if key in seen:
            continue
        seen.add(key)
        issues, edge_proofs, used = [], [], set()
        if not limit.buffer(-tol*2).covers(shape):
            issues.append("boundary_reaches_search_or_frame_limit")
        if any(shape.contains(Point(local(p))) for p in other_seeds):
            issues.append("contains_other_room_anchor")
        pts = list(shape.simplify(tol/2, preserve_topology=True).exterior.coords)
        for a, b in zip(pts, pts[1:]):
            obs, owned = context.support(a, b, 0, tol*2)
            outside, mates = context.support(a, b, opts.wall_min_m, opts.wall_max_m, shape)
            used.update(owned+mates)
            # Several substantial parallel faces on the same side are genuine
            # alternatives (finish lines, overlapping plans, equipment). Paired
            # support alone cannot choose a physical net face among them.
            length = math.dist(a,b);ux,uy=(b[0]-a[0])/length,(b[1]-a[1])/length
            face_offsets = defaultdict(list)
            for j in mates:
                c,d = context.segments[j]["points"]
                off = abs(((c[0]-a[0])*(-uy)+(c[1]-a[1])*ux+(d[0]-a[0])*(-uy)+(d[1]-a[1])*ux)/2)
                lo,hi = sorted((p[0]-a[0])*ux+(p[1]-a[1])*uy for p in (c,d))
                face_offsets[round(off/(tol*4))*(tol*4)].append((lo,hi))
            alternatives = sorted(off for off,spans in face_offsets.items() if coverage(spans,length)>=.5)
            if len(alternatives)>1:
                issues.append("competing_parallel_wall_faces")
            edge_proofs.append(dict(points=[world(a), world(b)], length_m=round(math.dist(a,b),6),
                                   observed_or_bridged_fraction=round(obs,6), outside_face_fraction=round(outside,6),
                                   parallel_face_offsets_m=alternatives,
                                   source_layers=sorted({by_id[eid]["layer"] for j in owned+mates for eid in context.segments[j]["entity_ids"]}),
                                   entity_ids=sorted({eid for j in owned+mates for eid in context.segments[j]["entity_ids"]})))
            if obs < .95 - 1e-9 or outside < .9 - 1e-9:
                issues.append("insufficient_outside_wall_face_support")
        # Closed loops strictly inside the room might be columns, equipment or
        # annotation boxes. Their area cannot be silently deducted or ignored.
        internal = []
        for f in raw_faces:
            inner = Polygon(f.exterior)
            if shape.contains(inner) and shape.boundary.distance(inner) > tol*2:
                if not any(inner.equals(x) for x in internal):
                    internal.append(inner)
        voids = []
        for eid in opts.void_entity_ids:
            rec = next(r for r in records if r["entity_id"] == eid)
            if not rec.get("linear") or len(rec["paths"]) != 1 or rec["paths"][0][0] != rec["paths"][0][-1]:
                raise ValueError("扣除区域必须是明确选择的闭合直线多段线。")
            poly = Polygon([local(p) for p in rec["paths"][0]])
            if not poly.is_valid or poly.covers(point):
                raise ValueError("扣除区域无效或覆盖室内测量点。")
            voids.append(poly)
        # A small competing face may omit a valid selected column. Reject that
        # candidate, not the entire request before reaching the enclosing room.
        if any(not shape.contains(poly) for poly in voids):
            void_excluded_candidates += 1
            continue
        unidentified = [p for p in internal if not any(v.buffer(tol*2).covers(p) for v in voids)]
        # Curves inside the enclosure require review; this version measures only
        # straight wall faces and cannot infer the role of arcs or curved voids.
        interior_curves = [r["entity_id"] for r in records if r["entity_id"] in curved_ids and
                           any(shape.buffer(-tol*2).intersects(LineString([local(p) for p in path])) for path in r["paths"])]
        if unidentified:
            issues.append("unclassified_interior_closed_objects")
        if interior_curves:
            issues.append("unclassified_interior_curves")
        net = shape.difference(unary_union(voids)) if voids else shape
        if net.geom_type != "Polygon" or not net.is_valid:
            issues.append("invalid_net_polygon")
            net = shape
        selected_bridges = [context.segments[j] for j in sorted(used) if context.segments[j]["synthetic"]
                            and shape.boundary.intersection(context.lines[j].buffer(tol*2)).length > tol*2]
        entity_ids = sorted({eid for j in used for eid in context.segments[j]["entity_ids"]} | set(opts.void_entity_ids))
        candidates.append(dict(method=method, candidate_area_m2=round(net.area,6),
            shell_area_m2=round(shape.area,6), perimeter_m=round(net.length,6),
            boundary=[world(p) for p in net.exterior.coords], holes=[[world(p) for p in ring.coords] for ring in net.interiors],
            bbox=[*world(net.bounds[:2]), *world(net.bounds[2:])],
            edge_evidence=edge_proofs, entity_ids=entity_ids,
            inferred_closures=[s | {"points":[world(p) for p in s["points"]], "length_m":round(math.dist(*s["points"]),6)} for s in selected_bridges],
            unresolved_interior_objects=[dict(boundary=[world(p) for p in p.exterior.coords], area_m2=round(p.area,6)) for p in unidentified],
            unresolved_curve_entity_ids=interior_curves, issues=sorted(set(issues))))
    candidates.sort(key=lambda c: (len(c["issues"]), c["candidate_area_m2"]))
    accepted = [c for c in candidates if not c["issues"]]
    if len(accepted) > 1:
        diagnostics.append("multiple_supported_boundaries")
    result["candidate_count"] = len(candidates)
    if opts.void_entity_ids:
        result["void_excluded_candidate_count"] = void_excluded_candidates
    result["candidates"] = candidates[:6]
    if len(candidates) > 6:
        diagnostics.append("candidate_display_truncated")
    if not candidates:
        if void_excluded_candidates:
            diagnostics.append("no_boundary_contains_selected_voids")
        diagnostics.append("no_supported_enclosing_boundary")
        return result
    chosen = accepted[0] if accepted else candidates[0]
    result.update(selected=chosen, candidate_area_m2=chosen["candidate_area_m2"],
                  status="measured" if len(accepted)==1 and not diagnostics else "needs_review")
    if result["status"] == "measured":
        result["area_m2"] = chosen["candidate_area_m2"]
    diagnostics.extend(chosen["issues"])
    return result
