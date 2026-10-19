"""Source-bound candidate objects and resumable room workflow receipts.

This is an evidence index, not an engineering certification system. A successful
review is retained as a review assertion; identity and net measurement stay open.
"""
from copy import deepcopy
import json
from cad_agent.contracts import stable_id

VERSION = 'object-registry-v1'
ROOM_TOOLS = {'analyze_room_batch', 'measure_room', 'get_room_contours', 'discover_rooms', 'prepare_room_boundaries',
              'select_room_boundaries', 'resolve_room_units', 'prepare_discovery_review',
              'review_discovered_spaces', 'analyze_measurements', 'measure_room_area',
              'prepare_space_connections', 'review_space_connections'}


def _key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def context(session):
    # Import lazily: geometry services also expose these records in summaries.
    from cad_agent.geometry.source import GEOMETRY_VERSION
    from cad_agent.geometry.rooms import ROOM_VERSION, ROOM_VOID_VERSION
    from cad_agent.geometry.discovery import DISCOVERY_VERSION
    from cad_agent.geometry.tiled_discovery import VERSION as TILED_VERSION
    from cad_agent.geometry.room_batch import VERSION as BATCH
    from cad_agent.geometry.room_boundary import VERSION as BOUNDARY
    from cad_agent.geometry.discovery_review import VERSION as REVIEW
    from cad_agent.geometry.connection_review import VERSION as CONNECTION
    from cad_agent.geometry.contours import CONTOUR_VERSION
    return dict(schema=VERSION, snapshot_id=session.request.snapshot_id,
                source_sha256=session.drawing['sha256'], drawing_unit=session.drawing.get('unit', 'unknown'),
                unit_policy=deepcopy(session.task.data.get('unit_policy')),
                algorithms=[GEOMETRY_VERSION, ROOM_VERSION, BATCH, BOUNDARY, REVIEW, CONNECTION, CONTOUR_VERSION, ROOM_VOID_VERSION, DISCOVERY_VERSION, TILED_VERSION])


def restore_registry(session, parent_task_data):
    registry = parent_task_data.get('object_registry')
    if not registry or registry.get('context') != context(session):
        return False
    session.task.data['object_registry'] = deepcopy(registry)
    session.task.data['object_workflows'] = deepcopy(parent_task_data.get('object_workflows', {}))
    return True


def _ring(points):
    """Exact rotation/direction invariant geometry; no fuzzy identity merging."""
    p = [tuple(v) for v in points]
    if p and p[0] == p[-1]: p.pop()
    if not p: return []
    return min([p[n:]+p[:n] for n in range(len(p))] +
               [list(reversed(p[n:]+p[:n])) for n in range(len(p))])


