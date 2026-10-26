"""Task-owned plan scopes and budgeted, source-bound continuation calls."""
from copy import deepcopy
from cad_agent.contracts import stable_id
from cad_agent.analysis.tool_metadata import SPATIAL_METHODS

SCOPED_TOOLS = {'search_similar_objects', 'bind_spatial_object', 'get_spatial_object', 'inspect_scope_objects', 'read_dimensions', 'check_dimension_chain', 'query_spatial', 'read_geometry',
                'measure_geometry', 'discover_rooms', 'measure_room', 'query_building_relations',
                'render_region', 'get_room_contours', 'measure_room_area', 'get_component_evidence', 'get_measurement_evidence',
                'analyze_measurements'} | SPATIAL_METHODS
PLAN_ONLY_TOOLS = {'analyze_room_batch', 'discover_rooms', 'measure_room', 'get_room_contours',
                   'measure_room_area', 'prepare_room_boundaries', 'select_room_boundaries',
                   'prepare_discovery_review', 'review_discovered_spaces',
                   'prepare_space_connections', 'review_space_connections',
                   'query_building_relations', 'inspect_enclosure'}
SCOPED_TOOLS |= PLAN_ONLY_TOOLS


def owners(session):
    trace = getattr(session, 'trace', [])
    ids = trace[-1].get('task_ids', []) if trace else []
    if not ids:
        ids = [s['id'] for s in session.task.data['subtasks'] if s['status'] == 'running']
    return ids


def remember_scope(session, selected, items, names=()):
    """Persist identities, not inferred building outlines or completed operations."""
    data = session.task.data
    ids = owners(session)
    active = data.setdefault('active_scope_by_task', {})
    unresolved = data.setdefault('unresolved_scope_by_task', {})
    if selected is None:
        for key in ids:
            active.pop(key, None)
            unresolved[key] = 'no_unique_eligible_scope'
        return []
    from cad_agent.analysis.scope_objects import prepare, call as object_call
    prepare(session, selected, items, names)
    data.setdefault('drawing_scopes', {})[selected['scope_id']] = deepcopy(selected)
    for key in ids:
        active[key] = selected['scope_id']
        unresolved.pop(key, None)
    measurable = [s['id'] for s in data['subtasks'] if s['id'] in ids and
                  (s['kind'] == 'measure' or set(s.get('operations', [])) &
                   {'geometry', 'dimensions', 'area', 'perimeter', 'contours', 'relations', 'discovery'})]
    calls = []
    if measurable:
        windows = selected['object_workflow']['windows']
        anchored = [w for w in windows if w['role'] == 'native_text_anchor']
        calls = [object_call(selected, w['window_id']) for w in anchored[:2]] or [object_call(selected)]
    queued = data.setdefault('scope_next_calls', [])
    for call in calls:
        call.update(scope_id=selected['scope_id'], task_ids=measurable, operation='scope_measurement_next')
        call['call_id'] = stable_id('scope_call', selected['scope_id'], call['tool'], str(call['arguments']))
        existing = next((c for c in queued if c['call_id'] == call['call_id']), None)
        if existing is None:
            queued.append(deepcopy(call))
        else:
            existing['task_ids'] = sorted(set(existing['task_ids']) | set(measurable))
    return calls


def contains(outer, inner):
    return (inner is not None and len(inner) == 4 and outer[0] <= inner[0] <= inner[2] <= outer[2]
            and outer[1] <= inner[1] <= inner[3] <= outer[3])


