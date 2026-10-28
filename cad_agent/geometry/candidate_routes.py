"""Candidate pairing and conditional sampled profiles on selected native sources."""
import itertools,math
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from cad_agent.geometry.local_measurement import measure_pair


def candidates(records,kind,anchor=None,neighbors=12):
    selected=[r for r in records if r['kind']==kind and r['status']=='ready']
    considered=len(selected);partial=False
    shape=lambda r:unary_union([LineString(p) for p in r.get('paths',[]) if len(p)>1])
    anchor_points=None
    if anchor:
        n=anchor['native']
        if anchor['kind']!='DIMENSION' or int(n.get('dimtype',0)) & 7 not in (0,1) or 'defpoint2' not in n or 'defpoint3' not in n:
            raise ValueError('LINEAR_DIMENSION_ANCHOR_REQUIRED')
        anchor_points=[Point(n[k][:2]) for k in ('defpoint2','defpoint3')]
        ranks=[[r for r in sorted(selected,key=lambda r:(shape(r).distance(p),r['entity_id']))[:neighbors]] for p in anchor_points]
        ids={r['entity_id'] for group in ranks for r in group}
        selected=[r for r in selected if r['entity_id'] in ids];partial=len(selected)<considered
    out=[]
    from cad_agent.harness.scheduler import ordered_compute
    pairs = itertools.combinations(selected, 2)
    payloads = (dict(a=a, b=b, basis='parallel_normal' if kind=='LINE' else 'concentric_radial')
                for a, b in itertools.combinations(selected, 2))
    for (a, b), m in zip(pairs, ordered_compute('measure_pair', payloads)):
        if m['status']!='local_pair_measured':continue
        domain=m['domain'];extent=domain.get('overlap_length',sum(v-u for u,v in domain.get('intervals_degrees',[])))
        score=None
        if anchor_points:
            ga,gb=shape(a),shape(b);p,q=anchor_points
            score=min(ga.distance(p)+gb.distance(q),ga.distance(q)+gb.distance(p))
        out.append(dict(entity_ids=m['source_entity_ids'],source_paths=[a['entity_path'],b['entity_path']],
            basis=m['basis'],width_drawing_units=m['measurements']['width_drawing_units'],domain=domain,
            anchor_endpoint_residual=score,common_extent=extent,
            interpretation='geometrically_applicable_pair_not_confirmed_opposing_faces'))
    out.sort(key=lambda x:((x['anchor_endpoint_residual'] or 0),-x['common_extent'],x['entity_ids']))
    return dict(items=out,source_candidates=considered,paired_sources=len(selected),anchor_prefiltered=partial,
        ranking='dimension_endpoint_residual_then_common_extent' if anchor else 'common_extent_only',
        target_complete=False,rule_threshold_used=False)


def intervals_difference(requested,available):
    pending=[list(requested)]
    for low,high in sorted(available):
        new=[]
        for a,b in pending:
            if high<=a or low>=b:new.append([a,b]);continue
            if low>a:new.append([a,low])
            if high<b:new.append([high,b])
        pending=new
    return pending


def sample_pair(a,b,count=9,obstacles=(),requested_interval=None):
    """Projection effects are conditional on co-height, never inferred from XY."""
    m=measure_pair(a,b,'parallel_normal' if a['kind']=='LINE' else 'concentric_radial')
    if m['status']!='local_pair_measured':return m|dict(samples=[],uncovered_reason='pair_not_measurable')
    domain=m['domain'];intervals=[domain['interval']] if domain['kind']=='linear' else domain['intervals_degrees']
    unavailable=[];polys=[]
    for r in obstacles:
        paths=r.get('paths',[])
        if len(paths)!=1 or len(paths[0])<4 or paths[0][0]!=paths[0][-1]:
            unavailable.append(r['entity_id']);continue
        polygon=Polygon(paths[0])
        if not polygon.is_valid or polygon.area<=0:unavailable.append(r['entity_id']);continue
        polys.append((r['entity_id'],polygon))
    if requested_interval and (len(requested_interval)!=2 or not all(math.isfinite(v) for v in requested_interval) or requested_interval[1]<=requested_interval[0]):
        raise ValueError('INVALID_REQUESTED_INTERVAL')
    active_intervals=intervals if requested_interval is None else [[max(lo,requested_interval[0]),min(hi,requested_interval[1])] for lo,hi in intervals if min(hi,requested_interval[1])>max(lo,requested_interval[0])]
    rows=[]
    for lo,hi in active_intervals:
        events=[lo,hi]
        for _,polygon in polys:
            for vertex in polygon.exterior.coords:
                if domain['kind']=='linear':
                    t=sum((vertex[k]-domain['origin'][k])*domain['direction'][k] for k in (0,1))
                else:
                    t=math.degrees(math.atan2(vertex[1]-domain['center'][1],vertex[0]-domain['center'][0]))%360
                if lo<t<hi:events.append(t)
        events=sorted(set(events))
        stations=sorted(set([lo+(hi-lo)*(i+.5)/count for i in range(count)]+[(x+y)/2 for x,y in zip(events,events[1:])]))
        for t in stations:
            if domain['kind']=='linear':
                p=domain['origin'];u=domain['direction'];x=[p[k]+t*u[k] for k in (0,1)]
                start,end=b['native']['start'],b['native']['end']
                v=[end[k]-start[k] for k in (0,1)]
                along=sum((start[k]-p[k])*u[k] for k in (0,1));span=sum(v[k]*u[k] for k in (0,1))
                y=[start[k]+(t-along)/span*v[k] for k in (0,1)]
            else:
                u=[math.cos(math.radians(t)),math.sin(math.radians(t))]
                x=[a['native']['center'][k]+a['native']['radius']*u[k] for k in (0,1)]
                y=[b['native']['center'][k]+b['native']['radius']*u[k] for k in (0,1)]
            cut=LineString([x,y]);middle=cut.interpolate(.5,normalized=True)
            active=[(eid,p) for eid,p in polys if p.intersects(cut)]
            union=unary_union([p for _,p in active]) if active else None
            free=cut.difference(union) if active else cut
            components=list(free.geoms) if hasattr(free,'geoms') else [free]
            through=[s for s in components if s.geom_type=='LineString' and s.distance(middle)<max(cut.length,1)*1e-10]
            blocked=bool(active and union.covers(middle))
            width=through[0].length if len(through)==1 and not blocked else None
            rows.append(dict(station=t,measurement_line=[x,y],baseline_width_drawing_units=cut.length,
                projected_obstacle_ids=[eid for eid,_ in active],height_relation='unknown',
                conditional_coheight_width_drawing_units=width,
                conditional_status='axis_blocked' if blocked else 'measured' if width is not None else 'ambiguous_free_component'))
    uncovered=intervals_difference(requested_interval,intervals) if requested_interval else None
    return dict(status='sampled_candidate_profile',source_entity_ids=m['source_entity_ids'],domain=domain,
        requested_interval=requested_interval,uncovered_declared_intervals=uncovered,
        route_extent='explicit_hypothesis' if requested_interval else 'unconfirmed',
        samples=rows,unsupported_obstacle_ids=unavailable,
        sample_minimum_drawing_units=min((r['baseline_width_drawing_units'] for r in rows),default=None),
        global_minimum_proven=False,obstacle_census='selected_only',
        interpretation='conditional_projected_geometry; neither physical clearance nor whole-route minimum',
        sampling='uniform_plus_obstacle_vertex_event_midpoints; transitions_and_extrema_not_proven',
        approximation=dict(boundary_error_bound=m.get('error_bound'),obstacles='source tessellation; physical precision unknown when units unknown'))
