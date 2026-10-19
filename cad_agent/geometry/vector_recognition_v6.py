"""Experimental separation of window evidence from semantic part ownership.

Geometry and opaque IDs only. Door completion remains v5's host-independent
seeded method. A structural support is not automatically a wall or a window.
"""
from dataclasses import dataclass,asdict
import math
from shapely.geometry import LineString
from .vector_recognition import RecognitionOptions
from .vector_recognition_v5 import WindowContext,build_v5,apply_v5,RefinementsV5
VERSION='vector-relations-v6'

class PartContext(WindowContext):
    def parts(self,candidate):
        ids=set(candidate['entity_ids']);parts=[self.byid[e] for e in ids if e in self.byid]
        empty=dict(interior_ids=[],outer_ids=[],cap_ids=[],other_ids=sorted(ids),continued_ids=[],continuation_support_ids=[],distinct_interior_levels=0,shared_cap_ids=[])
        if not parts:return empty
        p,g=max(parts,key=lambda pg:pg[1].length);a,b=p.points;L=g.length;u=((b[0]-a[0])/L,(b[1]-a[1])/L)
        def xy(pt):return ((pt[0]-a[0])*u[0]+(pt[1]-a[1])*u[1],-(pt[0]-a[0])*u[1]+(pt[1]-a[1])*u[0])
        rails=[];caps=[];other=[]
        for q,h in parts:
            pts=[xy(pt) for pt in q.points];dx=abs(pts[1][0]-pts[0][0]);dy=abs(pts[1][1]-pts[0][1])
            if dy/h.length<=self.sin and dx>=L*self.o.strip_min_overlap:
                rails.append((q.entity_id,min(t[0] for t in pts),max(t[0] for t in pts),sum(t[1] for t in pts)/2))
            elif dx/h.length<=self.sin:caps.append(q.entity_id)
            else:other.append(q.entity_id)
        if len(rails)<2:return empty
        low=min(r[3] for r in rails);high=max(r[3] for r in rails);w=high-low
        if w<=L*1e-6:return empty
        tol=w*self.o.frame_alignment_ratio;join=max(L*.005,tol);reach=min(w*2,L*.25)
        inside=[r for r in rails if low+tol<r[3]<high-tol];outer=[r for r in rails if r not in inside]
        levels=[]
        for r in sorted(inside,key=lambda r:r[3]):
            if not levels or r[3]-levels[-1]>tol:levels.append(r[3])
        context=[]
        for raw in self.tree.query(g.buffer(max(w*3,reach)),predicate='intersects'):
            q,h=self.lines[int(raw)]
            if q.entity_id in ids:continue
            pts=[xy(pt) for pt in q.points]
            if abs(pts[1][1]-pts[0][1])/h.length>self.sin:continue
            context.append((q.entity_id,pts))
        continued=[];support=set();endpoint_support={}
        for eid,start,end,off in rails:
            left=[];right=[]
            for rid,pts in context:
                if max(abs(y-off) for x,y in pts)>join:continue
                x0,x1=sorted(x for x,y in pts)
                # Entire witness stays beyond a jamb, rather than crossing a
                # window in an unrelated overlapping view.
                if abs(x1-start)<=join and x0<=start-reach:left.append(rid)
                if abs(x0-end)<=join and x1>=end+reach:right.append(rid)
            endpoint_support[eid]=dict(left=left,right=right)
            if left and right:continued.append(eid);support.update(left+right)
        shared=[]
        for eid in caps:
            q,h=self.byid[eid];pts=[xy(pt) for pt in q.points];x=sum(pt[0] for pt in pts)/2
            side='left' if abs(x)<=join else 'right' if abs(x-L)<=join else None
            if side is None:continue
            # A cap chain at a jamb is shared support when both outer faces
            # meet separately observed outside runs on that side.
            edges=[r for r in outer if endpoint_support.get(r[0],{}).get(side)]
            if edges and min(r[3] for r in edges)<=low+tol and max(r[3] for r in edges)>=high-tol:
                shared.append(eid)
                for r in edges:support.update(endpoint_support[r[0]][side])
        return dict(shared_cap_ids=sorted(shared),interior_ids=sorted(r[0] for r in inside),outer_ids=sorted(r[0] for r in outer),cap_ids=sorted(caps),other_ids=sorted(other),
            continued_ids=sorted(continued),continuation_support_ids=sorted(support),distinct_interior_levels=len(levels),span=L,spacing=w)

