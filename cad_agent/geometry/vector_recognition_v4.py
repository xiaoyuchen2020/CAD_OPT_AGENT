"""Experimental source-bound host walls and local opening topology.

No semantic annotations, layer names, drawing IDs or ground-truth walls enter
this module. Host witnesses are independently observed lines outside an opening;
they are evidence only and never become opening geometry. No room closure.
"""
from dataclasses import dataclass,asdict
import math
from shapely.geometry import LineString,Point
from shapely.ops import unary_union
from shapely.strtree import STRtree
from .vector_recognition import RecognitionOptions
from .vector_recognition_v3 import recognize_vectors_v3,Refinements
VERSION='vector-relations-v4'

@dataclass(frozen=True)
class Relations:
    host_wall:bool=True
    local_topology:bool=True
    door_parts:bool=True
    window_caps:bool=True


def build_relations(primitives,options=None):
    """Compute one geometry proposal pool shared by the frozen ablations."""
    o=options or RecognitionOptions()
    base=recognize_vectors_v3(primitives,o,Refinements(bounded_caps=False))
    if not base['complete']:return dict(base=base,proposals=[],complete=False)
    lines=[(p,LineString(p.points)) for p in primitives if p.kind=='line' and len(p.points)==2 and math.dist(*p.points)>0]
    tree=STRtree([g for p,g in lines]);sintol=math.sin(math.radians(o.angular_tolerance_degrees))
    lengths=[g.length for p,g in lines];units=[((p.points[1][0]-p.points[0][0])/g.length,(p.points[1][1]-p.points[0][1])/g.length) for p,g in lines]
    proposals=[];checks=0;keys=set()
    def coords(pt,a,u):
        dx,dy=pt[0]-a[0],pt[1]-a[1]
        return dx*u[0]+dy*u[1],-dx*u[1]+dy*u[0]
    def host(a,b,width,excluded,door=False):
        L=math.dist(a,b)
        if L<=0:return []
        u=((b[0]-a[0])/L,(b[1]-a[1])/L)
        reach=L*.5 if door else width*3
        tol=max(L*.005,width*o.frame_alignment_ratio);sides=[[],[]]
        for raw in tree.query(LineString([a,b]).buffer(reach),predicate='intersects'):
            k=int(raw);p,g=lines[k]
            if p.entity_id in excluded:continue
            v=units[k]
            if abs(u[0]*v[1]-u[1]*v[0])>sintol:continue
            xy=[coords(pt,a,u) for pt in p.points];lo,hi=sorted(x for x,y in xy);off=sum(y for x,y in xy)/2
            if abs(off)>reach or abs(xy[0][1]-xy[1][1])>tol:continue
            # Entire host is outside the opening except drafting tolerance.
            if hi<=tol and hi>=-tol and lo<=-max(width*2,L*.1):sides[0].append((off,k))
            if lo>=L-tol and lo<=L+tol and hi>=L+max(width*2,L*.1):sides[1].append((off,k))
        pairs=[]
        for side in sides:
            found=[]
            for ii,(x,i) in enumerate(side):
                for y,j in side[ii+1:]:
                    low,high=sorted((x,y));gap=high-low
                    if gap<width*.8 or gap>reach*2:continue
                    # The opening axis must lie between the two outside faces.
                    if low>tol or high<-tol:continue
                    found.append((low,high,i,j))
            pairs.append(found)
        for low,high,i,j in pairs[0]:
            for lo,hi,k,m in pairs[1]:
                if abs(low-lo)<=tol and abs(high-hi)<=tol:
                    return sorted({lines[z][0].entity_id for z in (i,j,k,m)})
        return []
    def cap(a,b,spacing,excluded):
        axis=LineString([a,b]);tol=spacing*o.frame_alignment_ratio
        if axis.length<=0:return [],[]
        u=((b[0]-a[0])/axis.length,(b[1]-a[1])/axis.length);band=axis.buffer(tol);pieces=[];support=[];owned=[]
        for raw in tree.query(band,predicate='intersects'):
            k=int(raw);p,g=lines[k]
            if p.entity_id in excluded or lengths[k]>spacing*3:continue
            v=units[k]
            if abs(u[0]*v[1]-u[1]*v[0])>sintol:continue
            pieces.append(g.buffer(tol));support.append(p.entity_id)
            if band.covers(g):owned.append(p.entity_id)
        return (support,owned) if pieces and unary_union(pieces).covers(axis) else ([],[])
    # Windows: at least three aligned observed rails, not a generated filled box.
    # Aspect 3 tests compact frames; all metrics/parameters frozen before scoring.
    for i,(p,g) in enumerate(lines):
        a,b=p.points;L=lengths[i];u=units[i]
        for raw in tree.query(g.buffer(L/3),predicate='intersects'):
            j=int(raw)
            if j<=i:continue
            checks+=1
            if checks>o.max_pair_checks:return dict(base=base,proposals=[],complete=False,diagnostic='relation_pair_budget_exceeded')
            q,h=lines[j];v=units[j]
            if abs(u[0]*v[1]-u[1]*v[0])>sintol:continue
            xy=[coords(pt,a,u) for pt in q.points];off=sum(y for x,y in xy)/2;w=abs(off)
            if w<=min(L,lengths[j])*1e-6 or w>L/3 or abs(xy[0][1]-xy[1][1])>w*.1:continue
            span=sorted(x for x,y in xy);tol=L*o.frame_alignment_ratio
            if abs(span[0])>tol or abs(span[1]-L)>tol:continue
            c,d=q.points
            if math.dist(a,d)<math.dist(a,c):c,d=d,c
            strip=g.union(h).convex_hull.buffer(w*o.frame_alignment_ratio);interior=[]
            for rawk in tree.query(strip,predicate='intersects'):
                k=int(rawk)
                if k in (i,j):continue
                v=units[k]
                if abs(u[0]*v[1]-u[1]*v[0])>sintol or not strip.covers(lines[k][1]):continue
                # Do not absorb tiny arbitrary parallel ticks as a third rail.
                if lengths[k]>=L*.5:interior.append(lines[k][0].entity_id)
            if not interior:continue
            rails=[p.entity_id,q.entity_id,*interior];ex=set(rails)
            lcap,lowned=cap(a,c,w,ex);rcap,rowned=cap(b,d,w,ex)
            center_a=((a[0]+c[0])/2,(a[1]+c[1])/2);center_b=((b[0]+d[0])/2,(b[1]+d[1])/2)
            hosts=host(center_a,center_b,w,ex|set(lcap+rcap))
            if not hosts and not (lcap and rcap):continue
            ids=sorted(set(rails+lowned+rowned));key=('window',tuple(ids))
            if key in keys:continue
            keys.add(key);proposals.append(dict(kind='window',entity_ids=ids,reason='observed_parallel_rails_with_external_host_and_caps',status='candidate',
                evidence=dict(host_support_ids=hosts,cap_support_ids=sorted(set(lcap+rcap)),host_supported=bool(hosts),topology_supported=bool(lcap and rcap),spacing=w,overlap=L)))
    # Doors: retained swing/leaf seed; add only connected lines in a narrow leaf
    # corridor. Independent host evidence may recover an arc whose leaf is absent.
    seed_by_arc={}
    for c in base['candidates']:
        if c['kind']=='door':
            for eid in c['entity_ids']:seed_by_arc.setdefault(eid,[]).append(c)
    for arc in primitives:
        if arc.kind!='arc' or not arc.center or not arc.radius or not arc.sweep_degrees or not 20<=abs(arc.sweep_degrees)<=160:continue
        r=arc.radius
        for tip,closed in (arc.points,arc.points[::-1]):
            seeds=[c for c in seed_by_arc.get(arc.entity_id,[]) if math.dist(c['evidence']['open_tip'],tip)<r*.005]
            seedids={arc.entity_id}|{e for c in seeds for e in c['entity_ids']}
            hosts=host(arc.center,closed,r*.04,seedids,door=True)
            if not seeds and not hosts:continue
            radial=LineString([arc.center,tip]);band=radial.buffer(r*.04);near=[]
            for raw in tree.query(band,predicate='intersects'):
                k=int(raw);p,g=lines[k]
                if p.entity_id not in hosts and band.covers(g):near.append(k)
            # Endpoint graph, no repeated distance expansion beyond the corridor.
            visited={k for k in near if lines[k][0].entity_id in seedids}
            anchors=[arc.center,tip]
            changed=True
            while changed:
                changed=False
                pts=anchors+[pt for k in visited for pt in lines[k][0].points]
                for k in near:
                    if k not in visited and any(math.dist(pt,x)<=r*.005 for pt in lines[k][0].points for x in pts):visited.add(k);changed=True
            grown={lines[k][0].entity_id for k in visited}-seedids
            proposals.append(dict(kind='door',entity_ids=sorted(seedids|grown),reason='swing_with_host_and_connected_leaf_parts',status='candidate',
                evidence=dict(host_support_ids=hosts,host_supported=bool(hosts),topology_supported=bool(seeds),seed_ids=sorted(seedids),added_part_ids=sorted(grown),hinge=arc.center,open_tip=tip,closed_tip=closed,radius=r,sweep_degrees=arc.sweep_degrees)))
    return dict(base=base,proposals=proposals,complete=True,pair_checks=checks)