def selected_for_call(session, name):
    if name not in SCOPED_TOOLS:
        return None
    data = session.task.data
    if not data.get('active_scope_by_task') and not data.get('unresolved_scope_by_task'):
        return None
    if any(i in data.get('unresolved_scope_by_task', {}) for i in owners(session)):
        # A source crop can help resolve conflicting titles. Its finite bbox,
        # layout, source identity and image limits are checked by VisionService;
        # it does not establish an engineering scope or clear this unresolved flag.
        if name == 'render_region':
            return None
        raise ValueError('SCOPE_UNRESOLVED：本次要求尚无唯一合格范围；请先核对候选及冲突标题。')
    keys = {data.get('active_scope_by_task', {}).get(i) for i in owners(session)} - {None}
    if not keys:
        return None
    if len(keys) != 1:
        raise ValueError('SCOPE_CONFLICT：本次要求绑定了不同图纸范围；请分别调用。')
    scope = data.get('drawing_scopes', {}).get(next(iter(keys)))
    if scope and scope.get('view_kind', 'plan') != 'plan' and name in PLAN_ONLY_TOOLS:
        raise ValueError('SCOPE_VIEW: 此工具要求平面范围，不能将剖面或详图当作房间平面。')
    drawing = session.w.store.get('drawing', session.request.snapshot_id)
    if not scope or scope['snapshot_id'] != session.request.snapshot_id or scope['source_sha256'] != drawing['sha256']:
        raise ValueError('SCOPE_STALE：范围与当前快照或源文件不一致，请重新解析。')
    if scope.get('source_stamp'):
        from cad_agent.cache import file_stamp
        if scope['source_stamp'] != list(file_stamp(session.w.store.artifact(session.request.snapshot_id, 'source.dxf'))):
            scope.pop('source_entity_extents', None)
            raise ValueError('SCOPE_STALE：源文件已改变，请重新解析范围。')
    frame = next((f for f in drawing['frames'] if f['frame_id'] == scope['frame_id']), None)
    if not frame or frame['layout_id'] != scope['layout_id'] or frame['bbox'] != scope['bbox']:
        raise ValueError('SCOPE_STALE：图框身份已改变，请重新解析。')
    return scope


def bind_scope_call(session, name, args):
    scope = selected_for_call(session, name)
    if scope is None:
        return args
    if name == 'analyze_measurements':
        from cad_agent.analysis.result_sets import collection
        validate_measurement_collection(session, collection(session, args), scope)
    update = {}
    for key in ('layout_id', 'frame_id'):
        if key not in type(args).model_fields:
            continue
        value = getattr(args, key)
        if value and value != scope[key]:
            raise ValueError('SCOPE_MISMATCH：调用的布局或图框不属于此要求已选定范围。')
        update[key] = scope[key]
    bounds = getattr(args, 'bbox', None)
    if bounds is not None and not contains(scope['bbox'], bounds):
        raise ValueError('SCOPE_BOUNDS：调用范围超出已选定平面。')
    if 'bbox' in type(args).model_fields and bounds is None:
        update['bbox'] = scope['bbox']
    if name == 'search_similar_objects':
        for box in (args.sample_bbox, args.search_bbox):
            if box is not None and not contains(scope['bbox'], box):
                raise ValueError('SCOPE_BOUNDS：相似搜索的样本或范围超出已选定图框。')
        if args.search_bbox is None:
            update['search_bbox'] = scope['bbox']
    if name == 'query_spatial':
        update['relation'] = 'within'
    region_id = getattr(args, 'region_id', '')
    if region_id:
        region = session.index.by_id.get(region_id)
        if not region or region.frame_id != scope['frame_id'] or region.layout_id != scope['layout_id']:
            raise ValueError('SCOPE_REGION：名称来源不属于已选定平面。')
    seed = getattr(args, 'seed', None)
    if seed is not None and not contains(scope['bbox'], [*seed, *seed]):
        raise ValueError('SCOPE_SEED：室内点超出已选定平面。')
    entity_ids = getattr(args, 'entity_ids', [])
    if entity_ids:
        from cad_agent.geometry.service import GeometryService
        from cad_agent.cache import file_stamp
        cached = scope.get('source_entity_extents', {})
        stamp = list(file_stamp(session.w.store.artifact(session.request.snapshot_id, 'source.dxf')))
        if cached and cached.get('source_stamp') != stamp:
            scope.pop('source_entity_extents', None)
            raise ValueError('SCOPE_STALE：源文件已改变，请重新解析范围。')
        known = cached.get('items', {}) if (cached.get('source_sha256') == scope['source_sha256'] and
                                          cached.get('source_stamp') == stamp) else {}
        missing = [eid for eid in entity_ids if eid not in known]
        records = ([known[eid] for eid in entity_ids if eid in known] +
                   (GeometryService(session.w, session.request.snapshot_id).records(missing) if missing else []))
        forbidden = set(scope.get('title_entity_ids', [])) | set(scope.get('border_entity_ids', []))
        for rec in records:
            if rec['entity_id'] in forbidden or rec['layout_id'] != scope['layout_id'] or not contains(scope['bbox'], rec['bbox']):
                raise ValueError('SCOPE_ENTITY：测量对象不属于选定平面，或对象是标题/图框边界。')
            if name == 'measure_geometry' and all(abs(x-y) < 1e-6 for x, y in zip(rec['bbox'], scope['bbox'])):
                raise ValueError('SCOPE_FRAME_NOT_GEOMETRY：图框不能作为建筑轮廓测量。')
    discovery_id = getattr(args, 'discovery_id', '')
    if discovery_id:
        from cad_agent.geometry.service import GeometryService
        record = GeometryService(session.w, session.request.snapshot_id).get(discovery_id)
        req = record.get('request', {})
        if req.get('layout_id') != scope['layout_id'] or not contains(scope['bbox'], req.get('bbox', [])):
            raise ValueError('SCOPE_DISCOVERY：围合候选不属于选定平面。')
    record_id = getattr(args, 'record_id', '')
    if record_id:
        from cad_agent.components.service import ComponentService
        record = ComponentService(session.w, session.request.snapshot_id).get(record_id)
        request = record.get('request', {})
        if request.get('layout_id') != scope['layout_id'] or not contains(scope['bbox'], request.get('bbox')):
            raise ValueError('SCOPE_COMPONENT：组件记录不属于选定平面。')
    measurement_id = getattr(args, 'measurement_id', '')
    if measurement_id:
        from cad_agent.geometry.service import GeometryService
        record = GeometryService(session.w, session.request.snapshot_id).get(measurement_id)
        if record.get('source_sha256') != scope['source_sha256']:
            raise ValueError('SCOPE_STALE：测量来源哈希与选定平面不一致。')
        request = record.get('request', {})
        if request.get('region_id'):
            from cad_agent.geometry.contracts import RoomMeasure
            bind_scope_call(session, 'measure_room', RoomMeasure.model_validate(request))
        elif request.get('entity_ids'):
            from cad_agent.geometry.contracts import GeometryRead
            bind_scope_call(session, 'read_geometry', GeometryRead(entity_ids=request['entity_ids']))
        elif request.get('layout_id') != scope['layout_id'] or not contains(scope['bbox'], request.get('bbox')):
            raise ValueError('SCOPE_MEASUREMENT：测量记录未绑定选定平面。')
    return args.model_copy(update=update)