@dataclass(frozen=True)
class Ownership:
    cap_policy:str='shared_or_wall'
    outer_support:bool=False
    restore_walls:bool=True

CAP_POLICIES={'none','all','shared','wall_evidence','shared_or_wall'}

def build_v6(primitives,options=None):
    bundle=build_v5(primitives,options)
    full=apply_v5(bundle,RefinementsV5())
    reference=apply_v5(bundle,RefinementsV5(windows=False))
    profiles={}
    if bundle['complete']:
        context=PartContext(primitives,options)
        for c in full['candidates']:
            if c['kind']=='window':profiles[tuple(sorted(c['entity_ids']))]=context.parts(c)
    return dict(bundle=bundle,full=full,door_reference=reference,part_profiles=profiles)

def apply_v6(state,features=None):
    f=features or Ownership()
    if f.cap_policy not in CAP_POLICIES:raise ValueError('Unknown cap ownership policy')
    current=state['full']
    if not current['complete']:return current|dict(version=VERSION,ownership=asdict(f))
    reference=state['door_reference'];wall_evidence={e for c in reference['candidates'] if c['kind']=='wall' for e in c['entity_ids']+c['evidence'].get('return_entity_ids',[])}
    profiles=state['part_profiles'];requested=set();rail_evidence=set();decisions=[]
    for c in current['candidates']:
        if c['kind']!='window':continue
        p=profiles[tuple(sorted(c['entity_ids']))];caps=set(p['cap_ids']);shared=set(p['shared_cap_ids']);known=caps&wall_evidence
        rail_evidence.update(p['interior_ids']+p['outer_ids'])
        selected={'none':set(),'all':caps,'shared':shared,'wall_evidence':known,'shared_or_wall':shared|known}[f.cap_policy]
        requested.update(selected)
        decisions.append(dict(original_entity_ids=c['entity_ids'],cap_support_requested=sorted(selected),part_profile=p))
    # A perpendicular cap in one proposal may be a longitudinal rail in another.
    # Preserve that positive rail evidence; do not globally erase it as a cap.
    suppressed_caps=requested-rail_evidence
    suppressed_outer=set()
    if f.outer_support:
        for p in profiles.values():suppressed_outer.update(set(p['outer_ids'])&set(p['continued_ids']))
    suppressed=suppressed_caps|suppressed_outer;candidates=[];keys=set()
    def add(c):
        key=(c['kind'],tuple(sorted(c['entity_ids'])))
        if key not in keys:keys.add(key);candidates.append(c)
    for c in current['candidates']:
        if c['kind']!='window':add(c);continue
        ids=sorted(set(c['entity_ids'])-suppressed);support=sorted(set(c['entity_ids'])&suppressed)
        if not ids:continue
        add(c|dict(entity_ids=ids,reason=c['reason']+'_source_part_ownership',evidence=c['evidence']|dict(
            observed_frame_entity_ids=c['entity_ids'],shared_boundary_support_ids=support,
            ownership_profile=profiles[tuple(sorted(c['entity_ids']))])))
    # Restore only previously observed wall proposals that were removed because
    # an opening claimed a shared boundary. Never relabel all rejected caps wall.
    restored=[]
    if f.restore_walls:
        opening={e for c in candidates if c['kind'] in ('window','door') for e in c['entity_ids']}
        for c in reference['candidates']:
            if c['kind']!='wall' or set(c['entity_ids'])&opening:continue
            key=(c['kind'],tuple(sorted(c['entity_ids'])))
            if key not in keys:restored.append(c['entity_ids']);add(c)
    roles={}
    for c in candidates:
        for eid in c['entity_ids']:roles.setdefault(eid,set()).add(c['kind'])
    return current|dict(version=VERSION,ownership=asdict(f),candidates=candidates,entity_roles={e:next(iter(v)) if len(v)==1 else 'ambiguous' for e,v in roles.items()},
        ownership_decisions=decisions,support_only_ids=sorted(suppressed),restored_wall_pairs=restored,original_geometry_preserved=True)

def recognize_vectors_v6(primitives,options=None,features=None):return apply_v6(build_v6(primitives,options),features)
