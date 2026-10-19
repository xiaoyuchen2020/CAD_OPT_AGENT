"""Experimental opening topology; geometry-only, independent door completion.

A wall-like window veto is supported by observed rails continuing outside its
jambs or transverse lines crossing its interior. Rejections never fabricate wall
geometry. Door leaf parts require an existing swing/leaf seed; host evidence can
be reported without forcing a hard gate. No room closure or area computation.
"""
from dataclasses import dataclass,asdict
import math
from shapely.geometry import LineString
from shapely.strtree import STRtree
from .vector_recognition import RecognitionOptions
from .vector_recognition_v4 import build_relations,apply_relations,Relations
VERSION='vector-relations-v5'

class WindowContext:
    def __init__(self,primitives,options=None):
        self.o=options or RecognitionOptions()
        self.lines=[(p,LineString(p.points)) for p in primitives if p.kind=='line' and len(p.points)==2 and math.dist(*p.points)>0]
        self.byid={p.entity_id:(p,g) for p,g in self.lines};self.tree=STRtree([g for p,g in self.lines])
        self.sin=math.sin(math.radians(self.o.angular_tolerance_degrees))
    def profile(self,candidate):
        ids=set(candidate['entity_ids']);parts=[self.byid[e] for e in ids if e in self.byid]
        if not parts:return dict(through_rail_ids=[],crossing_ids=[],continuation_support_ids=[],independent_interior_ids=[])
        p,g=max(parts,key=lambda pg:pg[1].length);a,b=p.points;L=g.length;u=((b[0]-a[0])/L,(b[1]-a[1])/L)
        def xy(pt):return ((pt[0]-a[0])*u[0]+(pt[1]-a[1])*u[1],-(pt[0]-a[0])*u[1]+(pt[1]-a[1])*u[0])
        rails=[]
        for q,h in parts:
            v=((q.points[1][0]-q.points[0][0])/h.length,(q.points[1][1]-q.points[0][1])/h.length)
            if h.length>=L*.5 and abs(u[0]*v[1]-u[1]*v[0])<=self.sin:
                points=[xy(pt) for pt in q.points];rails.append((q.entity_id,min(t[0] for t in points),max(t[0] for t in points),sum(t[1] for t in points)/2))
        if len(rails)<3:return dict(through_rail_ids=[],crossing_ids=[],continuation_support_ids=[],independent_interior_ids=[])
        low=min(r[3] for r in rails);high=max(r[3] for r in rails);w=high-low
        if w<=L*1e-6:return dict(through_rail_ids=[],crossing_ids=[],continuation_support_ids=[],independent_interior_ids=[])
        tol=max(L*.005,w*self.o.frame_alignment_ratio);reach=min(w*2,L*.25)
        # Search outside endpoints and the interior. Projection intervals must
        # actually connect to the rail; no extrapolated or filled-in extension.
        around=[]
        for raw in self.tree.query(g.buffer(max(w*3,reach)),predicate='intersects'):
            q,h=self.lines[int(raw)]
            if q.entity_id in ids:continue
            points=[xy(pt) for pt in q.points];dx=points[1][0]-points[0][0];dy=points[1][1]-points[0][1]
            around.append((q.entity_id,points,abs(dy)/h.length,abs(dx)/h.length))
        through=[];support=set();crossings=[]
        for eid,start,end,off in rails:
            if off<=low+w*.02 or off>=high-w*.02:continue
            sides=[[],[]]
            for rid,pts,parallel,perp in around:
                if parallel>self.sin or max(abs(y-off) for x,y in pts)>tol:continue
                x0,x1=sorted(x for x,y in pts)
                if x0<start:sides[0].append((x0,min(x1,start),rid))
                if x1>end:sides[1].append((max(x0,end),x1,rid))
            def reaches(intervals,origin,direction):
                projected=sorted(((origin-hi,origin-lo,rid) if direction<0 else (lo-origin,hi-origin,rid)) for lo,hi,rid in intervals)
                end=0;used=[]
                for lo,hi,rid in projected:
                    if hi<0:continue
                    if lo>end+tol:break
                    end=max(end,hi);used.append(rid)
                    if end>=reach:return used
                return []
            left=reaches(sides[0],start,-1);right=reaches(sides[1],end,1)
            if left and right:through.append(eid);support.update(left+right)
        for eid,pts,parallel,perp in around:
            if perp>self.sin:continue
            x=sum(pt[0] for pt in pts)/2;y0,y1=sorted(pt[1] for pt in pts)
            # A transverse line must pass through the complete frame and extend
            # beyond both faces, well away from its jambs.
            if L*.1<x<L*.9 and y0<=low-w and y1>=high+w:crossings.append(eid)
        independent=[eid for eid,start,end,off in rails if low+w*self.o.frame_alignment_ratio<off<high-w*self.o.frame_alignment_ratio and end-start>=L*self.o.strip_min_overlap]
        return dict(through_rail_ids=sorted(through),continuation_support_ids=sorted(support),crossing_ids=sorted(crossings),independent_interior_ids=sorted(independent),rail_count=len(rails),span=L,spacing=w)

