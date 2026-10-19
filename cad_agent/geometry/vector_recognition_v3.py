"""Topology-preserving development refinement; no global line replacement.

The first development ablation rejected global merging and unrestricted symbol
growth. This version adds narrowly supported proposals to the original geometry.
"""
from dataclasses import dataclass,asdict
import math
from statistics import median

from shapely.geometry import LineString,Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .vector_recognition import Primitive,RecognitionOptions,recognize_vectors

VERSION='vector-relations-v3'


@dataclass(frozen=True)
class Refinements:
    radial_chains:bool=True
    swing_range:bool=True
    bounded_caps:bool=True
    wall_context:bool=True


def recognize_vectors_v3(primitives,options=None,features=None):
    options=options or RecognitionOptions();features=features or Refinements()
    base=recognize_vectors(primitives,options)
    if not base['complete']:
        return base|{'version':VERSION,'refinements':asdict(features)}
    lines=[(p,LineString(p.points)) for p in primitives if p.kind=='line' and len(p.points)==2 and math.dist(*p.points)>0]
    tree=STRtree([g for _,g in lines]);lengths=[g.length for _,g in lines]
    sin_tol=math.sin(math.radians(options.angular_tolerance_degrees))
    candidates=[c for c in base['candidates'] if c['kind']!='wall'];keys={(c['kind'],tuple(c['entity_ids'])) for c in candidates}
    base_door_ids={e for c in candidates if c['kind']=='door' for e in c['entity_ids']}
    def add(kind,ids,reason,**evidence):
        ids=sorted(set(ids));key=(kind,tuple(ids))
        if key not in keys:
            keys.add(key);candidates.append(dict(kind=kind,entity_ids=ids,reason=reason,status='candidate',evidence=evidence))
    if features.radial_chains or features.swing_range:
        low,high=(20,160) if features.swing_range else (options.swing_min_degrees,options.swing_max_degrees)
        for arc in primitives:
            if arc.kind!='arc' or not arc.center or not arc.radius or not arc.sweep_degrees or not low<=abs(arc.sweep_degrees)<=high:continue
            r=arc.radius;tol=r*options.radial_tolerance_ratio
            for tip,other in (arc.points,arc.points[::-1]):
                radial=LineString([arc.center,tip]);band=radial.buffer(tol);u=((tip[0]-arc.center[0])/r,(tip[1]-arc.center[1])/r)
                direct=[];pieces=[]
                for idx in tree.query(band,predicate='intersects'):
                    p,g=lines[int(idx)]
                    if not band.covers(g):continue
                    if r*.8<=g.length<=r*1.2 and g.distance(Point(arc.center))<=tol and g.distance(Point(tip))<=tol:direct.append(p.entity_id)
                    if features.radial_chains:
                        a,b=p.points;v=((b[0]-a[0])/g.length,(b[1]-a[1])/g.length)
                        if abs(u[0]*v[1]-u[1]*v[0])>sin_tol:continue
                        span=sorted((pt[0]-arc.center[0])*u[0]+(pt[1]-arc.center[1])*u[1] for pt in p.points)
                        pieces.append((span[0],span[1],p.entity_id))
                ids=direct
                if features.radial_chains and not ids and pieces:
                    # Projection coverage must form one observed chain; no box fill.
                    pieces.sort();end=pieces[0][1];ids=[pieces[0][2]];connected=pieces[0][0]<=tol
                    for a,b,eid in pieces[1:]:
                        if a>end+r*.005:connected=False;break
                        end=max(end,b);ids.append(eid)
                    if not connected or end<r-tol:ids=[]
                if ids:
                    add('door',[arc.entity_id,*ids],'swing_with_direct_or_connected_radial_leaf',hinge=arc.center,open_tip=tip,closed_tip=other,radius=r,sweep_degrees=arc.sweep_degrees)
    if features.bounded_caps:
        pair_checks=0;caps_cache={}
        def caps(i,j,spacing):
            key=(i,j);cached=caps_cache.get(key)
            if cached is not None:return cached
            a,b=lines[i][0].points;c,d=lines[j][0].points
            if math.dist(a,d)+math.dist(b,c)<math.dist(a,c)+math.dist(b,d):c,d=d,c
            supports=[];role_ids=[]
            for x,y in ((a,c),(b,d)):
                cap=LineString([x,y]);tol=spacing*options.frame_alignment_ratio;band=cap.buffer(tol)
                if cap.length<=0:return None
                cu=((y[0]-x[0])/cap.length,(y[1]-x[1])/cap.length);pieces=[];ids=[];owned=[]
                for raw in tree.query(band,predicate='intersects'):
                    k=int(raw)
                    if k in (i,j) or lengths[k]>spacing*3:continue
                    p,g=lines[k];ga,gb=p.points;gu=((gb[0]-ga[0])/lengths[k],(gb[1]-ga[1])/lengths[k])
                    if abs(cu[0]*gu[1]-cu[1]*gu[0])>sin_tol:continue
                    pieces.append(g.buffer(tol));ids.append(p.entity_id)
                    if band.covers(g):owned.append(p.entity_id)
                if not pieces or not unary_union(pieces).covers(cap):return None
                supports.extend(ids);role_ids.extend(owned)
            cached=(supports,role_ids);caps_cache[key]=cached;return cached
        for i,(p,g) in enumerate(lines):
            a,b=p.points;u=((b[0]-a[0])/g.length,(b[1]-a[1])/g.length)
            for raw in tree.query(g.buffer(g.length/options.strip_min_aspect),predicate='intersects'):
                j=int(raw)
                if j<=i:continue
                pair_checks+=1
                if pair_checks>options.max_pair_checks:
                    return base|{'version':VERSION,'complete':False,'diagnostics':['extension_pair_budget_exceeded'],'refinements':asdict(features)}
                q,h=lines[j];c,d=q.points;v=((d[0]-c[0])/h.length,(d[1]-c[1])/h.length)
                if abs(u[0]*v[1]-u[1]*v[0])>sin_tol:continue
                offsets=[(pt[0]-a[0])*(-u[1])+(pt[1]-a[1])*u[0] for pt in (c,d)]
                spacing=abs(sum(offsets)/2)
                if spacing<=min(g.length,h.length)*1e-6 or abs(offsets[0]-offsets[1])>spacing*.1:continue
                span=sorted((pt[0]-a[0])*u[0]+(pt[1]-a[1])*u[1] for pt in (c,d))
                overlap=min(g.length,span[1])-max(0,span[0])
                if overlap<spacing*options.strip_min_aspect or overlap<options.strip_min_overlap*min(g.length,h.length):continue
                support=caps(i,j,spacing)
                if not support:continue
                # Construct one buffer per strip, not once per possible interior rail.
                strip=g.union(h).convex_hull.buffer(spacing*options.frame_alignment_ratio);interior=[]
                for rawk in tree.query(strip,predicate='intersects'):
                    k=int(rawk)
                    if k in (i,j) or lengths[k]<overlap*.95:continue
                    inner,line=lines[k];e,f=inner.points;ku=((f[0]-e[0])/line.length,(f[1]-e[1])/line.length)
                    if abs(u[0]*ku[1]-u[1]*ku[0])>sin_tol or not strip.covers(line):continue
                    interior.append(inner.entity_id)
                ids=[p.entity_id,q.entity_id,*interior,*support[1]]
                if interior and not set(ids)&base_door_ids:
                    add('window',ids,'bounded_observed_crossbar_coverage_with_parallel_rails',spacing=spacing,overlap=overlap,cap_support_ids=sorted(set(support[0])))
    # Wall filtering uses only the original high-confidence right-angle swings.
    radii=[c['evidence']['radius'] for c in base['candidates'] if c['kind']=='door']
    radius=median(radii) if len(radii)>=3 else None;rejections=[]
    byid={p.entity_id:g for p,g in lines};dw={e for c in candidates for e in c['entity_ids']}
    for c in base['candidates']:
        if c['kind']!='wall':continue
        if set(c['entity_ids'])&dw:continue
        ids=c['entity_ids'];spacing=c['evidence']['spacing']
        if features.wall_context and radius and spacing<radius*.1:
            g,h=(byid[e] for e in ids);strip=g.union(h).convex_hull;band=strip.buffer(spacing*.05)
            a,b=g.coords;u=((b[0]-a[0])/g.length,(b[1]-a[1])/g.length);support=[]
            for raw in tree.query(band,predicate='intersects'):
                other,seg=lines[int(raw)]
                if other.entity_id in ids:continue
                a,b=seg.coords;v=((b[0]-a[0])/seg.length,(b[1]-a[1])/seg.length)
                if abs(u[0]*v[0]+u[1]*v[1])<=sin_tol and seg.difference(band).length>=spacing*2:support.append(other.entity_id)
            if len(set(support))<2:
                rejections.append(dict(entity_ids=ids,reason='thin_strip_without_outside_context',outside_support_ids=sorted(set(support))))
                continue
        candidates.append(c)
    roles={}
    for c in candidates:
        for eid in c['entity_ids']:roles.setdefault(eid,set()).add(c['kind'])
    return base|dict(version=VERSION,candidates=candidates,entity_roles={e:next(iter(v)) if len(v)==1 else 'ambiguous' for e,v in roles.items()},
        wall_rejections=rejections,refinements=asdict(features),original_geometry_preserved=True)
