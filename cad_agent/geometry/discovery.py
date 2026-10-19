"""Vocabulary-free enclosure discovery; topology does not establish room identity.

Neither room names nor layer names participate in enclosure construction. Raw
faces and paired-face candidates are both retained, with explicit conflicts.
"""
import math
import json

from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union

from cad_agent.contracts import stable_id
from .rooms import Segments, close_supported_openings, MAX_SEGMENTS, MAX_FACES
from .contours import build_contours

DISCOVERY_VERSION = "room-discovery-v1.5-contours1.1"


def discover_enclosures(records, bounds, factor, opts, *, extra_bridges=()):
    origin = bounds[:2]
    def local(p):
        return ((p[0]-origin[0])*factor, (p[1]-origin[1])*factor)
    def world(p):
        return [p[0]/factor+origin[0], p[1]/factor+origin[1]]
    window = box(0, 0, (bounds[2]-bounds[0])*factor, (bounds[3]-bounds[1])*factor)
    tol = opts.tolerance_m
    result = dict(candidates=[], diagnostics=[], complete=False)
    segments, keys = [], {}
    curves = []
    for rec in records:
        if not rec.get("linear"):
            curves.append(rec["entity_id"])
            continue
        for path in rec["paths"]:
            for a, b in zip(path, path[1:]):
                line = LineString([local(a), local(b)]).intersection(window)
                if line.geom_type != "LineString" or line.length <= tol:
                    continue
                points = [tuple(round(v/tol)*tol for v in p) for p in (line.coords[0], line.coords[-1])]
                if math.dist(*points) <= tol:
                    continue
                key = tuple(sorted(points))
                if key in keys:
                    keys[key]["entity_ids"].append(rec["entity_id"])
                else:
                    item = dict(points=points, entity_ids=[rec["entity_id"]], synthetic=False)
                    segments.append(item); keys[key] = item
                if len(segments) > MAX_SEGMENTS:
                    result["diagnostics"].append("segment_limit_exceeded")
                    return result
    result["input_segment_count"] = len(segments)
    result["curved_entity_count"] = len(curves)
    if curves:
        result["diagnostics"].append("curved_boundaries_not_discovered")
    if not segments:
        result["diagnostics"].append("no_supported_linework")
        return result
    context = Segments(segments, tol)
    bridges = []
    if opts.close_openings:
        bridges, issues = close_supported_openings(context, opts)
        result["diagnostics"].extend(issues)
        # Gap matching rounds angles; snap generated endpoints to the same
        # grid as observed segments so vertical jambs actually node together.
        bridges = [dict(b, points=[tuple(round(v/tol)*tol for v in p) for p in b["points"]]) for b in bridges]
        for bridge in extra_bridges:
            points = [tuple(round(v/tol)*tol for v in local(p)) for p in bridge["points"]]
            if window.covers(LineString(points)):
                bridges.append(dict(bridge, points=points, synthetic=True))
    result["opening_sides"] = [dict(b, points=[world(p) for p in b["points"]]) for b in bridges]
    context = Segments(segments+bridges, tol)
    intersections = 0
    for i, line in enumerate(context.lines):
        intersections += sum(int(j)>i for j in context.tree.query(line, predicate="intersects"))
        if intersections > 20000:
            result["diagnostics"].append("intersection_limit_exceeded")
            return result
    paired = [i for i, s in enumerate(context.segments)
              if context.support(*s["points"], opts.wall_min_m, opts.wall_max_m)[0] >= .9-1e-9]
    faces = []
    for indices, method in [(paired, "paired_wall_faces"), (range(len(context.lines)), "observed_closed_face")]:
        if indices:
            for face in polygonize(unary_union([context.lines[i] for i in indices])):
                faces.append((face, method))
                if len(faces)>MAX_FACES:
                    result["diagnostics"].append("face_limit_exceeded")
                    return result
    library, models, issues = build_contours(faces, records, context, origin, factor,
                                            opts.model_copy(update={"bbox": bounds}))
    result["contours"] = library
    result["diagnostics"].extend(issues)
    contour_by_id = {c["contour_id"]: c for c in library}
    seen = set()
    for face, method in faces:
        # The two graphs may split one edge at different collinear nodes.
        # Canonicalize the outline before deduplication, retaining provenance.
        shape = Polygon(face.exterior).simplify(0).normalize()
        if not shape.is_valid or shape.area <= tol*tol:
            continue
        if shape.wkb in seen:
            continue
        seen.add(shape.wkb)
        boundary = list(shape.exterior.coords)
        proofs, ids, supported = [], set(), 0.
        for a, b in zip(boundary, boundary[1:]):
            observed, owned = context.support(a, b, 0, tol*2)
            outside, mates = context.support(a, b, opts.wall_min_m, opts.wall_max_m, shape)
            length = math.dist(a,b)
            supported += length*outside
            edge_ids = {eid for j in owned+mates for eid in context.segments[j]["entity_ids"]}
            ids.update(edge_ids)
            proofs.append(dict(points=[world(a),world(b)],length_m=length,
                               observed_or_bridged_fraction=observed,outside_face_fraction=outside,
                               entity_ids=sorted(edge_ids)))
        issues = ["room_identity_unconfirmed"]
        paired_fraction = supported/shape.length
        if not window.buffer(-tol*2).covers(shape):
            issues.append("boundary_reaches_search_or_frame_limit")
        if any(e["outside_face_fraction"]<.9-1e-9 for e in proofs):
            issues.append("insufficient_outside_wall_face_support")
        # Tiny symbol loops can also have parallel neighbours. Retain them as
        # low-confidence enclosures; do not let them crowd out room-scale spaces.
        # This is a wall-scale diagnostic, not a room-type minimum-area rule.
        if shape.buffer(-opts.wall_max_m/2).is_empty:
            issues.append("wall_scale_enclosure")
        closures = [dict(b, points=[world(p) for p in b["points"]]) for b in bridges
                    if shape.boundary.buffer(tol*2).covers(LineString(b["points"]))]
        if closures:
            issues.append("inferred_opening_closure")
        # A seed in the polygonized face avoids selecting a column/inner loop.
        seed = world(face.representative_point().coords[0])
        candidate = dict(candidate_id=stable_id("enclosure", DISCOVERY_VERSION, opts.layout_id, shape.wkb_hex, json.dumps(origin), str(factor)),
                         status="needs_review", identity_status="unconfirmed_enclosed_space",
                         boundary_support="paired_faces" if "insufficient_outside_wall_face_support" not in issues else "partial_or_single_face",
                         candidate_role="room_candidate" if not {"wall_scale_enclosure","insufficient_outside_wall_face_support","boundary_reaches_search_or_frame_limit"}.intersection(issues) else "enclosure_unconfirmed",
                         method=method, seed=seed, boundary=[world(p) for p in boundary],
                         bbox=[*world(shape.bounds[:2]),*world(shape.bounds[2:])],
                         candidate_shell_area_m2=round(shape.area,6), area_m2=None,
                         outside_face_fraction=round(paired_fraction,6), edge_evidence=proofs,
                         entity_ids=sorted(ids), inferred_closures=closures,
                         internal_rings=[[world(p) for p in ring.coords] for ring in face.interiors],
                         diagnostics=issues)
        if shape.wkb in models:
            model = models[shape.wkb]
            candidate["geometry_model"] = model
            candidate["internal_rings"] = [contour_by_id[cid]["boundary"] for cid in model["topological_hole_ids"]]
            candidate["diagnostics"].extend(model["diagnostics"])
            # Deduplication must not keep the paired graph's seed inside a hole
            # that appears only in the full observed graph.
            local_holes = [[local(p) for p in ring] for ring in candidate["internal_rings"]]
            free_face = Polygon(shape.exterior, local_holes)
            if free_face.is_valid and not free_face.is_empty:
                candidate["seed"] = world(free_face.representative_point().coords[0])
        result["candidates"].append(candidate)
    result["candidates"].sort(key=lambda c:(c["candidate_role"]!="room_candidate",
                                          "wall_scale_enclosure" in c["diagnostics"],
                                          "boundary_reaches_search_or_frame_limit" in c["diagnostics"],
                                          -c["candidate_shell_area_m2"],-c["outside_face_fraction"],c["bbox"],c["candidate_id"]))
    result["complete"] = not result["diagnostics"]
    if not result["candidates"]:
        result["diagnostics"].append("no_enclosed_space_found")
    return result
