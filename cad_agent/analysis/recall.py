"""Explicit lexical alternatives and source-identity pagination receipts.

No vocabulary implies room function or synonym equivalence. Counts describe
parsed source matches; nearby labels never establish an object relationship.
"""
from copy import deepcopy
import json
from cad_agent.contracts import stable_id
from cad_agent.analysis.typed_requirements import is_source_fact, accepts_source_prerequisites

VERSION = 'source-recall-v1'

def _call_key(tool, args):
    from cad_agent.analysis.tool_registry import TOOL_SPECS
    values = {k:v for k,v in args.items() if k != 'task_ids'}
    try:
        values = TOOL_SPECS[tool][0].model_validate(values).model_dump()
    except (KeyError, ValueError):
        # Historical/incomplete feedback is not an executable continuation.
        return None
    return json.dumps([tool, values], sort_keys=True)


def terms(query='', keywords=None, alternatives=None):
    if len(alternatives or []) > 8:
        raise ValueError('最多提供8个显式替代查询词。')
    groups = [[query] + list(alternatives or [])] + [[k] for k in (keywords or [])]
    result = []
    for group in groups:
        cleaned = list(dict.fromkeys(t.strip() for t in group if t.strip()))
        if any(len(t) > 150 for t in cleaned):
            raise ValueError('查询词过长')
        if cleaned:
            result.append(cleaned)
    return result


def matched(text, groups, match='any'):
    folded = text.casefold()
    hits = [[t for t in group if t.casefold() in folded] for group in groups]
    accepted = not groups or (all(hits) if match == 'all' else any(hits))
    return accepted, list(dict.fromkeys(t for group in hits for t in group))


def page(index, result, tool, arguments, *, pool_complete=True):
    """Attach query/snapshot identity and truthful scoped coverage to a page."""
    args = deepcopy(arguments)
    offset, limit = args.pop('offset', 0), args.pop('limit', 8)
    scope = {k: args.get(k) for k in ('layout_id','frame_id','section_id','bbox','layer','kind') if args.get(k)}
    identity = json.dumps([VERSION, index.snapshot, index.drawing['sha256'], tool, args],
                          ensure_ascii=False, sort_keys=True)
    if result.get('ranking_context'):identity += result['ranking_context']
    query_id = stable_id('recall', identity)
    from cad_agent.analysis.retrieval import source_context_path
    for item in result.get('items', []):
        region = index.by_id.get(item.get('region_id'))
        if region is not None:
            item['source_context_path'] = source_context_path(index, region)
    result['query_id'] = query_id
    result['snapshot_id'] = index.snapshot
    result['source_sha256'] = index.drawing['sha256']
    result['total_basis'] = 'matched_source_entities' if tool == 'query_entities' else 'source_region_candidates'
    result['engineering_room_count'] = None
    result['next_offset'] = offset + len(result['items']) if result.get('has_more') else None
    result['coverage'] = dict(scope=scope, candidate_pool_complete=pool_complete,
        enumeration_state='unread_pages' if result.get('has_more') or offset else 'page_returned',
        source_read_state='unread', match_state='matched' if result['total'] else 'not_matched_in_checked_scope',
        drawing_absence_proven=False, outside_scope_state='not_checked' if scope else 'not_applicable',
        external_material_state='not_checked',
        parse_status=index.drawing.get('parse_status', index.drawing.get('status','unknown')))
    outside = [f for f in index.drawing.get('frames',[]) if
        (args.get('layout_id') and f['layout_id'] != args['layout_id']) or
        (args.get('frame_id') and f['frame_id'] != args['frame_id'])]
    result['coverage']['unchecked_frame_count'] = len(outside)
    result['coverage']['unchecked_frame_samples'] = [dict(frame_id=f['frame_id'],layout_id=f['layout_id']) for f in outside[:8]]
    if outside:
        result['coverage']['scope_navigation_call'] = dict(tool='get_drawing_overview',arguments=dict(view='frames',offset=0,limit=8))
    result['query_variants'] = terms(args.get('query',''),args.get('keywords'),args.get('alternatives'))
    if result.get('has_more'):
        result['next_call'] = dict(tool=tool, arguments=args | {'offset':result['next_offset'],'limit':limit})
        result['next_call']['arguments'].pop('mode', None)
    result['coverage_note'] = ('未核查范围、候选未读、已查未命中、关联资料未提供是不同状态；'
        '本结果只核对当前快照内的显式查询。命中数不是实际房间数，替代词不确认功能，邻近文字不自动归属。')
    return result


