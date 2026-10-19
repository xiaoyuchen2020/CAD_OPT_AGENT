"""Pure component topology and opening classification, fixed worker operations.

Inputs are detached scoped geometry, native ARC descriptors, and text anchors.
No workspace, source document, persistence, credentials or executor is available.
"""
from copy import deepcopy
from shapely.geometry import LineString, Polygon, Point
from shapely.strtree import STRtree
from cad_agent.geometry.discovery import discover_enclosures
from .contracts import RelationQuery
from .fenestration import double_swing_pairs
from .openings import (swing_symbols_from_sources, supported_symbol_portals,
                       stepped_jamb_portals, classify_openings)


def compute_refinement(payload):
    records,selected=payload['records'],payload['selected']
    factor,bounds=payload['factor'],payload['bounds']
    options=RelationQuery.model_validate(payload['options'])
    symbols=swing_symbols_from_sources(records,payload["arc_sources"],factor)
    composite=[dict(hinge=p["hinges"][0],closed_tip=p["hinges"][1],entity_ids=p["entity_ids"],
                    sweep_degrees=90,method="composite_leaf_chord_for_jamb_search") for p in double_swing_pairs(symbols,factor)]
    portals=supported_symbol_portals(selected,symbols+composite,factor,options) if options.close_openings else []
    stepped,issues=stepped_jamb_portals(selected,factor,options) if options.close_openings else ([],[])
    portals.extend(stepped)
    bridges=[dict(points=face,entity_ids=p["entity_ids"],method=p["method"],portal_id=p["portal_id"])
             for p in portals for face in p["faces"]]
    refined=discover_enclosures(selected,bounds,factor,options,extra_bridges=bridges)
    refined["supported_portals"]=portals
    refined["diagnostics"].extend(issues)
    # A wall-thickness change creates a short return with no parallel mate;
    # a supported aperture may also have slightly nonparallel inner/outer faces.
    # Admit these only in a closed observed face, with explicit per-edge proof.
    for candidate in refined["candidates"]:
        if candidate["candidate_role"]=="room_candidate":continue
        if {"wall_scale_enclosure","boundary_reaches_search_or_frame_limit"}.intersection(candidate["diagnostics"]):continue
        edges=candidate["edge_evidence"];proofs=[];unresolved=False
        for i,edge in enumerate(edges):
            if edge["outside_face_fraction"]>=.9-1e-9:continue
            line=LineString(edge["points"])
            portal=next((p for p in portals if any(LineString(f).buffer(options.tolerance_m*2/factor).covers(line) for f in p["faces"])),None)
            if portal:
                proofs.append(dict(points=edge["points"],basis="observed_paired_jamb_aperture",portal_id=portal["portal_id"],entity_ids=portal["entity_ids"]))
            elif (edge["length_m"]<=.1 and edge["observed_or_bridged_fraction"]>=.98
                  and edges[i-1]["outside_face_fraction"]>=.9 and edges[(i+1)%len(edges)]["outside_face_fraction"]>=.9):
                proofs.append(dict(points=edge["points"],basis="observed_short_wall_return",entity_ids=edge["entity_ids"]))
            else:unresolved=True;break
        if proofs and not unresolved:
            candidate["candidate_role"]="room_candidate"
            candidate["boundary_support"]="paired_faces_with_supported_apertures_and_short_returns"
            candidate["supplemental_boundary_evidence"]=proofs
            candidate["diagnostics"].remove("insufficient_outside_wall_face_support")
            candidate["diagnostics"].append("supported_apertures_and_returns_not_net_area_confirmation")
    anchors=payload["anchors"]
    tree=STRtree([Point(p) for _,p in anchors])
    for candidate in refined["candidates"]:
        poly=Polygon(candidate["boundary"])
        hits={anchors[int(i)][0]["region_id"]:anchors[int(i)][0] for i in tree.query(poly,predicate="contains")}
        candidate["text_regions"]=[dict(region_id=r["region_id"],text=r["text"][:160]) for r in hits.values()][:12]
        candidate["diagnostics"].append("wall_layer_hints_not_confirmed_identity")
    return refined


def compute_relations(payload):
    from .relations import build_relations
    return build_relations(payload['discovery'],RelationQuery.model_validate(payload['options']))


def compute_openings(payload):
    graph=deepcopy(payload['graph'])
    classify_openings(graph,payload['records'],
        swing_symbols_from_sources(payload['records'],payload['arc_sources'],payload['factor']),
        payload['texts'],payload['factor'])
    return graph
