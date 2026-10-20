"""Source-bound local geometry investigation; windows never establish identity.

All topology here uses original WCS coordinates. No clipping, gap closure,
physical tolerance or unit assumption is allowed in this prerequisite stage.
"""
from copy import deepcopy
from pydantic import Field
from shapely.geometry import Point, Polygon, LineString, box
from shapely.ops import polygonize, unary_union, nearest_points
from cad_agent.contracts import Record, stable_id

VERSION = 'scope-objects-v2-target-review'
MAX_SEGMENTS = 1200
MAX_RECORDS = 1600
MAX_DEPTH = 3


class InspectScopeObjects(Record):
    scope_id: str
    window_id: str = ''
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=8)


def inside(outer, inner):
    return inner is not None and len(inner) == 4 and outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]


def call(scope, window_id='', **extra):
    return dict(tool='inspect_scope_objects', arguments=dict(scope_id=scope['scope_id'], window_id=window_id, **extra),
                purpose='核对源图子图、局部线形与闭合边界；不授予对象身份或测量完成。')


def window(scope, bounds, role, region_ids=(), depth=0, parent=''):
    return dict(window_id=stable_id('scope_window', VERSION, scope['scope_id'], str(bounds), role),
                bbox=list(bounds), role=role, anchor_region_ids=list(region_ids), depth=depth,
                parent_window_id=parent, boundary_role='search_window_only')


def tiles(scope, bounds, n=4, depth=0, parent=''):
    a,b,c,d=bounds; dx=(c-a)/n;dy=(d-b)/n
    # Overlap permits boundary inspection across tiles. The window edge is
    # never inserted into the line graph and completeness is never inferred.
    pad=min(dx,dy)*.08
    return [window(scope,[max(a,a+x*dx-pad),max(b,b+y*dy-pad),min(c,a+(x+1)*dx+pad),min(d,b+(y+1)*dy+pad)],
                   'bounded_tile',depth=depth,parent=parent) for y in range(n) for x in range(n)]


def target_windows(session, scope, items):
    """Attach newly retrieved native labels to the already selected scope.

    Queries often happen *after* scope resolution. Keep these target windows
    separate from the generic tile catalog and never convert notes to objects.
    """
    flow = scope.get('object_workflow', {})
    if not flow:
        return []
    from cad_agent.components.context import TITLE
    texts = {t['entity_id']: t for t in session.index.scene['texts']}
    known = {a['entity_id'] for a in flow['anchors']}
    bounds = scope['bbox']; span = min(bounds[2]-bounds[0], bounds[3]-bounds[1])
    added = []
    for item in items:
        region_ids = item.get('source_region_ids', []) or ([item['region_id']] if item.get('region_id') else [])
        for rid in region_ids:
            r = session.index.by_id.get(rid)
            if not r or r.layout_id != scope['layout_id'] or r.frame_id != scope['frame_id']:
                continue
            if (len(r.text) > 32 or r.text.endswith(('说明', '做法'))
                    or any(x in r.text for x in ('。', '；', '施工前', '详见总说明'))
                    or TITLE.fullmatch(''.join(r.text.split()))):
                rejected = dict(region_id=rid, reason='sentence_note_or_title_not_object_anchor')
                if rejected not in flow['rejected_anchors']:
                    flow['rejected_anchors'].append(rejected)
                continue
            entity_ids = [item['entity_id']] if item.get('entity_id') in r.entity_ids else r.entity_ids
            for eid in entity_ids:
                t = texts.get(eid)
                if not t or eid in known:
                    continue
                p = t['origin'][:2]
                if not inside(bounds, [*p, *p]):
                    continue
                radius = min(max(float(t.get('height', 0))*16, span*.015), span*.12)
                bb = [max(bounds[0], p[0]-radius), max(bounds[1], p[1]-radius),
                      min(bounds[2], p[0]+radius), min(bounds[3], p[1]+radius)]
                w = window(scope, bb, 'native_text_anchor', [rid])
                flow['windows'].append(w); added.append(w); known.add(eid)
                flow['anchors'].append(dict(region_id=rid, entity_id=eid, text=r.text, point=p,
                    window_id=w['window_id'], identity_status='label_only'))
    return added


