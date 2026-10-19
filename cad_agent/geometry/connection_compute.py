"""Pure per-pair jamb and opening evidence for the fixed geometry worker."""
import json
import math
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points
from cad_agent.contracts import stable_id
from cad_agent.components.openings import (segments, swing_symbols_from_sources,
    supported_symbol_portals, stepped_jamb_portals, classify_openings)
from cad_agent.components.contracts import RelationQuery
from cad_agent.geometry.room_boundary import WALL


def jamb_measurement(faces, records, factor, tolerance=.002):
    """Bind each end to an observed source segment, without rounded portal points.

    A jamb must overlap the wall depth; parallel wall faces/door arcs cannot be
    substituted. Whole-entity minimum distance can hit unrelated polyline parts.
    """
    segs=segments(records);found=[]
    # Orient both cross-opening faces consistently.
    a,b=faces[0];c,d=faces[1]
    if math.dist(a,c)+math.dist(b,d)>math.dist(a,d)+math.dist(b,c):c,d=d,c
    for p,q in ((a,c),(b,d)):
        target=LineString([p,q]);choices=[]
        if target.length*factor<.02:return None
        for line,eid in segs:
            # Full observed jamb segment must agree with the expected depth.
            if target.buffer(tolerance/factor).covers(line) and line.length>=target.length*.7:
                choices.append((line.hausdorff_distance(target),eid,line))
        if not choices:return None
        _,eid,line=min(choices,key=lambda t:(t[0],t[1]));found.append((eid,line))
    if found[0][0]==found[1][0] and found[0][1].equals(found[1][1]):return None
    line=[list(p.coords)[0] for p in nearest_points(found[0][1],found[1][1])]
    return dict(value=round(math.dist(*line)*factor,6),value_drawing_units=round(math.dist(*line),6),unit='m',basis='observed_jamb_segment_minimum',
        measurement_line=line,jambs=[dict(entity_id=eid,points=list(map(list,g.coords))) for eid,g in found],
        note='图上两门垛边线最短距离；未证明完成面、门框及开启门扇后的通行净宽。')