def validate_measurement_collection(session, batch, scope=None):
    """Validate the collection and its geometry, before cache use or receipts."""
    scope = scope or selected_for_call(session, 'analyze_measurements')
    if scope is None:
        return
    if (batch.get('source_sha256') != scope['source_sha256'] or
            batch.get('snapshot_id', scope['snapshot_id']) != scope['snapshot_id']):
        raise ValueError('SCOPE_STALE：测量集合来源与此要求已选定范围不一致。')
    bounds = batch.get('scope') or {}
    if (bounds.get('layout_id') != scope['layout_id'] or
            (bounds.get('frame_id') and bounds['frame_id'] != scope['frame_id']) or
            not contains(scope['bbox'], bounds.get('bbox'))):
        raise ValueError('SCOPE_COLLECTION：测量集合不属于此要求已选定平面；请读取对应范围的集合。')
    for item in batch.get('items', []):
        highlight = item.get('highlight') or {}
        if highlight.get('kind') != 'candidate_boundary':
            continue
        rings = [highlight.get('boundary', [])] + highlight.get('holes', [])
        if any(not ring or any(len(p) < 2 or not contains(scope['bbox'], [*p[:2], *p[:2]])
                               for p in ring) for ring in rings):
            raise ValueError('SCOPE_COLLECTION_GEOMETRY：集合中的候选轮廓超出选定平面或缺少可验证坐标。')


