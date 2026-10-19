"""Local opening evidence: symbols never substitute for a supported wall gap.

All tests use transformed WCS geometry and physical units. Label conventions
are clues, not a door schedule. No nearest-room or same-side union fallback.
"""
import math
import re
from collections import defaultdict

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id
from cad_agent.geometry.rooms import coverage
from .fenestration import double_swing_pairs, match_double_swing, sliding_panel_evidence

OPENING_VERSION = "opening-evidence-v3.1-composite-leaves"


def effective_layer(rec, source):
    entity, path, _, _ = source["entities"][rec["entity_id"]]
    name = entity.dxf.get("layer", "0")
    if name == "0":
        for token in reversed(path[:-1]):
            parent = source["doc"].entitydb.get(token)
            if parent is not None and parent.dxf.get("layer", "0") != "0":
                return parent.dxf.layer
    return name


def segments(records):
    return [(LineString([a, b]), rec["entity_id"]) for rec in records if rec.get("linear")
            for path in rec["paths"] for a, b in zip(path, path[1:]) if math.dist(a, b) > 1e-8]


def arc_sources(records, source):
    """Copy native ARC attributes in the parent; no DXF objects cross IPC."""
    arcs={}
    for rec in records:
        if rec['kind']!='ARC':continue
        e=source['entities'][rec['entity_id']][0]
        arcs[rec['entity_id']]=dict(extrusion_z=e.dxf.extrusion.z,radius=e.dxf.radius,
            sweep=(e.dxf.end_angle-e.dxf.start_angle)%360,
            center=list(e.ocs().to_wcs(e.dxf.center))[:2],
            ends=[list(e.start_point)[:2],list(e.end_point)[:2]],layer=effective_layer(rec,source))
    return arcs


def swing_symbols(records, source, factor):
    """Compatibility entry point; pure geometry can use detached ARC sources."""
    return swing_symbols_from_sources(records,arc_sources(records,source),factor)


def swing_symbols_from_sources(records, arcs, factor):
    """Native 40–100 degree arcs with radial leaves, in WCS.

    Single-door identity still requires 80–100 degrees; smaller arcs can only
    establish a door through a complete complementary double-leaf pair.
    """
    lines = segments([r for r in records if r["kind"] != "DIMENSION"])
    tree = STRtree([s[0] for s in lines])
    found = []
    for rec in records:
        if rec["kind"] != "ARC":
            continue
        e = arcs[rec["entity_id"]]
        if abs(abs(e["extrusion_z"])-1) > 1e-6:
            continue
        radius = e["radius"]
        sweep = e["sweep"]
        if not .3 <= radius*factor <= 2.5 or not 40 <= sweep <= 100:
            continue
        center = e["center"]
        ends = e["ends"]
        for closed, opened in (ends, ends[::-1]):
            radial = LineString([center, opened])
            slack = .035/factor
            leaf_ids = []
            for raw in tree.query(radial.buffer(slack), predicate="intersects"):
                line, eid = lines[int(raw)]
                if (radial.buffer(slack).covers(line) and line.length >= radius*.75
                        and line.distance(Point(center)) <= slack
                        and line.distance(Point(opened)) <= slack):
                    leaf_ids.append(eid)
            if leaf_ids:
                found.append(dict(hinge=center, closed_tip=closed, open_tip=opened, radius_m=radius*factor,
                    entity_ids=sorted({rec["entity_id"], *leaf_ids}), arc_id=rec["entity_id"],
                    sweep_degrees=sweep, method="native_arc_and_radial_leaf", layer=e["layer"]))
    return found