def candidate_pairs(records, bounds, forbidden, limit=4):
    """Bounded original-path relationships, with no semantic or box overlap claim."""
    center = Point((bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2)
    eligible = [r for r in records if r['entity_id'] not in forbidden and r.get('status') == 'ready'
                and r.get('kind') not in ('DIMENSION', 'HATCH') and r.get('paths')
                and (inside(bounds, r['bbox']) or r.get('kind') == 'LINE')]
    # A long native side crossing the crop is still inspectable. Rank by its
    # actual path, not by an enclosing bbox that happens to contain the label.
    eligible.sort(key=lambda r: (min(LineString(p).distance(center) for p in r['paths'] if len(p)>=2), r['entity_id']))
    # Pair review is deliberately small and explicit about its sampling scope.
    shapes = []
    for rec in eligible[:24]:
        paths = rec['paths']
        segments = [LineString(p) for p in paths if len(p) >= 2]
        if not segments:
            continue
        edge = unary_union(segments); polygon = None
        if rec.get('linear') and len(paths) == 1 and len(paths[0]) > 3 and paths[0][0] == paths[0][-1]:
            candidate = Polygon(paths[0])
            if candidate.is_valid and candidate.area > 0:
                polygon = candidate
        shapes.append((rec, edge, polygon))
    pairs = []
    for pos, (a, edge_a, poly_a) in enumerate(shapes):
        for b, edge_b, poly_b in shapes[pos+1:]:
            # Duplicate original paths are not two independent object sides.
            if edge_a.equals(edge_b):
                continue
            endpoints = nearest_points(edge_a, edge_b)
            if any(not inside(bounds, [p.x,p.y,p.x,p.y]) for p in endpoints):
                continue  # The minimum lies elsewhere; this crop cannot review it.
            ids = [a['entity_id'], b['entity_id']]
            overlap = poly_a.intersection(poly_b).area if poly_a is not None and poly_b is not None else None
            distance = edge_a.distance(edge_b)
            parallel = False
            if a.get('kind') == b.get('kind') == 'LINE':
                p,q = a['paths'][0]; r,s = b['paths'][0]
                u=(q[0]-p[0],q[1]-p[1]); v=(s[0]-r[0],s[1]-r[1])
                lengths=(u[0]**2+u[1]**2)*(v[0]**2+v[1]**2)
                parallel=bool(lengths and abs(u[0]*v[1]-u[1]*v[0]) <= lengths**.5*1e-7)
            pairs.append(dict(pair_id=stable_id('scope_pair', VERSION, *ids), entity_ids=ids,
                source_entities=[{k:r[k] for k in ('entity_id','entity_path','kind','layer','layout_id') if k in r} for r in (a,b)],
                identity_status='unconfirmed', basis='native_paths',
                raw_edge_distance=distance, raw_overlap_area=overlap,
                nearest_points=[list(p.coords)[0] for p in endpoints],
                parallel_native_edges=parallel,
                overlap_status='native_closed_paths_only' if overlap is not None else 'unresolved_open_geometry',
                value_unit='drawing_units', area_unit='drawing_units_squared',
                diagnostics=['object_pair_identity_unconfirmed', 'minimum_edge_distance_is_not_clear_width'],
                measure_call=dict(tool='measure_geometry', arguments=dict(operation='distance', entity_ids=ids),
                    purpose='仅计算这对源实体的最短距离；先局部图核对身份，不自动称为净宽或门开启范围。')))
    # Review nearby positive-area intersections first, then local edge pairs.
    pairs.sort(key=lambda p: (not bool(p['raw_overlap_area']),
        not (p['parallel_native_edges'] and p['raw_edge_distance']>0),
        p['raw_edge_distance']==0, p['raw_edge_distance'], p['pair_id']))
    return pairs[:limit], dict(eligible_native_entities=len(eligible), paired_native_entities=len(shapes),
        total_pairs=len(pairs), coverage='bounded_local_pairs_not_object_census',
        truncated=len(eligible)>24 or len(pairs)>limit)


def render_calls(scope, records, bounds, source_ids):
    """Crop around selected source geometry, never render the entire large tile."""
    chosen = [r for r in records if r['entity_id'] in source_ids]
    if not chosen:
        return []
    bb = [min(r['bbox'][0] for r in chosen), min(r['bbox'][1] for r in chosen),
          max(r['bbox'][2] for r in chosen), max(r['bbox'][3] for r in chosen)]
    span = max(bb[2]-bb[0], bb[3]-bb[1], 1.)
    pad = span*.15
    crop = [max(bounds[0], bb[0]-pad), max(bounds[1], bb[1]-pad),
            min(bounds[2], bb[2]+pad), min(bounds[3], bb[3]+pad)]
    if crop[0] >= crop[2] or crop[1] >= crop[3]:
        return []
    return [dict(tool='render_region', arguments=dict(layout_id=scope['layout_id'], bbox=crop,
        highlight_entity_ids=source_ids[:12]), purpose='核对所选原生实体的局部图和身份；此框只是搜索窗。')]


def pair_render_call(scope, bounds, pair):
    """A dense tile still has a bounded, actual-path intersection to inspect."""
    points = pair['nearest_points']
    pad = max(min(bounds[2]-bounds[0], bounds[3]-bounds[1])*.08,
              pair['raw_edge_distance']*.5)
    bb = [max(bounds[0],min(p[0] for p in points)-pad), max(bounds[1],min(p[1] for p in points)-pad),
          min(bounds[2],max(p[0] for p in points)+pad), min(bounds[3],max(p[1] for p in points)+pad)]
    return dict(tool='render_region',arguments=dict(layout_id=scope['layout_id'],bbox=bb,
        highlight_entity_ids=pair['entity_ids']),
        purpose='核对原生路径最近点附近的局部片段；不将此小窗当作完整对象或全图覆盖。')


def legend_context(records, anchors, titles, forbidden):
    """A native closed enclosure plus an explicit legend title is a reference.

    Text proximity alone is insufficient. Keep original border/title/anchor
    identities so a legend sample cannot silently become a placed object.
    """
    contexts = []
    for r in records:
        if r['entity_id'] in forbidden or not r.get('linear') or r.get('status') != 'ready':
            continue
        for path in r.get('paths', []):
            if len(path)<4 or path[0]!=path[-1]:
                continue
            poly=Polygon(path)
            if not poly.is_valid or poly.area<=0:
                continue
            labels=[t for t in titles if len(t['text'])<=24 and
                    ('图例' in t['text'] or 'legend' in t['text'].lower()) and
                    poly.covers(Point(t['point']))]
            targets=[a for a in anchors if poly.covers(Point(a['point']))]
            if labels and targets:
                contexts.append(dict(status='legend_reference_candidate',border_entity_id=r['entity_id'],
                    bbox=list(poly.bounds),boundary=[list(p) for p in poly.exterior.coords],title_region_ids=[t['region_id'] for t in labels],
                    anchor_region_ids=sorted({a['region_id'] for a in targets}),
                    object_identity='reference_symbol_not_confirmed_placed_object',raw_area=poly.area))
    return sorted(contexts,key=lambda c:c['raw_area'])[:3]


def prepare(session, scope, items, names=()):
    """Prepare cheap native-text anchors; geometry is read by a budgeted call."""
    from cad_agent.components.context import TITLE
    from cad_agent.cache import file_stamp
    from hashlib import file_digest
    path=session.w.store.artifact(scope['snapshot_id'],'source.dxf')
    with path.open('rb') as source:
        if file_digest(source,'sha256').hexdigest()!=scope['source_sha256']:
            raise ValueError('SCOPE_STALE：源文件哈希与快照不符。')
    scope['source_stamp']=list(file_stamp(path))
    texts={t['entity_id']:t for t in session.index.scene['texts']}
    bounds=scope['bbox']; widths=[bounds[2]-bounds[0],bounds[3]-bounds[1]]
    anchors=[];windows=[];rejected=[]
    for item in items:
        r=session.index.by_id[item['region_id']]
        if len(r.text)>32 or any(x in r.text for x in ('。','；','施工前','详见总说明')):
            rejected.append(dict(region_id=r.region_id,reason='sentence_or_note_not_object_anchor'));continue
        native=[texts[eid] for eid in r.entity_ids if eid in texts]
        if not native:
            rejected.append(dict(region_id=r.region_id,reason='no_native_text_anchor'));continue
        for t in native:
            p=t['origin'][:2]
            if not inside(bounds,[*p,*p]):continue
            # Drawing-coordinate scale from native text height. No mm/metre
            # inference; bounded by frame dimensions and expanded by review.
            radius=max(float(t.get('height',0))*16, min(widths)*.015)
            radius=min(radius,min(widths)*.12)
            bb=[max(bounds[0],p[0]-radius),max(bounds[1],p[1]-radius),min(bounds[2],p[0]+radius),min(bounds[3],p[1]+radius)]
            w=window(scope,bb,'native_text_anchor',[r.region_id]);windows.append(w)
            anchors.append(dict(region_id=r.region_id,entity_id=t['entity_id'],text=r.text,point=p,
                                window_id=w['window_id'],identity_status='label_only'))
    subframes=[]
    for r in session.index.scope(scope['layout_id'],scope['frame_id']):
        title=''.join(r.text.split())
        if len(title)<=60 and TITLE.fullmatch(title):
            subframes.append(dict(region_id=r.region_id,title=r.text,title_bbox=r.bbox,
                extent_status='unresolved',bbox=None,boundary_role='title_anchor_only'))
    # Source closed borders only; a label bbox is never eligible here.
    border_ids=set(scope.get('border_entity_ids',[]))
    all_frames=[f['bbox'] for f in session.index.drawing['frames'] if f['layout_id']==scope['layout_id'] and f.get('method')=='closed_border_and_title_candidate']
    for line in session.index.scene['lines']:
        if line['layout_id']!=scope['layout_id']:continue
        pts=line['points']
        if len(pts)<4 or pts[0]!=pts[-1]:continue
        bb=[min(p[0] for p in pts),min(p[1] for p in pts),max(p[0] for p in pts),max(p[1] for p in pts)]
        if any(all(abs(a-b)<1e-5 for a,b in zip(bb,f)) for f in all_frames):border_ids.add(line['entity_id'])
        if len(pts)==5 and inside(bounds,bb):
            vectors=[(b[0]-a[0],b[1]-a[1]) for a,b in zip(pts,pts[1:])]
            rectangular=all(abs(v[0]*u[0]+v[1]*u[1]) <= 1e-7*max(1,(v[0]**2+v[1]**2)**.5*(u[0]**2+u[1]**2)**.5)
                            for v,u in zip(vectors,vectors[1:]+vectors[:1]))
            if not rectangular:continue
            hits=[sf for sf in subframes if inside(bb,sf['title_bbox'])]
            if len(hits)==1 and bb!=bounds:
                sf=hits[0]; old=sf['bbox']
                # Reject a text rectangle: require space around the title.
                tb=sf['title_bbox']
                if (bb[2]-bb[0])*(bb[3]-bb[1]) < 25*max(1,(tb[2]-tb[0])*(tb[3]-tb[1])):continue
                if old is None or (bb[2]-bb[0])*(bb[3]-bb[1]) < (old[2]-old[0])*(old[3]-old[1]):
                    sf.update(bbox=bb,extent_status='single_title_native_border_candidate',boundary_role='search_scope_only',border_entity_id=line['entity_id'])
                    border_ids.add(line['entity_id'])
    scope['border_entity_ids']=sorted(border_ids)
    # The tile catalog remains available for unlabelled objects and outer
    # boundaries, even when some requested labels were found.
    for sf in subframes:
        if sf['bbox'] is not None:
            windows.append(window(scope,sf['bbox'],'subframe_border_candidate',[sf['region_id']]))
    windows += tiles(scope,bounds)
    dedup={w['window_id']:w for w in windows}
    scope['object_workflow']=dict(version=VERSION,anchors=anchors,subframes=subframes,
        windows=list(dedup.values()),requested_names=list(names),rejected_anchors=rejected,
        subframe_extent_status='native_border_candidates_require_review' if any(sf['bbox'] for sf in subframes) else 'unresolved_search_frame_only',geometry_status='not_read',
        coverage='local_windows_not_object_census', unit= session.index.drawing['unit'])


def candidates_from_records(records, bounds, forbidden):
    """Inspect observed native paths and exact line-graph faces, with hard caps."""
    usable=[r for r in records if r['entity_id'] not in forbidden and r.get('kind')!='DIMENSION'
            and r.get('status')=='ready']
    excluded=len(records)-len(usable);lines=[];owners=[];closed=[];issues=[]
    center=Point((bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2)
    usable.sort(key=lambda r:(box(*r['bbox']).distance(center),r['entity_id']))
    if len(usable)>MAX_RECORDS:issues.append('record_limit_exceeded')
    for r in usable[:MAX_RECORDS]:
        if r.get('kind')=='HATCH':continue # fill loops are not object edges
        for path in r['paths']:
            if len(path)<2:continue
            # Only complete original paths; never use window clipping to close.
            if r.get('linear') and len(path)>3 and path[0]==path[-1]:
                poly=Polygon(path)
                if poly.is_valid and poly.area>0:
                    closed.append((poly,[r['entity_id']],'native_closed_path'))
            if r.get('linear') and inside(bounds,r['bbox']):
                for a,b in zip(path,path[1:]):
                    if a==b:continue
                    if len(lines)>=MAX_SEGMENTS:
                        issues.append('segment_limit_exceeded');break
                    lines.append(LineString([a,b]));owners.append(r['entity_id'])
    # Avoid expensive quadratic noding in dense symbols. Exceeding a bound
    # leads to subdivision, never silently omits linework and claims closure.
    if len(lines)<MAX_SEGMENTS:
        from shapely.strtree import STRtree
        tree=STRtree(lines); pairs=sum(len(tree.query(l)) for l in lines)
        if pairs<=20000:
            for face in polygonize(unary_union(lines)):
                shell=Polygon(face.exterior)
                ids=sorted({owners[int(i)] for i in tree.query(shell.boundary) if shell.boundary.covers(lines[int(i)])})
                if ids:closed.append((shell,ids,'observed_line_graph'))
        else:issues.append('intersection_limit_exceeded')
    rows=[];seen=set()
    for poly,ids,method in closed:
        key=poly.normalize().wkb
        if key in seen:continue
        seen.add(key)
        points=[list(p) for p in poly.exterior.coords]
        # No bounding rectangle area substitutes for the actual source ring.
        rows.append(dict(candidate_id=stable_id('scope_object',VERSION,poly.wkb_hex,*ids),
            kind='closed_boundary_candidate',method=method,entity_ids=ids[:64],entity_count=len(ids),
            source_ids_truncated=len(ids)>64,bbox=list(poly.bounds),boundary=points[:96],
            boundary_truncated=len(points)>96,closed=True,raw_area=poly.area,raw_perimeter=poly.length,
            value_unit='drawing_units',identity_status='unconfirmed',boundary_role='observed_geometry_candidate',
            crosses_window=not inside(bounds,list(poly.bounds)),distance_to_anchor_raw=poly.distance(center),
            diagnostics=['object_identity_unconfirmed']+(['boundary_crosses_local_window'] if not inside(bounds,list(poly.bounds)) else [])))
    rows.sort(key=lambda c:(c['distance_to_anchor_raw'],-c['raw_area'],c['candidate_id']))
    # Keep open sides, arcs and isolated lines reviewable as well: a ramp need
    # not be a closed polygon and door swing arcs must not be lost.
    for r in usable[:MAX_RECORDS]:
        if r.get('kind')=='HATCH':continue
        rows.append(dict(candidate_id=stable_id('scope_edge',VERSION,r['entity_id']),kind='source_geometry_candidate',
            entity_ids=[r['entity_id']],bbox=r['bbox'],entity_kind=r.get('kind'),
            closed=bool(r['paths']) and all(p[0]==p[-1] for p in r['paths']),
            linear=r.get('linear'),identity_status='unconfirmed',boundary_role='source_geometry_only',
            crosses_window=not inside(bounds,r['bbox']),diagnostics=['object_identity_unconfirmed']))
    return rows,sorted(set(issues)),excluded


def inspect(session,args):
    from cad_agent.analysis.scope_workflow import selected_for_call
    from cad_agent.geometry.source import scoped_geometry_index
    scope=selected_for_call(session,'inspect_scope_objects')
    if not scope or scope['scope_id']!=args.scope_id:raise ValueError('SCOPE_MISMATCH：对象审查须使用本要求已绑定范围。')
    workflow=scope.get('object_workflow')
    if not workflow:raise ValueError('SCOPE_STALE：请重新解析范围以建立对象窗口。')
    base=dict(kind='scope_object_candidates',scope_id=scope['scope_id'],snapshot_id=scope['snapshot_id'],source_sha256=scope['source_sha256'],
        layout_id=scope['layout_id'],frame_id=scope['frame_id'],unit=workflow['unit'],status='needs_review',
        identity_status='unconfirmed',coverage='bounded_geometry_only_not_object_census',
        operation_receipts=[dict(operation='geometry',fulfilled=False)],
        note='标题/图框/文字范围只作检索锚点；原始闭合线形也不证明建筑、坡道或设备身份。先局部图和源实体核验，再选择对象测量；未知单位不产生米制结果。')
    if not args.window_id:
        wins=workflow['windows']; page=wins[args.offset:args.offset+args.limit]
        continuation = [call(scope,w['window_id']) for w in page]
        if args.offset+len(page)<len(wins):
            continuation.append(call(scope, offset=args.offset+len(page), limit=args.limit))
        return base|dict(items=page,total=len(wins),has_more=args.offset+len(page)<len(wins),
            next_offset=args.offset+len(page) if args.offset+len(page)<len(wins) else None,
            subframes=workflow['subframes'][args.offset:args.offset+args.limit],subframe_count=len(workflow['subframes']),
            subframes_has_more=args.offset+args.limit<len(workflow['subframes']),
            anchors=workflow['anchors'][args.offset:args.offset+args.limit],anchor_count=len(workflow['anchors']),
            anchors_has_more=args.offset+args.limit<len(workflow['anchors']),
            rejected_anchors=workflow['rejected_anchors'][args.offset:args.offset+args.limit],rejected_anchor_count=len(workflow['rejected_anchors']),
            subframe_extent_status=workflow['subframe_extent_status'],
            next_calls=continuation)
    w=next((w for w in workflow['windows'] if w['window_id']==args.window_id),None)
    if not w:raise ValueError('SCOPE_WINDOW：窗口不属于已绑定范围。')
    index=scoped_geometry_index(session.w,scope['snapshot_id'],scope['layout_id'],w['bbox'],workflow['unit'])
    records=index.query(scope['layout_id'],w['bbox'],'intersects')
    # Geometry crossing the frame is not allowed to acquire the selected title.
    records=[r for r in records if inside(scope['bbox'],r['bbox'])]
    forbidden=set(scope.get('title_entity_ids',[]))|set(scope.get('border_entity_ids',[]))
    rows,issues,excluded=candidates_from_records(records,w['bbox'],forbidden)
    anchors=[a for a in workflow['anchors'] if a['region_id'] in w['anchor_region_ids']]
    titles=[dict(region_id=r.region_id,text=r.text,point=[(r.bbox[0]+r.bbox[2])/2,(r.bbox[1]+r.bbox[3])/2])
            for r in session.index.scope(scope['layout_id'],scope['frame_id']) if '图例' in r.text or 'legend' in r.text.lower()]
    references=legend_context(records,anchors,titles,forbidden)
    known_references = workflow.setdefault('reference_contexts', [])
    for reference in references:
        if reference not in known_references:
            known_references.append(deepcopy(reference))
    if not index.data['complete']:issues.append('incomplete_geometry_source')
    children=[]
    if (issues or len(records)>800) and w['depth']<MAX_DEPTH:
        children=tiles(scope,w['bbox'],n=2,depth=w['depth']+1,parent=w['window_id'])
        known={x['window_id'] for x in workflow['windows']}
        workflow['windows'].extend(x for x in children if x['window_id'] not in known)
    page=rows[args.offset:args.offset+args.limit]
    for row in page:
        if row.get('source_ids_truncated'):
            continue  # A truncated source list cannot bind a complete object.
        row['bind_call'] = dict(tool='bind_spatial_object', arguments=dict(scope_id=scope['scope_id'],
            entity_ids=row['entity_ids'], label_region_ids=sorted({a['region_id'] for a in anchors}),
            role='source_geometry'), purpose='Persist exact source identity and independently verify native label containment; semantic role stays open.')
    pairs, pair_coverage = candidate_pairs(records, w['bbox'], forbidden)
    continuation=[]
    if children:continuation=[call(scope,x['window_id']) for x in children]
    elif args.offset+len(page)<len(rows):continuation=[call(scope,w['window_id'],offset=args.offset+len(page),limit=args.limit)]
    if references:
        # The reference symbol is useful for later identity review, but source
        # geometry investigation must move beyond the legend's own enclosure.
        reference_box=box(*references[0]['bbox'])
        alternatives=[x for x in workflow['windows'] if x['role']=='bounded_tile' and
                      x['depth']==0 and not box(*x['bbox']).intersects(reference_box)]
        alternatives.sort(key=lambda x:box(*x['bbox']).distance(reference_box))
        continuation=[call(scope,x['window_id']) for x in alternatives[:2]]
        for pair in pairs:
            pair.pop('measure_call',None)
            pair['reference_only']=True
    source_ids=list(dict.fromkeys([e for p in pairs[:2] for e in p['entity_ids']] +
                                 [e for row in page for e in row['entity_ids']]))[:12]
    # Reuse only source-verified extent metadata for the two scope guards in
    # a normal invocation. The actual read/measurement still opens source
    # geometry. A changed file stamp or hash invalidates this bounded cache.
    from cad_agent.cache import file_stamp
    stamp=list(file_stamp(session.w.store.artifact(scope['snapshot_id'],'source.dxf')))
    cache=scope.get('source_entity_extents',{})
    if cache.get('source_stamp')!=stamp or cache.get('source_sha256')!=scope['source_sha256']:
        cache=dict(source_stamp=stamp,source_sha256=scope['source_sha256'],items={})
    by_id={r['entity_id']:r for r in records}
    for eid in source_ids:
        if eid in by_id:
            r=by_id[eid];cache['items'][eid]={k:r[k] for k in ('entity_id','layout_id','bbox')}
    cache['items']=dict(list(cache['items'].items())[-256:])
    scope['source_entity_extents']=cache
    actions=[]
    if source_ids:
        actions.append(dict(tool='read_geometry',arguments=dict(entity_ids=source_ids),purpose='读取候选真实源路径，核对闭合与对象边界。'))
    if len(records)<=800:
        actions.extend(render_calls(scope, records, w['bbox'], source_ids))
    elif pairs and not children:
        # At the tile-depth cap, move to a source-derived local crop rather
        # than giving up on rendering or relaxing the geometry completeness cap.
        actions.append(pair_render_call(scope, w['bbox'], pairs[0]))
    actions.append(dict(tool='read_dimensions',arguments=dict(layout_id=scope['layout_id'],frame_id=scope['frame_id'],bbox=w['bbox'],offset=0,limit=5),purpose='仅核对局部尺寸及控制点；不自动认作目标净宽。'))
    workflow['geometry_status'] = 'local_candidates_read_identity_unconfirmed'
    review = workflow.setdefault('window_reviews', {})
    review[w['window_id']] = dict(entity_ids=source_ids, pair_ids=[p['pair_id'] for p in pairs],
        measurement_calls=[p['measure_call'] for p in pairs if 'measure_call' in p], review_calls=deepcopy(actions),
        identity_status='unconfirmed', offset=args.offset, total=len(rows))
    return base|dict(window=deepcopy(w),items=page,total=len(rows),has_more=args.offset+len(page)<len(rows),
        next_offset=args.offset+len(page) if args.offset+len(page)<len(rows) else None,
        source_record_count=len(records),excluded_annotation_or_border_count=excluded,
        diagnostics=sorted(set(issues+(['target_anchor_in_native_legend_enclosure'] if references else []))),
        geometry_complete=index.data['complete'] and not issues,
        source_diagnostics=index.data.get('diagnostics',[])[:8],subdivision_windows=children,
        candidate_pairs=pairs,pair_coverage=pair_coverage,reference_context=references,
        bind_next_calls=[row['bind_call'] for row in page if 'bind_call' in row],
        next_calls=continuation,review_calls=actions,
        measurement_gate=dict(object_identity='requires_source_and_visual_review',units='unknown' if workflow['unit']=='unknown' else 'dxf_header',
            ready=False,next_tool='measure_geometry',note='核对后用实际实体ID测距/长度/闭合面积；面积只适用于真实闭合路径。未知单位保留raw值并返回unresolved，不猜单位。'))