@dataclass(frozen=True)
class RefinementsV5:
    windows:bool=True
    independent_inner:bool=True
    continuation_veto:bool=False
    crossing_veto:bool=False
    door_parts:bool=True
    door_host_hard:bool=False

def build_v5(primitives,options=None):
    bundle=build_relations(primitives,options)
    if not bundle['complete']:return bundle|{'window_profiles':{}}
    context=WindowContext(primitives,options);profiles={}
    for c in bundle['base']['candidates']+bundle['proposals']:
        if c['kind']=='window':profiles[tuple(sorted(c['entity_ids']))]=context.profile(c)
    return bundle|{'window_profiles':profiles}

def apply_v5(bundle,features=None):
    f=features or RefinementsV5();base=bundle['base']
    if not bundle['complete']:
        return base|dict(version=VERSION,complete=False,diagnostics=base.get('diagnostics',[])+[bundle.get('diagnostic','incomplete_base')],refinements=asdict(f))
    candidates=[];rejections=[];keys=set();opening_ids=set()
    def add(c):
        key=(c['kind'],tuple(sorted(c['entity_ids'])))
        if key in keys:return
        keys.add(key);candidates.append(c)
    def accept_window(c):
        profile=bundle['window_profiles'][tuple(sorted(c['entity_ids']))]
        reasons=[]
        if f.independent_inner and not profile['independent_interior_ids']:reasons.append('no_independent_interior_rail_spanning_frame')
        if f.continuation_veto and profile['through_rail_ids']:reasons.append('interior_rail_continues_beyond_both_jambs')
        if f.crossing_veto and profile['crossing_ids']:reasons.append('transverse_run_crosses_window_interior')
        if reasons:
            rejections.append(dict(entity_ids=c['entity_ids'],reasons=reasons,evidence=profile));return False
        add(c|{'evidence':c['evidence']|{'wall_conflict_profile':profile}});return True
    for c in base['candidates']:
        if c['kind']=='wall':continue
        if c['kind']=='window' and f.windows:accept_window(c)
        else:add(c)
    for c in bundle['proposals']:
        ev=c['evidence']
        if c['kind']=='window':
            if f.windows and ev['host_supported'] and ev['topology_supported'] and accept_window(c):opening_ids.update(c['entity_ids'])
        elif f.door_parts and ev['topology_supported']:
            # Missing-leaf arcs do not seed growth. Host absence is independent
            # from connected-part evidence, exposed as a separate ablation.
            if f.door_host_hard and not ev['host_supported']:continue
            add(c|{'reason':'observed_seed_with_connected_leaf_parts_independent_of_host'});opening_ids.update(c['entity_ids'])
    for c in base['candidates']:
        if c['kind']=='wall' and not set(c['entity_ids'])&opening_ids:add(c)
    roles={}
    for c in candidates:
        for eid in c['entity_ids']:roles.setdefault(eid,set()).add(c['kind'])
    return base|dict(version=VERSION,refinements=asdict(f),candidates=candidates,entity_roles={e:next(iter(v)) if len(v)==1 else 'ambiguous' for e,v in roles.items()},
        window_rejections=rejections,relation_pair_checks=bundle['pair_checks'],original_geometry_preserved=True)

def recognize_vectors_v5(primitives,options=None,features=None):return apply_v5(build_v5(primitives,options),features)
