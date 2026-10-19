"""A candidate graph derived from existing wall-face and jamb evidence."""
import math
import re

from shapely.geometry import LineString, Polygon
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id
from cad_agent.geometry.source import UNIT_TO_M, scoped_geometry_index
from .openings import OPENING_VERSION, side_associations


def refine_discovery(workspace, sid, original, reading, options, source_bundle=None):
    """Optional layer clues exclude furnishings, without confirming wall identity.

    The original all-linework discovery remains available; the filtered pass
    is separately versioned and source-bound. Missing wall layers fall back.
    """
    if options.linework_mode=="all" or not UNIT_TO_M.get(original["applied_unit"]) or "non_plan_view" in original["diagnostics"] or "discovery_scope_too_large" in original["diagnostics"]:
        return original
    bounds=original["scope_bbox"];lid=options.layout_id
    if source_bundle is None:
        index=scoped_geometry_index(workspace,sid,lid,bounds,original["applied_unit"])
        records=index.query(lid,bounds)
        from cad_agent.geometry.source import source_descriptors
        descriptors=source_descriptors(workspace,sid,records)
    else:
        descriptors=source_bundle.get(lid,bounds,original["applied_unit"])
        records=descriptors['records']
    names=descriptors['effective_layers']
    wall=re.compile(r"墙|(?:^|[^A-Z])WALL(?:[^A-Z]|$)",re.I)
    column=re.compile(r"柱|(?:^|[^A-Z])COL(?:U(?:MN)?|S)?(?:[^A-Z]|$)",re.I)
    excluded=re.compile(r"标注|文字|填充|DIM|TEXT|HATCH",re.I)
    selected=[r for r in records if (wall.search(names[r["entity_id"]]) or column.search(names[r["entity_id"]])) and not excluded.search(names[r["entity_id"]])]
    if not any(wall.search(names[r["entity_id"]]) for r in selected):
        return original
    factor=UNIT_TO_M[original["applied_unit"]]
    from cad_agent.geometry.compute import compute
    texts={t['entity_id']:t for t in reading.scene['texts']}
    anchors=[(dict(region_id=r.region_id,text=r.text),texts[eid]['origin'])
             for r in reading.scope(lid,options.frame_id) for eid in r.entity_ids if eid in texts]
    refined=compute(workspace,'component_refine',dict(records=records,selected=selected,
        arc_sources=descriptors["arc_sources"],factor=factor,bounds=bounds,
        options=options.model_dump(),anchors=anchors))
    key=stable_id("measure",sid,original["measurement_id"],OPENING_VERSION,*sorted(names[r["entity_id"]]+r["entity_id"] for r in selected))
    result=original | refined | dict(measurement_id=key,algorithm_version=OPENING_VERSION,
        baseline_discovery_id=original["measurement_id"],linework_basis="wall_and_column_layer_hints",
        layer_selection=dict(included_entity_count=len(selected),excluded_entity_count=len(records)-len(selected),
            included_layers=sorted({names[r["entity_id"]] for r in selected}),
            note="图层名称仅作几何选择线索；缺少墙图层时回退全部线形。过滤可能漏掉图层命名不同的墙。"),
        evidence_url=f"/api/drawings/{sid}/measurements/{key}",
        preview_url=f"/api/drawings/{sid}/measurements/{key}/preview.svg")
    result["diagnostics"]=sorted(set(original["diagnostics"]+refined["diagnostics"]+["wall_layer_hints_not_confirmed_identity"]))
    result["complete"]=False
    workspace.store.put("measurement",key,result)
    return result