def _missing(positions, total):
    cursor = 0
    for position in sorted(set(positions)):
        if position > cursor:break
        if position == cursor:cursor += 1
    return cursor if cursor < total else None


def unread_spans(candidate, ledger):
    spans = candidate['source_spans'] or [dict(region_id=rid,text_span=[candidate['start'],candidate['start']+1]) for rid in candidate['region_ids']]
    pending = []
    for span in spans:
        start,end = span['text_span']
        for a,b in sorted(ledger['read_spans'].get(span['region_id'],[])):
            if a <= start < b: start = b
        if start < end:
            pending.append(dict(region_id=span['region_id'],start=start))
    return pending


def record(session, tool, args, result):
    """Record returned pages and actually read source spans in the task ledger."""
    if not isinstance(result, dict) or result.get('error'):
        return {}
    data = session.task.data
    context = dict(version=VERSION, snapshot_id=session.request.snapshot_id,
                   source_sha256=session.drawing['sha256'])
    current = data.get('source_recall', {})
    if current and current.get('context') != context:
        current = {}
    source_reads = {'read_source_context', 'read_assembly', 'read_table_semantics', 'read_project_sources', 'get_evidence', 'read_requirement_sources'}
    if tool not in ({'search_sources', 'query_entities'} | source_reads) and not current:
        return {}
    if not current and not result.get('query_id') and tool not in source_reads:
        return {}
    ledger = deepcopy(current) if current else dict(context=context, queries={}, read_spans={})
    query = None
    from cad_agent.analysis.scope_workflow import owners
    task_ids = owners(session)
    registry = getattr(session, 'evidence', data.get('evidence', {}))
    if tool in ('search_sources','query_entities') and result.get('query_id'):
        if result.get('snapshot_id') != context['snapshot_id'] or result.get('source_sha256') != context['source_sha256']:
            raise ValueError('Recall page belongs to another snapshot or source.')
        request = {k:deepcopy(v) for k,v in args.items() if k not in ('offset','task_ids')}
        query = ledger['queries'].setdefault(result['query_id'], dict(query_id=result['query_id'],
            tool=tool, arguments=request, task_ids=[], positions=[], candidates={}, total=result['total'],
            coverage=deepcopy(result['coverage'])))
        query['task_ids'] = sorted(set(query['task_ids']) | set(owners(session)))
        query['positions'] = sorted(set(query['positions']) | set(range(result['offset'],result['offset']+len(result['items']))))
        for item in result['items']:
            anchor = item.get('entity_id') or item.get('region_id')
            if not anchor:
                continue
            # Distinct same-name entities are retained; repeat pages are idempotent.
            query['candidates'][anchor] = dict(source_id=anchor,
                region_ids=item.get('source_region_ids', [item['region_id']] if item.get('region_id') else []),
                start=item.get('snippet_start',0), matched_terms=item.get('matched_terms',[]),
                source_preview=(item.get('snippet') or item.get('text') or '')[:900],
                assembly_heading=item.get('assembly_heading'),
                source_context_path=deepcopy(item.get('source_context_path', [])),
                source_spans=deepcopy(item.get('source_spans') or ([dict(region_id=item['region_id'],
                    text_span=[item.get('snippet_start',0),item.get('snippet_start',0)+len(item.get('snippet',''))])]
                    if item.get('region_id') and item.get('snippet') else [])),
                source_locations=deepcopy(item.get('source_locations',[])),
                layout_id=item.get('layout_id'), frame_id=item.get('frame_id'),
                location_state='source_coordinates_only' if item.get('bbox') else 'source_region_only',
                floor_state='not_checked', axis_state='not_checked', identity_state='candidate')
    if tool=='read_assembly':
        ledger.setdefault('assembly_reads', {})[args['region_id']] = True
    read_ids = set()
    def visit(value):
        if isinstance(value, dict):
            if value.get('evidence_id') and value.get('region_id') and value.get('text_span') and value.get('snapshot_id',context['snapshot_id']) == context['snapshot_id']:
                registered = registry.get(value['evidence_id'], {})
                if not registered.get('quote') or not all(value.get(k) == registered.get(k)
                        for k in ('snapshot_id', 'region_id', 'text_span', 'quote')):
                    return
                read_ids.add(value['evidence_id'])
                spans = ledger['read_spans'].setdefault(value['region_id'], [])
                if value['text_span'] not in spans:
                    spans.append(value['text_span'])
                if getattr(session, 'index', None) is not None:
                    from cad_agent.analysis.retrieval import source_context_path
                    region = session.index.by_id.get(value['region_id'])
                    if region is not None:
                        records = ledger.setdefault('source_records', {})
                        old = records.get(value['evidence_id'], {})
                        records[value['evidence_id']] = dict(evidence_id=value['evidence_id'],
                            region_id=value['region_id'], text_span=deepcopy(value['text_span']),
                            quote=value['quote'], layout_id=region.layout_id, frame_id=region.frame_id,
                            task_ids=sorted(set(old.get('task_ids', [])) | set(task_ids)),
                            source_context_path=source_context_path(session.index, region),
                            attribution_state='unverified')
            else:
                for v in value.values(): visit(v)
        elif isinstance(value, list):
            for v in value: visit(v)
    visit(result)
    if tool == 'read_requirement_sources':
        data.setdefault('requirement_source_reads', {}).setdefault(result['requirement_id'], []).append(
            dict(query_id=result['query_id'], offset=result['offset'], has_more=result['has_more']))
    # Persist executable continuations rather than losing them in a tool reply.
    # Keep only source reads, never auto-execute operations or infer ownership.
    continuations = ledger.setdefault('continuations', {})
    executed = _call_key(tool, args)
    continuations.pop(executed, None)
    for call in result.get('next_actions', []):
        if call.get('tool') not in ('read_source_context', 'read_assembly', 'read_requirement_sources'):
            continue
        key = _call_key(call['tool'], call['arguments'])
        if key is None or key == executed:
            continue
        prior = continuations.get(key, {})
        continuations[key] = dict(call, task_ids=sorted(set(task_ids) | set(prior.get('task_ids', []))))
    # Retain recent executable cursors without growing the task ledger forever.
    while len(continuations) > 128:
        continuations.pop(next(iter(continuations)))
    for entry in ledger['queries'].values():
        for candidate in entry['candidates'].values():
            candidate['read_state'] = 'read' if candidate['region_ids'] and not unread_spans(candidate,ledger) else 'unread'
        entry['first_unread_offset'] = _missing(entry['positions'],entry['total'])
        entry['returned_candidate_count'] = len(entry['candidates'])
        entry['unread_source_candidate_count'] = sum(c['read_state']=='unread' for c in entry['candidates'].values())
        entry['coverage']['enumeration_state'] = 'pages_exhausted' if entry['first_unread_offset'] is None else 'unread_pages'
        entry['coverage']['source_read_state'] = ('no_candidates_to_read' if not entry['total'] else
            'unread' if entry['unread_source_candidate_count'] or entry['first_unread_offset'] is not None else 'candidate_spans_read')
        entry['coverage']['linked_materials_state'] = 'provided_not_checked' if getattr(session.request,'related_drawings',[]) else 'not_provided'
        entry['coverage']['attribution_state'] = 'not_verified'
    # Resolve ancestry against actual read spans, not previews returned by search.
    for source in ledger.get('source_records', {}).values():
        for parent in source['source_context_path']:
            parent['evidence_ids'] = sorted(e['evidence_id'] for e in registry.values()
                if e.get('snapshot_id') == context['snapshot_id'] and e.get('region_id') == parent['region_id'])
            parent['read_state'] = ('read' if not unread_spans(dict(source_spans=[parent],
                region_ids=[parent['region_id']], start=0), ledger) else 'unread')
    data['source_recall'] = ledger
    from cad_agent.analysis.source_sequence import refresh as refresh_sequences
    refresh_sequences(session)
    if query:
        return {'recall_receipt':{k:deepcopy(query[k]) for k in ('query_id','task_ids','total','returned_candidate_count',
            'unread_source_candidate_count','first_unread_offset','coverage')}}
    # The task coverage view carries the bounded facts and parent edges. Return
    # only a receipt here, rather than duplicating the entire inventory per read.
    return {'source_coverage_receipt': dict(read_evidence_ids=sorted(read_ids),
        source_record_count=len(ledger.get('source_records', {})),
        attribution_state='unverified')} if read_ids else {}