def record_scope_call(session, name, args, result):
    # Text retrieval can discover a target after scope resolution. It is not
    # constrained like a geometry tool; only matching native labels are linked.
    if name == 'query_entities':
        try:
            scope = selected_for_call(session, 'inspect_scope_objects')
        except ValueError:
            return  # Retrieval remains usable while a scope is unresolved.
    else:
        scope = selected_for_call(session, name)
    if scope is None or result.get('error'):
        return
    if name == 'query_entities':
        from cad_agent.analysis.scope_objects import target_windows, call
        windows = target_windows(session, scope, result.get('items', []))
        result['target_window_calls'] = [call(scope, w['window_id']) for w in windows[:4]]
        result['target_window_count'] = len(windows)
        result['target_window_identity'] = 'native_label_only_requires_geometry_and_visual_review'
        queue_scope_calls(session, scope, result['target_window_calls'])
    if name == 'inspect_scope_objects':
        # Prefer actual source/visual investigation to endlessly paging generic
        # candidates. Subdivision is the prerequisite for dense local windows.
        calls = result.get('next_calls', []) if result.get('subdivision_windows') else (
            result.get('review_calls', [])[:2] + result.get('bind_next_calls', [])[:1] + result.get('next_calls', [])[:2])
        queue_scope_calls(session, scope, calls)
    if name == 'render_region':
        visible = {c['entity_id'] for c in result.get('candidates', [])}
        calls = []
        for review in scope.get('object_workflow', {}).get('window_reviews', {}).values():
            for call in review['measurement_calls']:
                if set(call['arguments']['entity_ids']) <= visible:
                    calls.append(deepcopy(call))
        result['measurement_candidates'] = calls[:4]
        result['identity_review_required'] = True
        # These calls quantify candidate source paths, never confirm labels.
        queue_scope_calls(session, scope, calls[:2])
    if name == 'read_dimensions' or (name == 'get_component_evidence' and args.get('view') == 'dimensions'):
        kept, excluded = [], []
        for item in result.get('items', []):
            points = item.get('definition_points', [])
            valid = (item.get('layout_id') == scope['layout_id'] and contains(scope['bbox'], item.get('bbox'))
                     and all(contains(scope['bbox'], [*p[:2], *p[:2]]) for p in points))
            if valid:
                kept.append(item)
            else:
                excluded.append(dict(item, scope_exclusion_reason='extent_or_control_points_outside_selected_scope_or_unknown'))
        result['items'] = kept
        result['excluded_scope_items'] = excluded
        result['scope_filtered_total'] = len(kept)
        result['total_basis'] = 'component_candidates_before_page_scope_filter'
        result['scope_filter_coverage'] = 'current_page_only'
        for receipt in result.get('operation_receipts', []):
            if receipt.get('operation') in ('dimensions', 'geometry') and not kept:
                receipt['fulfilled'] = False
    if name == 'check_dimension_chain':
        candidates=result.get('candidate_dimensions',[])
        kept=[d for d in candidates if d.get('layout_id') == scope['layout_id'] and
              contains(scope['bbox'],d.get('bbox')) and all(contains(scope['bbox'],[*p[:2],*p[:2]]) for p in d.get('definition_points',[]))]
        if candidates:
            result['candidate_dimensions']=kept
            result['excluded_scope_dimensions']=[dict(d,scope_exclusion_reason='extent_or_control_points_outside_selected_scope_or_unknown') for d in candidates if d not in kept]
            result['scope_filter_coverage']='returned_candidates_only'
        chain = result.get('parts', []) + ([result['total']] if isinstance(result.get('total'), dict) else [])
        if any(not contains(scope['bbox'], item.get('bbox')) or any(
               not contains(scope['bbox'], [*p[:2], *p[:2]]) for p in item.get('definition_points', [])) for item in chain):
            session.highlight_action = None
            session.task.data.pop('highlight_action', None)
            raise ValueError('SCOPE_DIMENSION_CHAIN：尺寸链控制点超出选定平面；请核对或显式重新解析范围。')
    result['scope_binding'] = dict(scope_id=scope['scope_id'], snapshot_id=scope['snapshot_id'],
        source_sha256=scope['source_sha256'], layout_id=scope['layout_id'], frame_id=scope['frame_id'],
        task_ids=owners(session), tool=name, arguments=deepcopy(args))


def queue_scope_calls(session, scope, calls):
    queued = session.task.data.setdefault('scope_next_calls', [])
    ids = owners(session)
    for item in reversed(calls):
        call = deepcopy(item)
        call.update(scope_id=scope['scope_id'], task_ids=list(ids), operation='scope_measurement_next')
        call['call_id'] = stable_id('scope_call', scope['scope_id'], call['tool'], str(call['arguments']))
        previous = next((c for c in queued if c['call_id'] == call['call_id']), None)
        if previous:
            previous['task_ids'] = sorted(set(previous['task_ids']) | set(ids))
        else:
            # Latest concrete target/review steps precede old catalog pages.
            queued.insert(0, call)


