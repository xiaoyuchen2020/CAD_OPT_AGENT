"""Experimental geometry-only diagnostics for local part ownership.

Source identifiers are opaque. No labels, file families, or layer names enter
these routines. Existing host-independent door seeds remain unchanged.
"""
from collections import defaultdict
import math
from shapely.geometry import LineString
from .vector_recognition_v5 import WindowContext

class LocalParts(WindowContext):
    def __init__(self, primitives, options=None):
        super().__init__(primitives, options)
        self.coincident=defaultdict(list)
        for p,g in self.lines:self.coincident[tuple(sorted(p.points))].append(p.entity_id)

    def door(self,candidate):
        ev=candidate['evidence'];seed=set(ev['seed_ids']);added=set(ev['added_part_ids']);ids=seed|added
        r=ev['radius'];hinge=ev['hinge'];tip=ev['open_tip'];u=((tip[0]-hinge[0])/r,(tip[1]-hinge[1])/r);join=r*.005
        parts={e:self.byid[e] for e in ids if e in self.byid}
        # Exact duplicate copies are a single geometric edge for degree counting.
        edges={tuple(sorted(p.points)):[] for p,g in parts.values()}
        for e,(p,g) in parts.items():edges[tuple(sorted(p.points))].append(e)
        active=set(edges)
        # Remove dangling edges iteratively: a closed leaf contour survives,
        # arbitrary branches do not. Seeds themselves are never changed later.
        while active:
            remove=set()
            for pts in active:
                if any(not any(other!=pts and any(math.dist(pt,q)<=join for q in other) for other in active) for pt in pts):remove.add(pts)
            if not remove:break
            active-=remove
        core={e for pts in active for e in edges[pts]}
        reached={e for e in seed if e in parts}
        while True:
            endpoints=[pt for e in reached for pt in parts[e][0].points]
            new={e for e,(p,g) in parts.items() if e not in reached and any(math.dist(pt,q)<=join for pt in p.points for q in endpoints)}
            if not new:break
            reached.update(new)
        out={}
        for e in added:
            p,g=parts[e];pts=p.points
            dx=(pts[1][0]-pts[0][0])*u[0]+(pts[1][1]-pts[0][1])*u[1]
            dy=-(pts[1][0]-pts[0][0])*u[1]+(pts[1][1]-pts[0][1])*u[0]
            orient='parallel' if abs(dy)/g.length<=self.sin else 'transverse' if abs(dx)/g.length<=self.sin else 'oblique'
            x=sum((pt[0]-hinge[0])*u[0]+(pt[1]-hinge[1])*u[1] for pt in pts)/2/r
            external=[]
            for raw in self.tree.query(g.buffer(join),predicate='intersects'):
                q,h=self.lines[int(raw)]
                if q.entity_id in ids:continue
                if any(math.dist(a,b)<=join for a in pts for b in q.points):external.append(q.entity_id)
            out[e]=dict(orientation=orient,location='hinge' if x<=.04 else 'tip' if x>=.96 else 'middle',
                closed_contour=e in core,seed_connected=e in reached,external_connection_ids=sorted(external),
                coincident_ids=sorted(q for q in self.coincident[tuple(sorted(pts))] if q!=e),
                coincident_seed=bool(set(self.coincident[tuple(sorted(pts))])&seed),length_ratio=g.length/r)
        return out

    def cap_extensions(self,candidate,profile):
        ids=set(candidate['entity_ids']);out={}
        for eid in profile['cap_ids']:
            p,g=self.byid[eid];a,b=p.points;L=g.length;u=((b[0]-a[0])/L,(b[1]-a[1])/L)
            tol=max(profile['span']*.005,profile['spacing']*self.o.frame_alignment_ratio)
            sides=[[],[]]
            for raw in self.tree.query(g.buffer(L),predicate='intersects'):
                q,h=self.lines[int(raw)]
                if q.entity_id in ids:continue
                xy=[((pt[0]-a[0])*u[0]+(pt[1]-a[1])*u[1],-(pt[0]-a[0])*u[1]+(pt[1]-a[1])*u[0]) for pt in q.points]
                if abs(xy[1][1]-xy[0][1])/h.length>self.sin or max(abs(y) for x,y in xy)>tol:continue
                lo,hi=sorted(x for x,y in xy)
                if abs(hi)<=tol and lo<=-L:sides[0].append(q.entity_id)
                if abs(lo-L)<=tol and hi>=2*L:sides[1].append(q.entity_id)
            out[eid]=dict(left=sorted(sides[0]),right=sorted(sides[1]),
                coincident_ids=sorted(q for q in self.coincident[tuple(sorted(p.points))] if q!=eid))
        return out

from dataclasses import dataclass,asdict
from .vector_recognition_v5 import apply_v5,RefinementsV5
from .vector_recognition_v6 import build_v6
VERSION='vector-relations-v7'

@dataclass(frozen=True)
class OwnershipV7:
    door_policy:str='none'
    cap_policy:str='none'