def apply_relations(bundle,features=None):
    f=features or Relations();base=bundle['base']
    if not bundle['complete']:
        return base|dict(version=VERSION,complete=False,diagnostics=base.get('diagnostics',[])+[bundle.get('diagnostic','incomplete_base')],relations=asdict(f))
    candidates=list(base['candidates']);keys={(c['kind'],tuple(sorted(c['entity_ids']))) for c in candidates};new_ids=set()
    for original in bundle['proposals']:
        c=original.copy();ev=c['evidence']
        if f.host_wall and not ev['host_supported']:continue
        if c['kind']=='window':
            if f.local_topology and f.window_caps and not ev['topology_supported']:continue
            if not f.host_wall and not ev['topology_supported']:continue
        else:
            if not f.host_wall and not ev['topology_supported']:continue
            if not f.door_parts or not f.local_topology:c['entity_ids']=ev['seed_ids']
        key=(c['kind'],tuple(sorted(c['entity_ids'])))
        if key not in keys:candidates.append(c);keys.add(key);new_ids.update(c['entity_ids'])
    # Resolve inherited wall proposals only on actually assigned opening parts.
    # Host witness IDs never enter this ownership set.
    candidates=[c for c in candidates if c['kind']!='wall' or not set(c['entity_ids'])&new_ids]
    roles={}
    for c in candidates:
        for eid in c['entity_ids']:roles.setdefault(eid,set()).add(c['kind'])
    return base|dict(version=VERSION,relations=asdict(f),candidates=candidates,entity_roles={k:next(iter(v)) if len(v)==1 else 'ambiguous' for k,v in roles.items()},
        relation_pair_checks=bundle['pair_checks'],original_geometry_preserved=True)


def recognize_vectors_v4(primitives,options=None,features=None):
    return apply_relations(build_relations(primitives,options),features)