def record_scope_failure(session, name, args, result):
    """Return bounded recovery after a render failure without relaxing guards.

    Called by the tool execution error path. An error remains an error and
    receives neither an observation nor a fulfilled operation receipt.
    """
    if name != 'render_region' or not result.get('error'):
        return
    message = result['error'].get('message', '')
    if not any(token in message for token in ('SCOPE_BOUNDS', '局部图像为空', '图元过多', '局部路径过于复杂', '图像超过传输上限', '局部范围过于狭长')):
        return
    try:
        scope = selected_for_call(session, name)
    except (ValueError, KeyError):
        return
    if scope is None or args.get('layout_id') != scope['layout_id']:
        return
    from cad_agent.analysis.scope_objects import tiles, call
    flow = scope.get('object_workflow', {})
    bounds = args.get('bbox', scope['bbox'])
    # Exact intersection fixes rounded-outward bounds; it is a new explicit
    # search request, never a silently clipped measurement or object boundary.
    crop = [max(bounds[0], scope['bbox'][0]), max(bounds[1], scope['bbox'][1]),
            min(bounds[2], scope['bbox'][2]), min(bounds[3], scope['bbox'][3])]
    if crop[0] >= crop[2] or crop[1] >= crop[3]:
        return
    windows = tiles(scope, crop, n=2, depth=1, parent='failed_render')
    # Prefer windows with observed source linework; an empty child is not an
    # object-absence finding. This avoids repeating an empty overview crop.
    lines = [l for l in session.index.scene['lines'] if l['layout_id'] == scope['layout_id']]
    def hits(w):
        a,b,c,d = w['bbox']
        return sum(any(a <= p[0] <= c and b <= p[1] <= d for p in line['points']) for line in lines)
    windows.sort(key=lambda w: -hits(w))
    known = {w['window_id'] for w in flow.get('windows', [])}
    flow.setdefault('windows', []).extend(w for w in windows if w['window_id'] not in known)
    calls = [call(scope, w['window_id']) for w in windows[:2]]
    result['recovery'] = dict(reason='render_failed_scope_subdivision', scope_id=scope['scope_id'],
        failed_bbox=bounds, search_bbox=crop, identity_status='unconfirmed', next_calls=calls,
        note='保留原错误；先在缩小窗口核查源实体，再按返回的局部图调用重试。未确认外轮廓或完成测量。')
    queue_scope_calls(session, scope, calls)


def pending_scope_calls(data):
    pending = []
    active = data.get('active_scope_by_task', {})
    states = {s['id']:s['status'] for s in data['subtasks']}
    observations = data.get('observations', {}).values()
    for call in data.get('scope_next_calls', []):
        ids = [i for i in call['task_ids'] if active.get(i) == call['scope_id'] and states.get(i) in ('pending', 'running')]
        if not ids:
            continue
        remaining = []
        for owner in ids:
            done = any(o.get('tool') == call['tool'] and
                (binding := o.get('data', {}).get('scope_binding', {})).get('scope_id') == call['scope_id'] and
                owner in binding.get('task_ids', []) and
                all(binding.get('arguments', {}).get(k) == v for k, v in call['arguments'].items())
                for o in observations)
            if not done:
                remaining.append(owner)
        if remaining:
            pending.append(deepcopy(call) | {'task_ids': remaining, 'blocking': False})
    return pending


def compact_scope(scope):
    """Model-visible identity; complete anchors/windows stay in task storage."""
    if scope is None:return None
    result={k:deepcopy(scope[k]) for k in ('scope_id','snapshot_id','source_sha256','frame_id','layout_id',
        'name','bbox','title_match','boundary_role','identity_state','view_kind',
        'datum_status','role_status','view_equivalence_status') if k in scope}
    titles=scope.get('title_evidence',[])
    result.update(title_evidence=deepcopy(titles[:3]),title_evidence_count=len(titles),title_evidence_truncated=len(titles)>3)
    flow=scope.get('object_workflow',{})
    if flow:
        result['object_workflow']=dict(version=flow['version'],unit=flow['unit'],
            anchor_count=len(flow['anchors']),subframe_count=len(flow['subframes']),window_count=len(flow['windows']),
            rejected_anchor_count=len(flow['rejected_anchors']),subframe_extent_status=flow['subframe_extent_status'],
            geometry_status=flow['geometry_status'],coverage=flow['coverage'],
            catalog_call=dict(tool='inspect_scope_objects',arguments=dict(scope_id=scope['scope_id'],offset=0,limit=5)))
    return result