def pending_calls(data):
    """Optional continuations, bounded by the existing task/global tool budgets.

    Only an explicit query or a parser parent of an actual read, owned by a
    runnable requirement, queues work. Stopped requirements do not enumerate CAD.
    """
    from cad_agent.analysis.scheduling import execution_state
    ledger = data.get('source_recall', {})
    if ledger and (ledger.get('context', {}).get('snapshot_id') != data.get('snapshot_id') or ledger.get('context', {}).get('source_sha256') != data.get('source_sha256')):
        return []
    active = {s['id'] for s in data['subtasks'] if s['status'] in ('pending','running')
              and execution_state(data, s['id'])['executable'] and accepts_source_prerequisites(s)}
    from cad_agent.analysis.requirement_sources import pending as requirement_pending
    pending = requirement_pending(data)
    from cad_agent.analysis.source_sequence import pending as sequence_pending
    pending.extend(sequence_pending(data, active))
    for call in ledger.get('continuations', {}).values():
        ids = sorted(active & set(call['task_ids']))
        if not ids:continue
        args = call['arguments']
        if call['tool'] == 'read_source_context':
            region = args['region_id']
            start = args.get('start', 0)
            if any(a <= start < b for a,b in ledger['read_spans'].get(region, [])):
                continue
        pending.append(dict(call, task_ids=ids, operation='source_recall_next', blocking=False,
                            priority=15 if call['tool']=='read_requirement_sources' else 3, coverage_gaps=[], reason='source_read_continuation'))
    queries = list(ledger.get('queries', {}).values())
    # Propose a bounded literal search for each explicit unresolved pair. These
    # are task-owned lower bounds, not a generated domain checklist. Returned
    # query identities prevent repeatedly issuing an exhausted exact search.
    for step in data['subtasks']:
        if step['id'] not in active:continue
        owned = [q for q in queries if step['id'] in q['task_ids']]
        for cell in step.get('coverage', []):
            if not is_source_fact(cell, step):continue
            if cell['state'] in ('addressed','source_candidate'):continue
            obj, field = cell['object'], cell['field']
            exact = [q for q in owned if q['arguments'].get('query') == obj and
                     q['arguments'].get('keywords') == [field] and q['arguments'].get('match') == 'all']
            scope = next(({k:q['arguments'][k] for k in ('layout_id','frame_id','section_id')
                           if q['arguments'].get(k)} for q in reversed(owned)
                          if _related(q['arguments'].get('query', ''), obj)), {})
            object_queries = [q for q in owned if q['arguments'].get('query') == obj
                              and not q['arguments'].get('keywords')]
            object_read = any(step['id'] in record.get('task_ids', []) and obj in record.get('quote', '')
                              for record in ledger.get('source_records', {}).values())
            if not object_queries and not object_read:
                args = dict(query=obj,limit=3,**scope)
            elif not exact:
                args = dict(query=obj,keywords=[field],match='all',limit=3,**scope)
            else:continue
            pending.append(dict(operation='source_recall_next',tool='search_sources',arguments=args,
                task_ids=[step['id']],blocking=not object_read,priority=20,reason='explicit_object_field_gap',object=obj,field=field,
                coverage_gaps=[dict(requirement_id=step['id'],object=obj,field=field,
                                   gap_kind=cell.get('gap_kind','unread_source'))]))
    # A parsed parent belongs to the actual source record, not to every nearby
    # clause. Restore it before widening a search; never infer object ownership.
    for source in ledger.get('source_records', {}).values():
        ids = sorted(active & set(source.get('task_ids', [])))
        if not ids:continue
        for parent in source.get('source_context_path', []):
            unread = unread_spans(dict(source_spans=[parent],region_ids=[parent['region_id']],start=0),ledger)
            if not unread:continue
            pending.append(dict(operation='source_recall_next',tool='read_source_context',
                arguments=unread[0] | {'neighbors':0},task_ids=ids,blocking=False,priority=5,
                coverage_gaps=[],reason='read_source_parent_context',source_evidence_id=source['evidence_id']))
    for query in ledger.get('queries', {}).values():
        args = query['arguments']
        ids = sorted(active & set(query['task_ids']))
        if not ids or not (args.get('query') or args.get('keywords') or args.get('alternatives')):
            continue
        # A legacy automatic object/field query may already be persisted. Do
        # not paginate it after discovering that its field is an operation.
        ids = [sid for sid in ids if not any(
            not is_source_fact(c, s) and args.get('query') == c['object']
            and args.get('keywords') == [c['field']]
            for s in data['subtasks'] if s['id'] == sid for c in s.get('coverage', []))]
        if not ids:continue
        gaps = [dict(requirement_id=s['id'],object=c['object'],field=c['field'],
                     gap_kind=c.get('gap_kind','unread_source'))
                for s in data['subtasks'] if s['id'] in ids for c in s.get('coverage',[])
                if is_source_fact(c, s) and c['state'] not in ('addressed','source_candidate')]
        # Explicit targets already have reviewable sources. Do not spend the
        # remaining budget enumerating every irrelevant hit of a broad query.
        targeted = any(any(is_source_fact(c, s) for c in s.get('coverage', []))
                       for s in data['subtasks'] if s['id'] in ids)
        if targeted and not gaps:continue
        words = [args.get('query',''), *args.get('keywords',[]), *args.get('alternatives',[])]
        query_gaps = [g for g in gaps if any(_related(word, g['object']) for word in words)]
        for candidate in query['candidates'].values():
            # A field-only search can already have returned a relevant object.
            # Read that bounded hit, but do not enumerate its unrelated pages.
            candidate_gaps = [g for g in gaps if g in query_gaps or
                g['object'] in candidate.get('source_preview', '') or
                any(_related(term, g['object']) for term in candidate.get('matched_terms', []))]
            if targeted and not candidate_gaps:continue
            if candidate.get('assembly_heading') and any(r in ledger.get('assembly_reads', {}) for r in candidate['region_ids']):continue
            if candidate['read_state'] == 'unread' and candidate['region_ids']:
                unread = unread_spans(candidate,ledger)
                if not unread: continue
                pending.append(dict(operation='source_recall_next',tool='read_assembly' if candidate.get('assembly_heading') else 'read_source_context',
                    arguments=dict(region_id=unread[0]['region_id'],view='record',offset=0,limit=5) if candidate.get('assembly_heading') else unread[0] | {'neighbors':0},
                    task_ids=sorted({g['requirement_id'] for g in candidate_gaps}) if candidate_gaps else ids,
                    query_id=query['query_id'],blocking=False,
                    priority=1,coverage_gaps=candidate_gaps,
                    source_preview=candidate.get('source_preview', '')[:500],
                    matched_terms=candidate.get('matched_terms', []),
                    reason='read_returned_source_before_more_candidates'))
        offset = query['first_unread_offset']
        if offset is not None and (not targeted or query_gaps):
            pending.append(dict(operation='source_recall_next',tool=query['tool'],
                arguments=args | {'offset':offset},
                task_ids=sorted({g['requirement_id'] for g in query_gaps}) if query_gaps else ids,
                query_id=query['query_id'],blocking=False,
                priority=30,coverage_gaps=query_gaps,reason='unread_candidate_page'))
    # Several query variants can point to the same source span.
    unique = {}
    for call in pending:
        key = json.dumps([call['tool'],call['arguments']],sort_keys=True)
        if key in unique:
            unique[key]['task_ids'] = sorted(set(unique[key]['task_ids']) | set(call['task_ids']))
            for gap in call.get('coverage_gaps',[]):
                if gap not in unique[key]['coverage_gaps']:unique[key]['coverage_gaps'].append(gap)
            unique[key]['blocking'] = unique[key]['blocking'] or call['blocking']
            unique[key]['priority'] = min(unique[key]['priority'],call['priority'])
        else:
            unique[key] = call
    # Rank already-returned candidates by their owning question, not insertion order.
    # The preview is a navigation hint only; it does not grant evidence credit.
    from cad_agent.analysis.retrieval import lexical_rank
    reads = [c for c in unique.values() if c.get('source_preview')]
    ranks = {}
    for step in data['subtasks']:
        owned = [c for c in reads if step['id'] in c['task_ids']]
        if not owned:continue
        for rank, (i, _) in enumerate(lexical_rank([{'index_text': c['source_preview'], 'text': c['source_preview'], 'chunk_id': str(i)} for i,c in enumerate(owned)], step['requirement'])):
            ranks[id(owned[i])] = min(rank, ranks.get(id(owned[i]), rank))
    return sorted(unique.values(),key=lambda c:(c['priority'],ranks.get(id(c),999)))[:12]


