"""Composite door leaves and sliding panels, independent of room topology."""
import math
from itertools import combinations

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union


def double_swing_pairs(symbols, factor):
    pairs=[]
    for a,b in combinations(symbols,2):
        if a['arc_id']==b['arc_id']:continue
        h,k=a['hinge'],b['hinge'];span=math.dist(h,k)
        if span*factor<.6 or math.dist(a['closed_tip'],b['closed_tip'])*factor>.06:continue
        radius=(a['radius_m']+b['radius_m'])/factor
        if abs(span-radius)*factor>.06:continue
        chord=LineString([h,k])
        if chord.distance(Point(a['closed_tip']))*factor>.035 or chord.distance(Point(b['closed_tip']))*factor>.035:continue
        u=[(k[i]-h[i])/span for i in (0,1)]
        # Both leaves open onto the same half-plane. Radial-leaf evidence is
        # already required separately for each native arc by swing_symbols.
        offsets=[]
        for s in (a,b):
            tip=s.get('open_tip')
            if tip is None:break
            offsets.append((tip[0]-h[0])*(-u[1])+(tip[1]-h[1])*u[0])
        if len(offsets)!=2 or offsets[0]*offsets[1]<=0:continue
        pairs.append(dict(hinges=[h,k],meeting_point=[(a['closed_tip'][i]+b['closed_tip'][i])/2 for i in (0,1)],
            symbol_span_m=round(span*factor,6),leaves=[a,b],
            entity_ids=sorted(set(a['entity_ids']+b['entity_ids'])),method='two_radial_leaves_with_meeting_closed_tips'))
    return pairs


def match_double_swing(opening, pairs, line_records, factor):
    poly=Polygon(opening['boundary']);f=opening['faces'][0];width=math.dist(*f)
    u=[(f[1][i]-f[0][i])/width for i in (0,1)];matches=[]
    for pair in pairs:
        chord=LineString(pair['hinges']);span=chord.length
        projected=[sum((p[i]-f[0][i])*u[i] for i in (0,1)) for p in pair['hinges']]
        if not .55*width<=span<=1.2*width or min(projected)<-.08/factor or max(projected)>width+.08/factor:continue
        # Chord must run along the aperture, not a perpendicular nearby door.
        if abs(projected[1]-projected[0])<span*.98:continue
        frame_ids=[];offset=chord.distance(poly)*factor
        if not poly.buffer(.08/factor).covers(chord):
            if offset>.35:continue
            zone=unary_union([poly,chord]).convex_hull.buffer(.04/factor)
            pieces=[]
            for g,eid in line_records:
                if not g.intersects(zone):continue
                a,b=g.coords;alignment=abs(sum((b[i]-a[i])*u[i] for i in (0,1)))/g.length
                # Oblique leaders crossing the gap are not jamb/frame links.
                if alignment<.98 and alignment>.02:continue
                pieces.append((g.intersection(zone),eid))
            joined=unary_union([g.buffer(.035/factor) for g,_ in pieces])
            parts=list(joined.geoms) if hasattr(joined,'geoms') else [joined]
            for hinge in pair['hinges']:
                part=next((g for g in parts if g.covers(Point(hinge)) and g.intersects(poly)),None)
                if part is None:break
                frame_ids.extend(eid for g,eid in pieces if g.intersects(part))
            else:
                matches.append(pair|dict(frame_offset_m=round(offset,6),frame_binding_entity_ids=sorted(set(frame_ids)),
                    opening_coverage=round(span/width,6),binding='observed_frame_connects_offset_door_to_wall_aperture'))
                continue
            continue
        matches.append(pair|dict(frame_offset_m=round(offset,6),frame_binding_entity_ids=[],
            opening_coverage=round(span/width,6),binding='door_chord_inside_aperture'))
    return matches