def supported_symbol_portals(walls, symbols, factor, options):
    """Close stepped jamb gaps only when a swing and BOTH observed jamb paths agree.

    Tiny wall returns are kept. Unequal face widths are retained, not averaged
    away; local jamb connectivity prevents bridging an absent or broken jamb.
    """
    segs = segments(walls)
    tree = STRtree([s[0] for s in segs])
    tol = options.tolerance_m/factor
    maxwall = options.wall_max_m/factor
    portals = {}
    for symbol in symbols:
        if not 80<=symbol.get("sweep_degrees",90)<=100:continue
        h, tip = symbol["hinge"], symbol["closed_tip"]
        width = math.dist(h, tip)
        if width*factor > options.max_opening_m:
            continue
        u = [(tip[k]-h[k])/width for k in (0, 1)]; n = [-u[1], u[0]]
        def local(p):
            d = [p[k]-h[k] for k in (0, 1)]
            return [sum(d[k]*u[k] for k in (0, 1)), sum(d[k]*n[k] for k in (0, 1))]
        def world(x, y):return [h[k]+x*u[k]+y*n[k] for k in (0, 1)]
        nearby = [int(i) for i in tree.query(LineString([h,tip]).buffer(maxwall+.3/factor), predicate="intersects")]
        groups = defaultdict(list)
        slack = min(.25/factor, width*.25)
        for i in nearby:
            line, eid = segs[i]; a,b = [local(p) for p in line.coords]
            if abs(a[1]-b[1]) <= tol*2:
                off = (a[1]+b[1])/2
                if abs(off) <= maxwall:
                    groups[round(off/tol)].append((min(a[0],b[0]),max(a[0],b[0]),eid))
        gaps = []
        for key, spans in groups.items():
            left = [s for s in spans if -slack <= s[1] <= slack and s[0] < 0]
            right = [s for s in spans if width-slack <= s[0] <= width+slack and s[1] > width]
            if not left or not right:continue
            a=max(left,key=lambda s:s[1]);b=min(right,key=lambda s:s[0])
            if b[0]-a[1] < width*.75:continue
            if any(lo < b[0]-tol*2 and hi > a[1]+tol*2 for lo,hi,_ in spans):continue
            gaps.append(dict(points=[world(a[1],key*tol),world(b[0],key*tol)],offset=key*tol,
                             entity_ids=[a[2],b[2]]))
        for i,a in enumerate(gaps):
            for b in gaps[i+1:]:
                distance=abs(a["offset"]-b["offset"])*factor
                if not options.wall_min_m <= distance <= options.wall_max_m:continue
                jamb_ids=[]
                for p,q in zip(a["points"],b["points"]):
                    # Follow an observed stepped return within the local jamb zone.
                    zone=LineString([p,q]).buffer(slack+tol*2)
                    pieces=[(segs[j][0].intersection(zone),segs[j][1]) for j in nearby]
                    pieces=[(g,eid) for g,eid in pieces if not g.is_empty]
                    joined=unary_union([g.buffer(tol*2) for g,_ in pieces])
                    parts=list(joined.geoms) if hasattr(joined,"geoms") else [joined]
                    connected=next((g for g in parts if g.covers(Point(p)) and g.covers(Point(q))),None)
                    if connected is None:break
                    jamb_ids.extend(eid for g,eid in pieces if connected.intersects(g))
                else:
                    faces=[x["points"] for x in (a,b)]
                    key=repr(sorted(tuple(sorted(tuple(round(v/tol) for v in p) for p in f)) for f in faces))
                    ids=sorted(set(a["entity_ids"]+b["entity_ids"]+jamb_ids+symbol["entity_ids"]))
                    portals.setdefault(key,dict(portal_id=stable_id("portal",key),faces=faces,
                        entity_ids=ids,symbol=symbol,method="swing_supported_stepped_jamb_pair"))
    return list(portals.values())