def record_tool_result(session, tool_name, arguments, result):
    """Record one successful tool result; caller owns atomic task persistence.

    Return additive public fields. Replays are idempotent and interrupted work
    resumes from persisted page receipts, never from a last-page boolean.
    """
    if tool_name not in ROOM_TOOLS or not isinstance(result, dict): return {}
    args = arguments.model_dump() if hasattr(arguments, 'model_dump') else dict(arguments or {})
    ctx = context(session)
    if result.get('snapshot_id', ctx['snapshot_id']) != ctx['snapshot_id'] or result.get('source_sha256', ctx['source_sha256']) != ctx['source_sha256']:
        raise ValueError('Object evidence belongs to another snapshot or source.')
    previous = session.task.data.get('object_registry', {})
    reg = deepcopy(previous) if previous.get('context') == ctx else dict(context=ctx, objects={}, aliases={}, collections={}, events={})
    flows = deepcopy(session.task.data.get('object_workflows', {})) if previous.get('context') == ctx else {}
    data = result
    batch_id = result.get('batch_id') or args.get('batch_id')
    if batch_id and batch_id in getattr(session, 'room_batches', {}):
        data = session.room_batches[batch_id] | result
        data['items'] = session.room_batches[batch_id]['items']
    # Summary tools intentionally omit geometry; recover the immutable record only
    # for a record already returned to this session, without enumerating more pages.
    if batch_id and not data.get('scope') and hasattr(session, 'w'):
        try:
            stored = session.w.store.get('room_batch', batch_id)
            if stored['snapshot_id'] != ctx['snapshot_id'] or stored['source_sha256'] != ctx['source_sha256']: raise ValueError('Foreign batch.')
            data = stored | result
            data['items'] = stored['items']
        except KeyError: pass
    review_id = result.get('review_id')
    if review_id and hasattr(session, 'w') and not batch_id:
        try:
            stored = session.w.store.get('measurement', review_id)
            if stored['snapshot_id'] != ctx['snapshot_id'] or stored['source_sha256'] != ctx['source_sha256']: raise ValueError('Foreign review.')
            data = stored | result
            data['items'] = stored['items']
        except KeyError: pass
    if tool_name == 'discover_rooms' and hasattr(session, 'w'):
        try:
            stored = session.w.store.get('measurement', data['measurement_id'])
            if stored['snapshot_id'] != ctx['snapshot_id'] or stored['source_sha256'] != ctx['source_sha256']: raise ValueError('Foreign discovery.')
            by_id = {r['candidate_id']:r for r in stored['candidates']}
            data = dict(data, items=[by_id[r['candidate_id']] | r for r in data.get('items', [])])
        except KeyError: pass
    inherited_collection = None
    if tool_name in ('analyze_measurements', 'get_room_contours', 'measure_room_area'):
        requested_refs = {v for v in (args.get('review_id'), args.get('discovery_id'), args.get('batch_id'), data.get('discovery_id')) if v}
        matches = [c for c in reg['collections'].values() if requested_refs & set(c.get('record_refs', []) + [c['source_ref']])]
        if len(matches) == 1:
            inherited_collection = matches[0]
            data = dict(data)
            data.setdefault('scope', deepcopy(inherited_collection['scope']))
            data.setdefault('applied_unit', inherited_collection['unit'])
    scope = deepcopy(data.get('scope') or dict(layout_id=data.get('layout_id', args.get('layout_id')), bbox=data.get('scope_bbox', args.get('bbox'))))
    scope_key = _key({k:scope.get(k) for k in ('layout_id', 'frame_id', 'bbox')})
    unit = data.get('unit', data.get('applied_unit', ctx['drawing_unit']))
    rows = data.get('items', data.get('candidates', []))
    if tool_name == 'analyze_measurements': rows = data.get('values', [])
    if tool_name in ('get_room_contours', 'measure_room_area') and data.get('candidate_id'): rows = [data]
    if not rows and (data.get('region_id') or args.get('region_id')): rows = [dict(data, region_id=data.get('region_id', args.get('region_id')))]
    collection_ref = data.get('discovery_id') or args.get('discovery_id')
    if tool_name == 'discover_rooms': collection_ref = data.get('measurement_id')
    if inherited_collection: collection_ref = inherited_collection['source_ref']
    if not collection_ref:
        # All label pages share a collection even though each has its own batch id.
        request = data.get('request', {})
        collection_ref = stable_id('labels', scope_key, request.get('room_label', ''), unit) if batch_id else data.get('measurement_id', data.get('review_id', 'unknown'))
    existing = [c for c in reg['collections'].values() if c['source_ref'] == collection_ref and c['unit'] == unit]
    if len(existing) == 1 and not scope.get('bbox') and not scope.get('frame_id'):
        scope = deepcopy(existing[0]['scope'])
        scope_key = _key({k:scope.get(k) for k in ('layout_id','frame_id','bbox')})
    collection_id = stable_id('collection', _key(ctx), str(collection_ref), scope_key, unit)
    col = reg['collections'].setdefault(collection_id, dict(collection_id=collection_id, source_ref=collection_ref, scope=scope, unit=unit, object_ids=[], read_positions=[], reviewed_ids=[], pages=[], total_candidates=None, enumeration_exhaustive=False, kind='label_candidates' if batch_id else 'geometry_candidates'))
    record_refs = col.setdefault('record_refs', [])
    for reference in (data.get('review_id'), data.get('measurement_id'), batch_id):
        if reference and reference not in record_refs: record_refs.append(reference)
    refs = []
    for row_index, row in enumerate(rows):
        anchor = row.get('region_id') or row.get('candidate_id') or row.get('item_id') or row.get('object_id')
        if not anchor: continue
        if row.get('snapshot_id', ctx['snapshot_id']) != ctx['snapshot_id']: raise ValueError('Foreign object.')
        alias_key = _key([scope_key, anchor])
        oid = reg['aliases'].get(alias_key) or stable_id('object', ctx['snapshot_id'], ctx['source_sha256'], str(anchor))
        reg['aliases'][alias_key] = oid
        obj = reg['objects'].setdefault(oid, dict(object_id=oid, source_anchor=anchor, identity={'status':'candidate', 'engineering_confirmed':False}, scope={'status':'candidate', 'value':scope}, boundary={'status':'unknown'}, measurement_basis={'status':'unknown'}, names=[], dimensions=[], visual_reviews=[], provenance=[]))
        if obj['measurement_basis'].get('unit', unit) != unit:
            obj['measurement_basis'] = {'status':'unknown', 'invalidated_by':'unit_change'}
        scopes = obj.setdefault('scope_assertions', [])
        if scope not in scopes: scopes.append(deepcopy(scope))
        obj['object_kind'] = 'connection_candidate' if row.get('source_region_id') else 'space_candidate'
        if row.get('source_region_id'):
            obj['endpoints'] = dict(source_region_id=row['source_region_id'], target_region_id=row.get('target_region_id'))
        if row.get('text_regions'): obj['name_evidence'] = deepcopy(row['text_regions'])
        label = row.get('label')
        if label and label not in obj['names']: obj['names'].append(label)
        h = row.get('highlight', {})
        boundary = row.get('boundary') or h.get('boundary') or row.get('outer_contour', {}).get('boundary')
        if boundary:
            obj['boundary'] = dict(status='candidate', coordinates=deepcopy(boundary), holes=deepcopy(h.get('holes', [])), geometry_key=stable_id('shape', _key(_ring(boundary))), source_ref=data.get('review_id', batch_id))
        decision = row.get('decision') or row.get('model_choice')
        if not decision and tool_name == 'select_room_boundaries':
            decision = next((c for c in args.get('choices', []) if c.get('region_id') == anchor), None)
        if decision:
            review = dict(review_id=data.get('review_id'), decision=deepcopy(decision), image_ids=data.get('image_ids', [i['image_id'] for i in data.get('images', [])]))
            if review not in obj['visual_reviews']: obj['visual_reviews'].append(review)
            if oid not in col['reviewed_ids']: col['reviewed_ids'].append(oid)
            obj['identity']['review_classification'] = decision.get('classification', decision.get('decision'))
        value = row.get('candidate_area_m2')
        if value is None: value = row.get('basis_area_m2', row.get('area_m2'))
        if value is not None or row.get('measurement_ref'):
            obj['measurement_basis'] = dict(status='candidate_measurement', unit=unit, basis=row.get('area_basis', row.get('basis', 'selected_enclosure')), area_m2=value, source_ref=row.get('measurement_ref', row.get('measurement_id')), engineering_confirmed=False)
        dimension_evidence = row.get('dimensions', []) + row.get('selected_measurements', []) + [d for opening in row.get('openings', []) for d in opening.get('dimension_checks', [])]
        for dimension in dimension_evidence:
            if dimension not in obj['dimensions']: obj['dimensions'].append(deepcopy(dimension))
        provenance = dict(tool=tool_name, record_id=data.get('review_id', data.get('measurement_id', batch_id)), algorithm_version=data.get('algorithm_version', data.get('version')), unit=unit)
        if provenance not in obj['provenance']: obj['provenance'].append(provenance)
        if oid not in col['object_ids']: col['object_ids'].append(oid)
        if tool_name == 'discover_rooms':
            position = data.get('offset', args.get('offset',0)) + row_index + 1
            if position not in col['read_positions']: col['read_positions'].append(position)
        if isinstance(row.get('number'), int) and row['number'] > 0 and row['number'] not in col['read_positions']: col['read_positions'].append(row['number'])
        refs.append(dict(source_anchor=anchor, object_id=oid))
    total = data.get('total', data.get('total_candidates')) if tool_name not in ('get_room_contours', 'measure_room_area', 'analyze_measurements') else None
    if isinstance(total, int) and total >= 0: col['total_candidates'] = total
    offset = data.get('offset', data.get('request', {}).get('offset', args.get('offset', 0)))
    page = dict(offset=offset, object_ids=sorted({r['object_id'] for r in refs}), has_more=data.get('has_more'))
    if page not in col['pages']: col['pages'].append(page)
    total = col['total_candidates']
    positions_complete = total is not None and len(col['object_ids']) == total and set(range(1, total+1)) <= set(col['read_positions'])
    # A non-paginated complete discovery can use IDs; paginated results require
    # explicit positions, so receiving only the final page cannot prove coverage.
    full_discovery = tool_name == 'discover_rooms' and data.get('complete') is True and not data.get('has_more') and offset == 0 and total == len(col['object_ids'])
    col['enumeration_exhaustive'] = bool(positions_complete or full_discovery)
    col['coverage'] = dict(read_count=len(col['object_ids']), reviewed_count=len(col['reviewed_ids']), unread_count=None if total is None else max(0,total-len(col['object_ids'])), unreviewed_count=len(set(col['object_ids'])-set(col['reviewed_ids'])), enumeration_exhaustive=col['enumeration_exhaustive'], engineering_room_count=None)
    event_result = {k:v for k,v in result.items() if k not in ('object_registry','object_workflow','observation_id')}
    event_id = stable_id('step', tool_name, _key(args), _key(event_result))
    reg['events'][event_id] = dict(tool=tool_name, collection_id=collection_id, object_ids=sorted({r['object_id'] for r in refs}), status='completed')
    flow = flows.setdefault(collection_id, dict(workflow_id=collection_id, template='room_evidence_v1', steps=[], stages={}))
    if event_id not in flow['steps']: flow['steps'].append(event_id)
    objects = [reg['objects'][oid] for oid in col['object_ids']]
    flow['stages'] = dict(scope='candidate' if scope.get('bbox') or scope.get('frame_id') else 'unresolved', enumeration='exhausted_candidates' if col['enumeration_exhaustive'] else 'partial', identity_review='complete' if objects and not col['coverage']['unreviewed_count'] else 'pending', boundary='candidate' if objects and all(o['boundary']['status']=='candidate' for o in objects) else 'pending', measurement='candidate' if objects and all(o['measurement_basis']['status']=='candidate_measurement' for o in objects) else 'pending', engineering_acceptance='not_established')
    flow['next_steps'] = [stage for stage in ('scope','enumeration','identity_review','boundary','measurement') if flow['stages'][stage] in ('unresolved','partial','pending')]
    session.task.data['object_registry'] = reg
    session.task.data['object_workflows'] = flows
    public_flow = deepcopy(flow)
    public_flow['step_count'] = len(flow['steps'])
    public_flow['steps'] = flow['steps'][-20:]
    public_flow['steps_truncated'] = len(flow['steps']) > 20
    return dict(object_registry=dict(version=VERSION, collection_id=collection_id, objects=refs, coverage=deepcopy(col['coverage'])), object_workflow=public_flow)