def sliding_panel_evidence(opening, records, factor):
    """Closed thin leaves on separate tracks; also accept exploded rectangles.

    A connected group must cover the aperture and contain positive overlap on
    different tracks. Fixed side-by-side glazing and parallel rails fail this.
    All four edges remain observed; polygon completion never invents a cap.
    """
    f=opening['faces'][0];width=math.dist(*f);u=[(f[1][i]-f[0][i])/width for i in (0,1)]
    poly=Polygon(opening['boundary']);zone=poly.buffer(.04/factor);panels={};local_lines=[]
    def local(p):
        d=[p[i]-f[0][i] for i in (0,1)]
        return [sum(d[i]*u[i] for i in (0,1)),d[0]*(-u[1])+d[1]*u[0]]
    def world(x,y):return [f[0][i]+x*u[i]+y*[-u[1],u[0]][i] for i in (0,1)]
    def add(path,ids):
        shape=Polygon(path)
        if not shape.is_valid or not zone.covers(shape):return
        pts=[local(p) for p in path]
        x0,x1=min(p[0] for p in pts),max(p[0] for p in pts);y0,y1=min(p[1] for p in pts),max(p[1] for p in pts)
        long=x1-x0;short=y1-y0
        if not .15*width<=long<=.8*width or not .008<=short*factor<=.12:return
        if abs(shape.area-long*short)>long*short*.08:return
        key=tuple(round(v*factor,4) for v in (x0,x1,y0,y1))
        panels.setdefault(key,dict(entity_ids=sorted(set(ids)),span=[x0,x1],track_offset=(y0+y1)/2,
            thickness_m=short*factor,boundary=path))
    for rec in records:
        if not rec.get('linear') or rec['kind']=='DIMENSION':continue
        for path in rec['paths']:
            if len(path)>=5 and math.dist(path[0],path[-1])*factor<=.002:add(path,[rec['entity_id']])
            for a,b in zip(path,path[1:]):
                line=LineString([a,b])
                if line.length and zone.covers(line):local_lines.append((LineString([local(a),local(b)]),rec['entity_id']))
    # Bounded local reconstruction of exploded rectangular leaves. Do not snap
    # disconnected endpoints or polygonize furnishings elsewhere in the scope.
    if len(local_lines)<=500:
        along=[]
        for line,eid in local_lines:
            a,b=line.coords
            if abs(a[1]-b[1])*factor<=.001 and .15*width<=abs(a[0]-b[0])<=.8*width:
                along.append((min(a[0],b[0]),max(a[0],b[0]),(a[1]+b[1])/2,eid))
        for a,b in combinations(along,2):
            if max(abs(a[i]-b[i]) for i in (0,1))*factor>.002 or not .008<=abs(a[2]-b[2])*factor<=.12:continue
            ids=[a[3],b[3]]
            for x in a[:2]:
                cap=LineString([(x,a[2]),(x,b[2])]);support=[(g,eid) for g,eid in local_lines if cap.buffer(.002/factor).covers(g)]
                if not support or not unary_union([g.buffer(.002/factor) for g,_ in support]).covers(cap):break
                ids.extend(eid for _,eid in support)
            else:add([world(a[0],a[2]),world(a[1],a[2]),world(b[1],b[2]),world(b[0],b[2]),world(a[0],a[2])],ids)
    panels=list(panels.values())
    if len(panels)>24:return []
    adjacency={i:set() for i in range(len(panels))};overlaps={}
    for i,j in combinations(range(len(panels)),2):
        a,b=panels[i],panels[j];al,ah=a['span'];bl,bh=b['span']
        overlap=(min(ah,bh)-max(al,bl))*factor;spacing=abs(a['track_offset']-b['track_offset'])*factor
        sliding=(.005<=overlap<=min(ah-al,bh-bl)*factor*.3 and max(a['thickness_m'],b['thickness_m'])*.8<=spacing<=.15)
        butt=abs(overlap)<=.002 and spacing<=.002
        if sliding or butt:
            adjacency[i].add(j);adjacency[j].add(i)
            if sliding:overlaps[i,j]=dict(overlap_m=round(overlap,6),track_spacing_m=round(spacing,6))
    groups=[];seen=set()
    for i in adjacency:
        if i in seen:continue
        stack=[i];component=set()
        while stack:
            j=stack.pop()
            if j in component:continue
            component.add(j);stack.extend(adjacency[j]-component)
        seen.update(component);proof=[v for (a,b),v in overlaps.items() if a in component and b in component]
        if not proof:continue
        leaves=[panels[j] for j in sorted(component)];span=max(p['span'][1] for p in leaves)-min(p['span'][0] for p in leaves)
        if span<width*.75:continue
        groups.append(dict(entity_ids=sorted({eid for p in leaves for eid in p['entity_ids']}),panels=leaves,
            track_overlaps=proof,panel_count=len(leaves),symbol_span_m=round(span*factor,6),
            method='overlapping_closed_panels_on_separate_tracks'))
    return groups