DOOR_POLICIES={'none','seed_connected','unique','connected_unique','closed_contour'}
CAP_POLICIES={'none','both_extensions','any_extension','coincident','both_or_coincident'}

def build_v7(primitives,options=None):
    state=build_v6(primitives,options);dp={};wp={}
    if state['bundle']['complete']:
        context=LocalParts(primitives,options)
        for i,c in enumerate(state['bundle']['proposals']):
            if c['kind']=='door' and c['evidence']['topology_supported']:dp[i]=context.door(c)
        for c in state['full']['candidates']:
            if c['kind']=='window':
                key=tuple(sorted(c['entity_ids']));wp[key]=context.cap_extensions(c,state['part_profiles'][key])
    return state|dict(door_part_profiles=dp,cap_extension_profiles=wp)

def apply_v7(state,features=None):
    f=features or OwnershipV7()
    if f.door_policy not in DOOR_POLICIES or f.cap_policy not in CAP_POLICIES:raise ValueError('Unknown ownership policy')
    if not state['bundle']['complete']:return state['full']|dict(version=VERSION,ownership_v7=asdict(f))
    proposals=[];door_decisions=[]
    for i,c in enumerate(state['bundle']['proposals']):
        if c['kind']!='door' or i not in state['door_part_profiles']:
            proposals.append(c);continue
        ev=c['evidence'];profiles=state['door_part_profiles'][i];removed=[]
        for eid,p in profiles.items():
            reject={'none':False,'seed_connected':not p['seed_connected'],'unique':bool(p['coincident_ids']),
                'connected_unique':not p['seed_connected'] or bool(p['coincident_ids']),'closed_contour':not p['closed_contour']}[f.door_policy]
            if reject:removed.append(eid)
        kept=sorted(set(c['entity_ids'])-set(removed));ev=ev|dict(added_part_ids=sorted(set(ev['added_part_ids'])-set(removed)),
            observed_door_entity_ids=c['entity_ids'],unassigned_part_support_ids=sorted(removed),local_part_profiles=profiles)
        proposals.append(c|dict(entity_ids=kept,evidence=ev))
        door_decisions.append(dict(seed_ids=ev['seed_ids'],removed_added_ids=sorted(removed),profiles=profiles))
    bundle=state['bundle']|dict(proposals=proposals)
    current=apply_v5(bundle);reference=apply_v5(bundle,RefinementsV5(windows=False))
    requested=set();rail_ids=set();window_decisions=[]
    for c in current['candidates']:
        if c['kind']!='window':continue
        key=tuple(sorted(c['entity_ids']));p=state['part_profiles'][key];profiles=state['cap_extension_profiles'][key]
        rail_ids.update(p['interior_ids']+p['outer_ids']);rejected=[]
        for eid,e in profiles.items():
            both=bool(e['left'] and e['right']);either=bool(e['left'] or e['right']);duplicate=bool(e['coincident_ids'])
            reject={'none':False,'both_extensions':both,'any_extension':either,'coincident':duplicate,'both_or_coincident':both or duplicate}[f.cap_policy]
            if reject:rejected.append(eid)
        requested.update(rejected);window_decisions.append(dict(observed_frame_entity_ids=c['entity_ids'],requested_support_only_ids=rejected,profiles=profiles))
    suppressed=requested-rail_ids;candidates=[];keys=set()
    def add(c):
        key=(c['kind'],tuple(sorted(c['entity_ids'])))
        if key not in keys:keys.add(key);candidates.append(c)
    for c in current['candidates']:
        if c['kind']!='window':add(c);continue
        ids=sorted(set(c['entity_ids'])-suppressed)
        if not ids:continue
        add(c|dict(entity_ids=ids,evidence=c['evidence']|dict(observed_frame_entity_ids=c['entity_ids'],unassigned_boundary_support_ids=sorted(set(c['entity_ids'])&suppressed))))
    openings={e for c in candidates if c['kind'] in ('door','window') for e in c['entity_ids']}
    restored=[]
    # Restore only an existing independent wall proposal after its ownership
    # conflict disappears. Abstained parts are not automatically called walls.
    if suppressed:
        for c in reference['candidates']:
            if c['kind']=='wall' and not set(c['entity_ids'])&openings and (c['kind'],tuple(sorted(c['entity_ids']))) not in keys:
                restored.append(c['entity_ids']);add(c)
    roles=defaultdict(set)
    for c in candidates:
        for e in c['entity_ids']:roles[e].add(c['kind'])
    return current|dict(version=VERSION,ownership_v7=asdict(f),candidates=candidates,
        entity_roles={e:next(iter(v)) if len(v)==1 else 'ambiguous' for e,v in roles.items()},door_part_decisions=door_decisions,
        cap_extension_decisions=window_decisions,support_only_ids=sorted(suppressed),restored_wall_pairs=restored,
        original_geometry_preserved=True,automatic_room_closure=False)

def recognize_vectors_v7(primitives,options=None,features=None):return apply_v7(build_v7(primitives,options),features)