def registry_summary(task_data):
    """Bounded public status for task/finish UI; never serializes source polygons."""
    reg = task_data.get('object_registry', {})
    collections = list(reg.get('collections', {}).values())
    return dict(version=VERSION, candidate_object_count=len(reg.get('objects', {})),
                engineering_confirmed_count=0, collection_count=len(collections),
                collections=[dict(collection_id=c['collection_id'], kind=c['kind'], lifecycle_status=c.get('lifecycle_status','active'),
                                  coverage=deepcopy(c['coverage'])) for c in collections[:20]],
                collections_truncated=len(collections)>20,
                workflow_count=len(task_data.get('object_workflows', {})))


def record_local_batch(workspace, task_data, batch):
    """Apply a validated UI continuation to the same candidate registry.

    Caller stores task_data with its normal CAS/lease check. No model, task
    creation, source writes, or observation fabrication occurs here.
    """
    from types import SimpleNamespace
    from cad_agent.geometry.room_batch import VERSION as BATCH_VERSION
    drawing = workspace.store.get('drawing', task_data['snapshot_id'])
    if (batch.get('snapshot_id') != task_data['snapshot_id'] or
            batch.get('source_sha256') != drawing['sha256'] or
            task_data.get('source_sha256') != drawing['sha256']):
        raise ValueError('本地批次与任务来源版本不一致。')
    if batch.get('version') != BATCH_VERSION:
        raise ValueError('本地批次算法版本已变化，请重新读取。')
    if batch['batch_id'] not in task_data.get('batches', {}):
        raise ValueError('本地批次尚未绑定到任务。')
    working = dict(task_data)
    proxy = SimpleNamespace(w=workspace, request=SimpleNamespace(snapshot_id=task_data['snapshot_id']),
                            drawing=drawing, task=SimpleNamespace(data=working),
                            room_batches={batch['batch_id']:batch})
    update = record_tool_result(proxy, 'analyze_room_batch', batch.get('request', {}), batch)
    reg = working['object_registry']
    current_id = update['object_registry']['collection_id']
    current = reg['collections'][current_id]
    current['lifecycle_status'] = 'active'
    # Old unit generations remain auditable but cannot advertise a current
    # measured workflow for objects that have just been recalculated.
    for cid, previous in reg['collections'].items():
        if cid == current_id: continue
        unit_changed = previous['unit'] != current['unit'] and bool(set(previous['object_ids']) & set(current['object_ids']))
        scope_changed = batch.get('parent_batch_id') in previous.get('record_refs', []) and previous['scope'] != current['scope']
        if unit_changed or scope_changed:
            previous.update(lifecycle_status='superseded', superseded_by=current_id)
            flow = working['object_workflows'].get(cid)
            if flow:
                flow.update(lifecycle_status='superseded', superseded_by=current_id)
                flow['stages']['measurement'] = 'invalidated_unit_change' if unit_changed else 'invalidated_scope_change'
    for key in ('object_registry', 'object_workflows'):
        task_data[key] = working[key]
    return update