def local_evidence_from_sources(records, arcs, factor, anchors, bounds, texts=()):
    walls=[r for r in records if r.get('linear') and WALL.search(r['effective_layer'])]
    probe=LineString(anchors);wall_segments=segments(walls)
    barriers=[]
    crossings=[dict(entity_id=eid,points=list(map(list,line.coords)),layer=next(r['effective_layer'] for r in records if r['entity_id']==eid)) for line,eid in segments(records) if line.length*factor>.1 and line.crosses(probe)]
    for line,eid in wall_segments:
        if line.length*factor>.1 and line.intersects(probe):
            clipped=line.intersection(box(*bounds))
            if clipped.geom_type=='LineString':barriers.append(dict(entity_id=eid,points=list(map(list,clipped.coords)),full_points=list(map(list,line.coords))))
    opts=RelationQuery(layout_id='local',bbox=bounds,unit_override='m')
    symbols=swing_symbols_from_sources(records,arcs,factor)
    portals=supported_symbol_portals(walls,symbols,factor,opts)
    more,issues=stepped_jamb_portals(walls,factor,opts);portals+=more
    # Exploded sliding symbols often have no native swing arc. Use their
    # span only to SEARCH for two observed transverse jambs, never as width.
    # Require both original wall caps and at least one outward wall leg at
    # each cap; short wall returns must not be rejected by a 250 mm threshold.
    for ref,eid in segments(records):
        if not ref.crosses(probe) or not .3<=ref.length*factor<=6:continue
        ends=list(ref.coords);u=[(ends[1][k]-ends[0][k])/ref.length for k in (0,1)]
        caps=[]
        for end in ends:
            possible=[]
            for cap,capid in wall_segments:
                a,b=cap.coords;v=[b[k]-a[k] for k in (0,1)]
                if not .06<=cap.length*factor<=.5 or abs(sum(v[k]*u[k] for k in (0,1)))*factor>.002:continue
                if cap.distance(Point(end))*factor>.02:continue
                attached=any(otherid!=capid and any(Point(p).distance(other)*factor<=.002 for p in cap.coords) and other.length*factor>=.06 and abs((other.coords[-1][0]-other.coords[0][0])*u[0]+(other.coords[-1][1]-other.coords[0][1])*u[1])*factor>=.05 for other,otherid in wall_segments)
                if attached:possible.append((cap.distance(Point(end)),capid,cap))
            if not possible:break
            caps.append(min(possible,key=lambda t:(t[0],t[1])))
        if len(caps)!=2:continue
        a,b=list(caps[0][2].coords);c,d=list(caps[1][2].coords)
        if math.dist(a,c)+math.dist(b,d)>math.dist(a,d)+math.dist(b,c):c,d=d,c
        faces=[[list(a),list(c)],[list(b),list(d)]]
        portals.append(dict(faces=faces,entity_ids=[caps[0][1],caps[1][1],eid],method='source_span_with_two_observed_wall_caps'))
    openings=[];seen=set()
    for portal in portals:
        faces=portal['faces'];mid=[sum(p[k] for f in faces for p in f)/4 for k in (0,1)]
        identity=tuple(round(v*factor,3) for v in mid)
        measurement=jamb_measurement(faces,walls,factor)
        if not measurement:continue
        # Preserve different widths as competing hypotheses; identical source jambs dedup.
        key=tuple(sorted((j['entity_id'],tuple(map(tuple,j['points']))) for j in measurement['jambs']))
        if key in seen:continue
        seen.add(key)
        tangent=[faces[0][1][k]-faces[0][0][k] for k in (0,1)]
        normal=[-tangent[1],tangent[0]]
        sides=[sum((p[k]-mid[k])*normal[k] for k in (0,1)) for p in anchors]
        opposite=sides[0]*sides[1]<0
        chord=LineString(faces[0]);swing=[s for s in symbols if Polygon([faces[0][0],faces[0][1],faces[1][1],faces[1][0]]).buffer(.08/factor).covers(LineString([s['hinge'],s['closed_tip']])) and abs(math.dist(s['hinge'],s['closed_tip'])-chord.length)*factor<.2]
        oid=stable_id('aperture',json.dumps(key),str(factor))
        openings.append(dict(opening_id=oid,faces=faces,entity_ids=portal['entity_ids'],measurement=measurement,
            opposite_anchor_sides=opposite,swing_supported=bool(swing),swing_entity_ids=sorted({v for s in swing for v in s['entity_ids']}),
            association='visual_review_required',center=mid,object_id=oid,boundary=[faces[0][0],faces[0][1],faces[1][1],faces[1][0]],width_m=measurement['value'],connection_status='unresolved',diagnostics=[],room_ids=[]))
    graph=dict(openings=openings,relations=[])
    classify_openings(graph,records,symbols,list(texts),factor)
    for opening in openings:
        aperture=Polygon(opening['boundary']).buffer(.01/factor)
        legs=[LineString([anchor,opening['center']]).difference(aperture) for anchor in anchors]
        opening['visual_door_eligible']=opening['opening_type'] in ('door_candidate','unknown') and not any(t['kind']=='window' for t in opening['classification_evidence']['labels']) and opening.get('operation_type')!='conflict'
        opening['access_legs_clear']=all(not any(line.intersection(leg).length>.001/factor or line.crosses(leg) for line,_ in wall_segments) for leg in legs)
    openings.sort(key=lambda o:(not o['opposite_anchor_sides'],Point(o['center']).distance(probe),o['opening_id']))
    return dict(barriers=barriers,crossing_entities=crossings,openings=openings[:12],openings_truncated=len(openings)>12,
        probe=list(map(list,probe.coords)),wall_segment_count=len(wall_segments),diagnostics=issues,
        note='probe仅连接名称锚点以查找横穿线；不是行走路径。wall线筛选为图层线索；看图逐处核对，不能据零交点证明无墙。')


def compute_connection_evidence(payload):
    return local_evidence_from_sources(payload['records'],payload['arc_sources'],payload['factor'],
        payload['anchors'],payload['bounds'],payload.get('texts',[]))