def stepped_jamb_portals(walls, factor, options):
    """Apertures bounded by two observed jambs and four outward wall ends.

    Unlike grouping collinear faces, this keeps modest changes in wall thickness
    across an aperture. Closing the aperture establishes no door/window identity.
    """
    segs=segments(walls); tree=STRtree([s[0] for s in segs]);tol=options.tolerance_m/factor
    if len(segs)>5000:return [],["stepped_jamb_segment_limit"]
    jambs=[i for i,(g,_) in enumerate(segs) if options.wall_min_m<=g.length*factor<=options.wall_max_m]
    found={}
    def continuation(point, away, omitted):
        ids=[]
        for raw in tree.query(Point(point).buffer(tol*2),predicate="intersects"):
            i=int(raw)
            if i in omitted:continue
            line,eid=segs[i];a,b=list(line.coords)
            # An interior T crossing is not a supporting outward wall endpoint.
            if math.dist(point,b)<math.dist(point,a):a,b=b,a
            length=math.dist(a,b)
            if math.dist(a,point)<=tol*2 and length*factor>=.25 and sum((b[k]-a[k])/length*away[k] for k in (0,1))>=.98:
                ids.append(eid)
        return ids
    for pos,i in enumerate(jambs):
        line,eid=segs[i]; a,b=list(line.coords);normal=[(b[k]-a[k])/line.length for k in (0,1)]
        for j in jambs[pos+1:]:
            other,otherid=segs[j];c,d=list(other.coords)
            if sum((d[k]-c[k])*normal[k] for k in (0,1))<0:c,d=d,c
            if abs((d[0]-c[0])*normal[1]-(d[1]-c[1])*normal[0])*factor>options.tolerance_m*2:continue
            delta=[(c[k]+d[k]-a[k]-b[k])/2 for k in (0,1)]
            depth=sum(delta[k]*normal[k] for k in (0,1));tangent=[delta[k]-depth*normal[k] for k in (0,1)]
            width=math.hypot(*tangent)
            if not .3<=width*factor<=options.max_opening_m or abs(depth)*factor>.1:continue
            u=[v/width for v in tangent]
            proof=[]
            for p,away in [(a,[-v for v in u]),(b,[-v for v in u]),(c,u),(d,u)]:
                support=continuation(p,away,{i,j})
                if not support:break
                proof.extend(support)
            else:
                faces=[[a,c],[b,d]]
                # Already drawn continuous wall faces are not apertures.
                blocked=False
                for face in faces:
                    chord=LineString(face);near=[segs[int(k)][0] for k in tree.query(chord.buffer(tol*2),predicate="intersects") if int(k) not in (i,j)]
                    if near and unary_union(near).intersection(chord.buffer(tol*2)).length>chord.length*.12:
                        blocked=True;break
                if blocked:continue
                key=repr(sorted(tuple(sorted(tuple(round(v/tol) for v in p) for p in f)) for f in faces))
                found[key]=dict(portal_id=stable_id("portal",key),faces=faces,
                    entity_ids=sorted({eid,otherid,*proof}),method="observed_jambs_and_four_outward_wall_ends")
                if len(found)>500:return [],["stepped_jamb_candidate_limit"]
    return list(found.values()),[]


def side_associations(faces, rooms, factor, tolerance):
    """Require overlap with a face AND interior probes beyond that wall side."""
    mid=[[(p[k]+q[k])/2 for k in (0,1)] for p,q in faces]
    tangent=[faces[0][1][k]-faces[0][0][k] for k in (0,1)]
    length=math.hypot(*tangent)
    if length == 0:return []
    normal=[-tangent[1]/length,tangent[0]/length]
    if sum(normal[k]*(mid[0][k]-mid[1][k]) for k in (0,1))<0:normal=[-v for v in normal]
    results=[]
    for i,points in enumerate(faces):
        sign=1 if i==0 else -1
        probe_distance=max(.03,tolerance*4)/factor
        probes=[[points[0][k]*(1-t)+points[1][k]*t+sign*normal[k]*probe_distance for k in (0,1)] for t in (.25,.5,.75)]
        line=LineString(points); matches=[]
        for room in rooms:
            poly=Polygon(room["boundary"],room.get("internal_rings",[]))
            if not poly.is_valid:continue
            overlap=poly.boundary.intersection(line.buffer(tolerance*2/factor)).length
            hits=sum(poly.contains(Point(p)) for p in probes)
            if overlap>=line.length*.8 and hits>=2:
                matches.append(room["object_id"])
        results.append(dict(side=i,face_points=points,probe_points=probes,room_ids=sorted(matches),
                            status="unique" if len(matches)==1 else "ambiguous" if matches else "unresolved"))
    return results


DOOR_TAG=re.compile(r"^(?:FMZ|FDM|TLM|FM|LM|PM|M)\d{2}[A-Z]?\d{2}(?:[A-Z0-9'’\-]*)$",re.I)
WINDOW_TAG=re.compile(r"^(?:TLC|TC|LC|GC|C)\d{2}[A-Z]?\d{2}(?:[A-Z0-9'’\-]*)$",re.I)