def enrich_openings(workspace, sid, graph, discovery, reading, options, source_bundle=None):
    factor=UNIT_TO_M.get(discovery["applied_unit"])
    if not factor or not graph["openings"]:return
    if source_bundle is None:
        records=scoped_geometry_index(workspace,sid,options.layout_id,discovery["scope_bbox"],discovery["applied_unit"]).query(options.layout_id,discovery["scope_bbox"])
        from cad_agent.geometry.source import source_descriptors
        descriptors=source_descriptors(workspace,sid,records)
    else:
        descriptors=source_bundle.get(options.layout_id,discovery["scope_bbox"],discovery["applied_unit"])
        records=descriptors['records']
    for rec in records:rec['effective_layer']=descriptors['effective_layers'][rec['entity_id']]
    texts=[t for t in reading.scene["texts"] if t["layout_id"]==options.layout_id]
    from cad_agent.geometry.compute import compute
    result=compute(workspace,'component_openings',dict(graph=graph,records=records,
        arc_sources=descriptors["arc_sources"],texts=texts,factor=factor))
    graph.clear();graph.update(result)


def build_relations(discovery, options):
    factor = UNIT_TO_M.get(discovery["applied_unit"])
    result = dict(rooms=[], walls=[], openings=[], relations=[],
                  diagnostics=list(discovery["diagnostics"]),
                  discovery_id=discovery["measurement_id"],
                  coverage="bounded_candidate_graph; not_confirmed_rooms_or_doors")
    if factor is None:
        return result
    tol=options.tolerance_m/factor
    candidates=[c for c in discovery["candidates"] if c["candidate_role"]=="room_candidate"]
    if len(candidates)>300:
        result["diagnostics"].append("relation_room_limit")
    candidates=candidates[:300]
    walls={}; bridges={}
    def relation(a,b,kind,ids):
        return dict(object_id=stable_id("relation",discovery["measurement_id"],a,b,kind),
                    source_id=a,target_id=b,kind=kind,status="candidate",entity_ids=ids)
    for c in candidates:
        rid=c["candidate_id"]
        result["rooms"].append(dict(object_id=rid,kind="room_candidate",bbox=c["bbox"],boundary=c["boundary"],
            internal_rings=c.get("internal_rings",[]),
            supplemental_boundary_evidence=c.get("supplemental_boundary_evidence",[]),
            seed=c["seed"],text_regions=c["text_regions"],entity_ids=c["entity_ids"],
            area_m2=None,candidate_shell_area_m2=c["candidate_shell_area_m2"],status="candidate",
            diagnostics=c["diagnostics"],measurement_request=c.get("measurement_request")))
        for edge in c["edge_evidence"]:
            if edge["outside_face_fraction"]<.9 or len(edge["entity_ids"])<2:
                continue
            key=repr(sorted(tuple(round(v/tol) for v in p) for p in edge["points"]))
            wid=stable_id("wall",discovery["measurement_id"],key)
            walls.setdefault(wid,dict(object_id=wid,kind="wall_face_candidate",points=edge["points"],
                entity_ids=edge["entity_ids"],outside_face_fraction=edge["outside_face_fraction"],
                status="candidate",note="有平行外侧线支持的房间边；尚未确认材质、墙厚和完整墙体身份。"))
            result["relations"].append(relation(rid,wid,"bounded_by",edge["entity_ids"]))
        for closure in c["inferred_closures"]:
            key=repr(sorted(tuple(round(v/tol) for v in p) for p in closure["points"]))
            record=bridges.setdefault(key,dict(points=closure["points"],entity_ids=closure["entity_ids"],room_ids=set()))
            record["room_ids"].add(rid)
    # Keep opposite faces even when the adjacent space is unclassified/outside.
    for closure in discovery.get("opening_sides", []):
        key=repr(sorted(tuple(round(v/tol) for v in p) for p in closure["points"]))
        bridges.setdefault(key,dict(points=closure["points"],entity_ids=closure["entity_ids"],room_ids=set()))
    result["walls"]=list(walls.values())
    bridge_list=list(bridges.values())
    lines=[LineString(b["points"]) for b in bridge_list]
    tree=STRtree(lines)
    pairs=[]
    for i,a in enumerate(bridge_list):
        for raw_j in tree.query(lines[i].buffer(options.wall_max_m/factor)):
            j=int(raw_j)
            if j<=i:continue
            b=bridge_list[j]
            p,q=a["points"];r,s=b["points"]
            length=math.dist(p,q)
            if length<=tol:continue
            ux,uy=(q[0]-p[0])/length,(q[1]-p[1])/length
            projected=sorted((v[0]-p[0])*ux+(v[1]-p[1])*uy for v in (r,s))
            offsets=[(v[0]-p[0])*(-uy)+(v[1]-p[1])*ux for v in (r,s)]
            width=abs(sum(offsets)/2)*factor
            if (abs(offsets[0]-offsets[1])<=tol*2 and max(abs(projected[0]),abs(projected[1]-length))<=tol*2
                and options.wall_min_m<=width<=options.wall_max_m):
                pairs.append((i,j,width))
    # Explicit symbol-supported stepped jambs can have unequal face widths.
    def face_key(points):return repr(sorted(tuple(round(v/tol) for v in p) for p in points))
    bridge_keys={face_key(b["points"]):i for i,b in enumerate(bridge_list)}
    for portal in discovery.get("supported_portals",[]):
        indices=[bridge_keys.get(face_key(face)) for face in portal["faces"]]
        if None in indices:continue
        i,j=sorted(indices)
        if i==j or any((i,j)==(a,b) for a,b,_ in pairs):continue
        pairs.append((i,j,lines[i].distance(lines[j])*factor))
    counts={i:sum(i in (a,b) for a,b,_ in pairs) for i in range(len(bridge_list))}
    wall_lines=[LineString(w["points"]) for w in result["walls"]]
    wall_tree=STRtree(wall_lines)
    for i,j,thickness in pairs:
        a,b=bridge_list[i],bridge_list[j]
        oid=stable_id("opening",discovery["measurement_id"],str(i),str(j))
        polygon=Polygon(a["points"]+list(reversed(b["points"])))
        if not polygon.is_valid:
            polygon=Polygon(a["points"]+b["points"])
        faces=[a["points"],b["points"]]
        sides=side_associations(faces,result["rooms"],factor,options.tolerance_m)
        room_ids=sorted({rid for side in sides for rid in side["room_ids"]})
        two_sides=len(sides)==2 and all(len(s["room_ids"])==1 for s in sides) and len(room_ids)==2
        ids=sorted(set(a["entity_ids"]+b["entity_ids"]))
        diagnostics=["opening_not_confirmed_as_door"]
        if counts[i]>1 or counts[j]>1:diagnostics.append("competing_opening_pairs")
        if not two_sides:diagnostics.append("two_sided_space_connection_unresolved")
        attached=[]
        for k in wall_tree.query(polygon.buffer(tol*2),predicate="intersects"):
            k=int(k)
            if any(wall_lines[k].buffer(tol*2).covers(lines[n]) for n in (i,j)):
                attached.append(result["walls"][k]["object_id"])
        result["openings"].append(dict(object_id=oid,kind="opening_candidate",status="candidate",
            boundary=list(polygon.exterior.coords),bbox=list(polygon.bounds),entity_ids=ids,
            faces=faces,sides=sides,
            width_m=round(min(lines[i].length,lines[j].length)*factor,6),
            face_widths_m=[round(lines[n].length*factor,6) for n in (i,j)],
            paired_face_spacing_m=round(thickness,6),
            room_ids=room_ids,wall_ids=attached,door_type=None,
            connection_status="two_space_candidates" if two_sides and counts[i]==counts[j]==1 else "unresolved",
            diagnostics=diagnostics,method="paired_gaps_with_observed_jambs"))
        for rid in room_ids:result["relations"].append(relation(oid,rid,"opens_to_candidate_space",ids))
        for wid in attached:result["relations"].append(relation(oid,wid,"opening_in_candidate_wall",ids))
    result["unpaired_closure_count"]=sum(counts[i]==0 for i in counts)
    if result["unpaired_closure_count"]:result["diagnostics"].append("unpaired_opening_sides")
    return result
