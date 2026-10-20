"""Explicit-selection source inventory; metadata never proves cross-file identity."""
import re


_CROSS_SOURCE = re.compile(r'跨图|跨文件|关联图|共同轴网|首层功能|上方是否|计算书|资料清单|另册|另一张图|另一份文件')
_INVENTORY_REQUEST = re.compile(r'(?:资料|文件).{0,16}(?:提供|清单|目录)|(?:提供|选定).{0,16}(?:资料|文件)')
_REFERENCE = re.compile(r'详见|参见|引用|按.{0,20}(?:说明|专篇|计算书)')
_INTERNAL = re.compile(r'(?:图内|本图|本文件|本说明)(?:的|中|内)?[^，。；\n]{0,24}(?:专篇|章节)|本节|第[一二三四五六七八九十百\d]+[章节篇]')
_EXTERNAL = re.compile(r'计算书|另册|独立文件|\.(?:pdf|dxf|dwg)\b', re.I)


def selected_ids(s):
    return [s.request.snapshot_id, *[d.snapshot_id for d in s.request.related_drawings]]


def relevant(s):
    return bool(s.request.related_drawings or _CROSS_SOURCE.search(s.request.question) or _INVENTORY_REQUEST.search(s.request.question) or any(
        _REFERENCE.search(e.get('quote', '')) for e in s.evidence.values()))


def source_state(s, sid):
    evidence = [e['evidence_id'] for e in s.evidence.values()
                if e.get('snapshot_id') == sid and e.get('evidence_id')]
    return dict(selection_state='selected',
        read_state='source_spans_read' if evidence else 'selected_not_read',
        read_evidence_count=len(evidence), whole_file_read=False,
        relationship_state='current_snapshot' if sid == s.request.snapshot_id else 'project_floor_revision_unconfirmed',
        cross_file_comparison_complete=False)


def inventory(s):
    metadata = {d.snapshot_id: d.model_dump() for d in s.request.related_drawings}
    return [dict(snapshot_id=sid, name=s.w.store.get('drawing', sid)['name'],
        sha256=s.w.store.get('drawing', sid)['sha256'], metadata=metadata.get(sid, {}),
        metadata_basis='user_labels_not_source_identity', **source_state(s, sid)) for sid in selected_ids(s)]


def early_context(s):
    if not relevant(s):
        return None
    seen = any(o.get('tool') == 'read_project_sources' and o.get('data', {}).get('action') == 'catalog'
               for o in s.observations.values())
    return dict(items=inventory(s), catalog_state='catalog_observed' if seen else 'selection_manifest_only',
        next_calls=[] if seen else [dict(tool='read_project_sources', arguments=dict(action='catalog'))],
        independent_current_file_work='continue',
        note='先核对本轮选定清单。文件名和用户项目/楼层/版本标签不证明对应关系。外部文件未选、已选未读、原文引用、版本未确认须分列；图内章节仍在当前图search/read，不因缺外部文件阻塞本图事实。此清单提示不算原文读取或比较完成。')


def catalog(s, args):
    allowed = selected_ids(s)
    if args.snapshot_id and args.snapshot_id not in allowed:
        raise ValueError('目标图纸未被用户加入本轮关联资料。')
    result = dict(action='catalog', items=inventory(s), independent_current_file_work='continue',
        operation_receipts=[], cross_file_comparison_complete=False,
        note='仅用户明确选定快照；未扫描未选文件。原文引用不证明文件可用；已读若干原文不证明整图读完；项目、楼层、轴网及版本关系需分别核对原图。')
    if not args.query:
        result['next_calls'] = []
        result['next_action'] = '按当前要求选择清单内snapshot_id，以具体题词search，再按返回region_id/start读取；图内章节留在当前文件。'
        return result
    reference = None
    if args.reference_evidence_id or args.reference_quote:
        e = s.evidence.get(args.reference_evidence_id)
        if not e or e.get('snapshot_id') not in allowed or not args.reference_quote or args.reference_quote not in e.get('quote', ''):
            raise ValueError('引用须绑定本轮已读、白名单内的原文及逐字片段。')
        reference = dict(reference_evidence_id=args.reference_evidence_id, source_quote=args.reference_quote,
            snapshot_id=e['snapshot_id'], state='source_reference_only', file_availability_proven=False)
    kind = args.reference_kind
    # Explicit same-document language cannot be converted into a missing file.
    text = args.reference_quote or args.query
    if _INTERNAL.search(text) and not _EXTERNAL.search(text):
        kind = 'internal_section'
    sid = args.snapshot_id
    if kind == 'internal_section':
        sid = reference['snapshot_id'] if reference else (sid or s.request.snapshot_id)
        state = 'internal_section_lookup_pending'
        next_calls = [dict(tool='read_project_sources', arguments=dict(action='search', snapshot_id=sid, query=args.query))]
    elif sid:
        state = source_state(s, sid)['read_state']
        next_calls = [dict(tool='read_project_sources', arguments=dict(action='search', snapshot_id=sid, query=args.query))]
    elif kind == 'external_file' and len(allowed) == 1:
        state = 'no_additional_file_selected'
        next_calls = [dict(tool='read_project_sources', arguments=dict(action='search', snapshot_id=s.request.snapshot_id, query=args.query))]
    else:
        state = 'selected_file_match_unconfirmed' if len(allowed) > 1 else 'reference_location_unconfirmed'
        # An unspecified chapter/reference can be located in the current file;
        # never equate a title or failed search with an absent external file.
        next_calls = [dict(tool='read_project_sources', arguments=dict(action='search', snapshot_id=s.request.snapshot_id, query=args.query))]
    result.update(requested_source=dict(query=args.query, reference_kind=kind, state=state,
        snapshot_id=sid or None, reference=reference, identity_confirmed=False,
        requested_file_availability='unconfirmed', current_file_match='unconfirmed',
        additional_file_selection_state='selected' if len(allowed)>1 else 'none_selected',
        current_file_content_absence_proven=False), next_calls=next_calls)
    return result