def classify_openings(graph, records, symbols, texts, factor):
    """Evidence fusion, preserving conflicts. No label-only or proximity-only door."""
    # Different jamb hypotheses may describe the same physical opening. Keep
    # both sources for review, but never publish two independent passages.
    polygons=[Polygon(o["boundary"]) for o in graph["openings"]]
    opening_tree=STRtree(polygons)
    for i,poly in enumerate(polygons):
        peers=[]
        for raw in opening_tree.query(poly,predicate="intersects"):
            j=int(raw)
            if i==j:continue
            area=min(poly.area,polygons[j].area)
            if area and poly.intersection(polygons[j]).area/area>=.8:
                peers.append(graph["openings"][j]["object_id"])
        if peers:
            o=graph["openings"][i];o["competing_opening_ids"]=sorted(peers)
            o["connection_status"]="blocked_overlapping_candidates"
            if "overlapping_opening_hypotheses" not in o["diagnostics"]:o["diagnostics"].append("overlapping_opening_hypotheses")
    assignments=defaultdict(list)
    for text in texts:
        tag=text["text"].strip().replace(' ','')
        kind="door" if DOOR_TAG.fullmatch(tag) else "window" if WINDOW_TAG.fullmatch(tag) else None
        if kind is None:continue
        nominal_width=int(re.search(r"\d{2}",tag).group())/10
        p=box(*text['bbox']) if text.get('bbox') else Point(text["origin"])
        choices=[]
        for o in graph["openings"]:
            f=o["faces"][0]; width=math.dist(*f); u=[(f[1][k]-f[0][k])/width for k in (0,1)]
            projected=[sum((q[k]-f[0][k])*u[k] for k in (0,1)) for q in (list(p.exterior.coords) if text.get("bbox") else [text["origin"]])]
            distance=Polygon(o["boundary"]).distance(p)*factor
            if (max(projected)>=-.12/factor and min(projected)<=width+.12/factor and distance<=.65
                    and abs(width*factor-nominal_width)<=max(.15,nominal_width*.25)):
                choices.append((distance,o["object_id"]))
        choices.sort()
        if choices and (len(choices)==1 or choices[1][0]-choices[0][0]>.1):
            assignments[choices[0][1]].append(dict(kind=kind,text=text["text"],entity_id=text["entity_id"],origin=text["origin"],
                bbox=text.get('bbox'),association='unique_native_text_extent_with_conventional_width_consistency',nominal_width_hint_m=nominal_width))
    lines=segments([r for r in records if r["kind"]!="DIMENSION"])
    tree=STRtree([x[0] for x in lines])
    double_pairs=double_swing_pairs(symbols,factor)
    frame_records=[r for r in records if not re.search(r'DIM|TEXT|标注|文字|洁具|FURN|LATRINE|LVTRY',r.get('effective_layer',r['layer']),re.I)]
    frame_lines=segments([r for r in frame_records if r['kind']!='DIMENSION'])
    for o in graph["openings"]:
        poly=Polygon(o["boundary"]); faces=o["faces"]
        width=max(math.dist(*f) for f in faces); slack=.08/factor
        swings=[]
        for s in symbols:
            chord=LineString([s["hinge"],s["closed_tip"]])
            # A short nearby cabinet arc or half-width decorative swing cannot win.
            if 80<=s.get("sweep_degrees",90)<=100 and .75*width<=chord.length<=1.2*width and poly.buffer(slack).covers(chord):
                swings.append(s)
        rails=defaultdict(list);direction=[(faces[0][1][k]-faces[0][0][k])/math.dist(*faces[0]) for k in (0,1)]
        for raw in tree.query(poly,predicate="intersects"):
            line,eid=lines[int(raw)];a,b=list(line.coords)
            off=[(p[0]-faces[0][0][0])*(-direction[1])+(p[1]-faces[0][0][1])*direction[0] for p in (a,b)]
            if abs(off[0]-off[1])*factor<=.005 and poly.buffer(.005/factor).covers(line):
                span=sorted(sum((p[k]-faces[0][0][k])*direction[k] for k in (0,1)) for p in (a,b))
                rails[round((sum(off)/2)*factor/.005)].append((*span,eid))
        supported={off:spans for off,spans in rails.items() if coverage([(a,b) for a,b,_ in spans],width)>=.55}
        rail_ids=sorted({eid for off,spans in supported.items() if any(abs(off-other)*.005>=.015 for other in supported) for _,_,eid in spans})
        doubles=match_double_swing(o,double_pairs,frame_lines,factor)
        sliding=sliding_panel_evidence(o,frame_records,factor)
        labels=assignments[o["object_id"]];door_label=any(t["kind"]=="door" for t in labels);window_label=any(t["kind"]=="window" for t in labels)
        door_doubles=[d for d in doubles if door_label or all(80<=s.get("sweep_degrees",90)<=100 for s in d["leaves"])]
        door=bool(swings or door_doubles) or (door_label and bool(sliding))
        window=window_label and bool(rail_ids or sliding)
        conflict=(door and window_label) or (window and door_label)
        typ="conflict" if conflict else "door_candidate" if door else "window_candidate" if window else "unknown"
        operation=('conflict' if sliding and (swings or doubles) else 'ambiguous_swing' if swings and doubles
                   else 'double_swing' if doubles else 'single_swing' if swings else 'sliding' if sliding else 'unknown')
        reasons=[]
        if typ=='unknown':
            if not door_label and not window_label:reasons.append('no_unique_door_or_window_identity_evidence')
            if not (swings or doubles or sliding):reasons.append('opening_operation_not_established')
            if not rail_ids and not sliding:reasons.append('no_supported_panel_or_frame_pattern')
        o.update(opening_type=typ,classification_status="conflict" if conflict else "supported_candidate" if door or window else "insufficient_evidence",
            operation_type=operation,unresolved_reasons=reasons,
            classification_evidence=dict(swing_symbols=swings,double_swing_pairs=doubles,sliding_panel_pairs=sliding,parallel_rail_entity_ids=rail_ids,labels=labels,
                method="symbol_geometry_and_locally_assigned_labels",label_conventions="M/FM/FDM/FMZ/TLM/LM/PM vs C/TC/LC/GC/TLC; not a verified schedule"),
            door_type=('swing_candidate' if operation=='single_swing' else operation+'_candidate') if typ=='door_candidate' and operation not in ('unknown','conflict') else None,
            window_type='sliding_candidate' if typ=='window_candidate' and operation=='sliding' else None)
        o["entity_ids"]=sorted(set(o["entity_ids"]+rail_ids+[t["entity_id"] for t in labels]+[eid for s in swings+doubles+sliding for eid in s["entity_ids"]]
                                  +[eid for s in doubles for eid in s['frame_binding_entity_ids']]))
        if typ=="door_candidate":
            o["diagnostics"]=[d for d in o["diagnostics"] if d!="opening_not_confirmed_as_door"]
            o["diagnostics"].append("door_identity_inferred_from_symbol_evidence")
        elif typ in ("window_candidate","conflict"):
            o["connection_status"]="blocked_window" if typ=="window_candidate" else "blocked_type_conflict"
        if operation=='conflict':o['connection_status']='blocked_operation_conflict'
        o["passage_status"]="supported_door_between_two_spaces" if typ=="door_candidate" and o["connection_status"]=="two_space_candidates" else "not_established"
    # Windows retain geometric adjacency, but never a passage edge.
    by_id={o["object_id"]:o for o in graph["openings"]}
    for r in graph["relations"]:
        if r["source_id"] in by_id and r["kind"]=="opens_to_candidate_space":
            o=by_id[r["source_id"]]
            r["kind"]="adjacent_to_candidate_space"
            r["opening_type"]=o["opening_type"]
    for o in graph["openings"]:
        if o["passage_status"]=="supported_door_between_two_spaces":
            a,b=o["room_ids"]
            graph["relations"].append(dict(object_id=stable_id("relation",o["object_id"],a,b,"door_connects"),
                source_id=a,target_id=b,kind="door_connects_candidate_spaces",opening_id=o["object_id"],
                status="candidate",entity_ids=o["entity_ids"]))
