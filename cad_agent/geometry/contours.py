"""Reusable contour topology and explicitly selected geometric area bases.

Inner rings are geometric evidence, never automatically columns/voids/shafts.
All computations use local metres; exported vertices remain source WCS.
"""
import json

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id

CONTOUR_VERSION = "room-contours-v1.1"


def outline(ring):
    return Polygon(ring).simplify(0).normalize()


def build_contours(faces, records, context, origin, factor, opts):
    shapes, methods, source_ids, variants, approximation = {}, {}, {}, {}, {}

    def add(ring, method, eid=None):
        shape = outline(ring)
        if not shape.is_valid or shape.area <= opts.tolerance_m**2:
            return None
        key = shape.wkb
        shapes[key] = shape
        methods.setdefault(key, set()).add(method)
        source_ids.setdefault(key, set()).update([eid] if eid else [])
        return key

    for face, method in faces:
        key = add(face.exterior, method)
        holes = [k for ring in face.interiors if (k := add(ring, method)) is not None]
        if key is not None:
            variants.setdefault(key, []).append((method, holes))
    # Preserve original closed objects too: crossing objects may be split into
    # several faces by polygonize, and their unsplit outlines remain useful.
    for rec in records:
        for path in rec["paths"]:
            if len(path) < 4 or path[0] != path[-1]:
                continue
            pts = [tuple(round((p[i]-origin[i])*factor/opts.tolerance_m)*opts.tolerance_m
                         for i in (0, 1)) for p in path]
            if all(0 <= p[0] <= (opts.bbox[2]-origin[0])*factor and
                   0 <= p[1] <= (opts.bbox[3]-origin[1])*factor for p in pts):
                key = add(pts, "source_closed_path" if rec.get("linear") else "tessellated_closed_path", rec["entity_id"])
                if key is not None and not rec.get("linear"):
                    approximation[key] = max(approximation.get(key, 0), rec.get("approximation_m") or .001)
    if len(shapes) > 4000:
        return [], {}, ["contour_limit_exceeded"]
    keys = sorted(shapes)
    polygons = [shapes[k] for k in keys]
    tree = STRtree(polygons)
    ids = {k: stable_id("contour", CONTOUR_VERSION, opts.layout_id, json.dumps(origin), str(factor), k.hex()) for k in keys}
    library, models = [], {}
    def world(p):
        return [p[0]/factor+origin[0], p[1]/factor+origin[1]]
    for i, (key, shape) in enumerate(zip(keys, polygons)):
        hits = [int(j) for j in tree.query(shape, predicate="intersects") if int(j) != i]
        inside = [j for j in hits if shape.contains_properly(polygons[j])]
        containers = [j for j in hits if polygons[j].contains_properly(shape)]
        # Visit smallest containers first. A containing ancestor of an already
        # selected parent is not direct; this also avoids cubic nested-ring work.
        parents = []
        for j in sorted(containers, key=lambda j: polygons[j].area):
            if not any(polygons[j].contains_properly(polygons[k]) for k in parents):
                parents.append(j)
        conflicts = [j for j in hits if shape.boundary.intersects(polygons[j].boundary)]
        evidence, owned_ids = [], set(source_ids[key])
        for a, b in zip(shape.exterior.coords, list(shape.exterior.coords)[1:]):
            if key in approximation:
                evidence.append(dict(points=[world(a), world(b)], entity_ids=sorted(source_ids[key]),
                                     approximate=True, inferred=False, method="source_curve_tessellation"))
                continue
            coverage, owned = context.support(a, b, 0, opts.tolerance_m*2)
            eids = sorted({eid for j in owned for eid in context.segments[j]["entity_ids"]})
            owned_ids.update(eids)
            evidence.append(dict(points=[world(a), world(b)], observed_or_bridged_fraction=coverage,
                                 entity_ids=eids, inferred=any(context.segments[j]["synthetic"] for j in owned)))
        library.append(dict(contour_id=ids[key], boundary=[world(p) for p in shape.exterior.coords],
                            candidate_area_m2=round(shape.area, 6), perimeter_m=round(shape.length, 6),
                            approximate=key in approximation, curve_approximation_m=approximation.get(key, 0),
                            coordinate_quantization_m=opts.tolerance_m,
                            semantic_role="unclassified", methods=sorted(methods[key]),
                            parent_contour_ids=[ids[keys[j]] for j in sorted(parents)],
                            intersecting_boundary_ids=[ids[keys[j]] for j in sorted(conflicts)],
                            entity_ids=sorted(owned_ids), edge_evidence=evidence))
        versions = [dict(method=method, hole_contour_ids=sorted(ids[k] for k in holes))
                    for method, holes in variants.get(key, [])]
        # Prefer the complete observed graph for this particular face. A face
        # excludes its immediate inner rings; nested islands are separate faces.
        preferred = next((v for v in versions if v["method"] == "observed_closed_face"), versions[0] if versions else None)
        models[key] = dict(version=CONTOUR_VERSION, outer_contour_id=ids[key],
                           interior_contour_ids=[ids[keys[j]] for j in sorted(inside)],
                           boundary_intersection_contour_ids=[ids[keys[j]] for j in sorted(conflicts)],
                           topological_hole_ids=preferred["hole_contour_ids"] if preferred else [],
                           topology_method=preferred["method"] if preferred else None,
                           face_variants=versions,
                           diagnostics=["alternative_inner_topology"] if len({tuple(v["hole_contour_ids"]) for v in versions}) > 1 else [])
    return library, models, []


def area_from_contours(candidate, library, factor, basis, exclusions):
    model = candidate["geometry_model"]
    by_id = {c["contour_id"]: c for c in library}
    outer = by_id[model["outer_contour_id"]]
    origin = outer["boundary"][0]
    def polygon(cid):
        return Polygon([((p[0]-origin[0])*factor, (p[1]-origin[1])*factor) for p in by_id[cid]["boundary"]])
    shell = polygon(outer["contour_id"])
    selected = model["topological_hole_ids"] if basis == "topological_face" else exclusions if basis == "selected_exclusions" else []
    if any(cid not in model["interior_contour_ids"] for cid in selected):
        raise ValueError("扣除轮廓必须严格位于当前候选外轮廓内，不能跨房间、相交或接触外边界。")
    excluded = unary_union([polygon(cid) for cid in selected])
    net = shell.difference(excluded)
    def world(ring):
        return [[p[0]/factor+origin[0], p[1]/factor+origin[1]] for p in ring.coords]
    components = list(net.geoms) if net.geom_type == "MultiPolygon" else [net]
    approximate = any(by_id[cid].get("approximate") for cid in [outer["contour_id"]]+selected)
    return dict(shell_area_m2=round(shell.area, 6), excluded_union_area_m2=round(excluded.area, 6),
                basis_area_m2=round(net.area, 6), perimeter_m=round(net.length, 6),
                approximate=approximate,
                maximum_curve_approximation_m=max(by_id[cid].get("curve_approximation_m", 0) for cid in [outer["contour_id"]]+selected),
                selected_contour_ids=selected,
                result_polygons=[dict(boundary=world(p.exterior), holes=[world(r) for r in p.interiors]) for p in components])


def compute_contour_area(payload):
    """Pure worker entry point; build_contours stays inside discovery workers."""
    return area_from_contours(payload['candidate'],payload['library'],payload['factor'],
                              payload['basis'],payload['exclusions'])