def _related(word, obj):
    # Literal containment is a retrieval hint, never a synonym/identity assertion.
    return bool(word and (obj in word or (len(word) >= 2 and word in obj)))


def coverage_feedback(data, task_ids=None, limit=12):
    """A bounded review inventory retaining source-to-parent edges and gaps."""
    ledger = data.get('source_recall', {})
    context = ledger.get('context', {})
    if context.get('snapshot_id') != data.get('snapshot_id') or context.get('source_sha256') != data.get('source_sha256'):
        return dict(source_records=[], total=0, truncated=False)
    selected = set(task_ids or [s['id'] for s in data.get('subtasks', [])])
    records = [r for r in ledger.get('source_records', {}).values() if selected & set(r.get('task_ids', []))]
    # No source becomes an accepted object/field pair merely because it contains
    # a requested term. Full records remain persisted when the view is bounded.
    rows = []
    for record in records[-limit:]:
        row = deepcopy(record)
        row['quote_truncated'] = len(row['quote']) > 500
        row['quote'] = row['quote'][:500]
        row['delivery_state'] = 'not_verified_by_source_read'
        rows.append(row)
    return dict(snapshot_id=context['snapshot_id'], source_sha256=context['source_sha256'],
        source_records=rows, total=len(records), truncated=len(records)>limit,
        note='逐条保留原文和其解析器父标题；父标题仍需语义复核。已读事实须交付或说明归属缺口，候选名称不证明范围、面积或数量，不能用附录摘录代替主表。')