def next_reads(sid, result):
    calls = []
    for item in result.get('items', []):
        if item.get('region_id'):
            calls.append(dict(tool='read_project_sources', arguments=dict(action='read', snapshot_id=sid,
                region_id=item['region_id'], start=item.get('snippet_start', 0))))
    return calls


def run(s, args):
    if args.action == 'catalog':
        return catalog(s, args)
    if args.snapshot_id not in selected_ids(s):
        raise ValueError('目标图纸未被用户加入本轮关联资料。')
    if args.action == 'search':
        from cad_agent.analysis.general import DrawingIndex
        result = DrawingIndex(s.w, args.snapshot_id).search(query=args.query, offset=args.offset, limit=args.limit)
        calls = next_reads(args.snapshot_id, result)
        if result.get('has_more'):
            result['next_call'] = dict(tool='read_project_sources', arguments=dict(action='search',
                snapshot_id=args.snapshot_id, query=args.query, offset=result['next_offset'], limit=args.limit))
            calls.append(result['next_call'])
        result.update(action='search', source_status=source_state(s, args.snapshot_id), next_calls=calls,
            note='检索命中尚未读取；零命中不证明文件或章节不存在。search/read不确认跨文件项目、楼层或版本关系。')
        return result
    from cad_agent.analysis.evidence_ledger import source_context
    result = source_context(s.w, args.snapshot_id, args.region_id, start=args.start, neighbors=1, bounded=True)
    calls = [dict(tool='read_project_sources', arguments=dict(action='read', snapshot_id=args.snapshot_id,
        region_id=c['arguments']['region_id'], start=c['arguments'].get('start', 0)))
        for c in result.get('next_actions', []) if c['tool'] == 'read_source_context']
    state = dict(source_state(s, args.snapshot_id), read_state='source_spans_read')
    state['read_evidence_count'] = len({e['evidence_id'] for e in s.evidence.values()
        if e.get('snapshot_id') == args.snapshot_id and e.get('evidence_id')} |
        {i['evidence']['evidence_id'] for i in result['items']})
    result.update(action='read', source_status=state,
        next_calls=calls, next_actions=calls,
        note='仅所返原文片段已读取，不证明整文件完整或跨文件对应关系；未读分页按next_calls继续。邻近关系不证明适用范围；omitted_context_region_count非零时上下文仍不完整。')
    return result


def pending_calls(data):
    """One catalog check for an explicit unresolved source-inventory request.

    Metadata-only, optional, and subject to the same per-requirement stop gates.
    Internal chapter reads can continue independently of this catalog operation.
    """
    seen = any(o.get('tool') == 'read_project_sources' and o.get('data', {}).get('action') == 'catalog'
               for o in data.get('observations', {}).values())
    if seen:return []
    ids = [s['id'] for s in data.get('subtasks', [])
        if s['status'] in ('pending', 'running') and s.get('calls', 0) < 12 and s.get('stalled', 0) < 2
        and (_CROSS_SOURCE.search(s.get('requirement', '')) or _INVENTORY_REQUEST.search(s.get('requirement', '')))]
    if not ids:return []
    return [dict(operation='project_source_catalog', tool='read_project_sources',
        arguments=dict(action='catalog'), task_ids=ids, blocking=False, priority=8,
        reason='selected_material_inventory_not_checked',
        coverage_gaps=[dict(requirement_id=rid, gap_kind='selected_material_inventory_not_checked') for rid in ids])]
