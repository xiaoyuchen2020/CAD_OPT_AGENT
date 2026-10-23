"""Bounded recovery advice on the existing task ledger, never a tool executor.

Only explicit tool diagnostics create gaps. Reading a header can close an unread
header gap; it cannot establish header applicability or engineering identity.
"""
from copy import deepcopy
from hashlib import sha256
import json

ATTEMPT_LIMIT = 3
RULES = {
    'assembly_ownership': ('做法归属未确认；核对表格或局部原图，不继续分页无界邻近文字。', ['read_table_semantics', 'render_region']),
    'header_unread': ('读取目标表的表头原文和对应列，不能按首行猜表头。', ['read_table_semantics']),
    'unit_missing': ('补读该数值直接绑定的单位来源；未知单位不换算、不自动确认。', ['read_source_context', 'read_dimensions']),
    'visual_identity': ('先核对原生来源，再看同对象局部图；视觉候选不是工程确认。', ['read_geometry', 'render_region']),
    'single_side_connection': ('补读同一开口的另一侧关系；单侧邻近不能证明双侧通行。', ['get_component_evidence', 'render_region']),
    'calculation_inputs': ('复用已有计算输入清单，补齐来源后重新计算，不重放旧操作数。', ['calculate_facts']),
    'no_new_evidence': ('读取已命中而未读的来源；没有新的已知入口时停止换词重试。', ['read_source_context']),
}
REFS = ('table_id', 'cell_id', 'region_id', 'batch_id', 'object_id', 'record_id',
        'calculation_key', 'layout_id', 'bbox', 'entity_ids', 'snapshot_id')


def _key(tool, args):
    return sha256(json.dumps([tool, args], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def _call_key(tool, args):
    from cad_agent.analysis.tool_registry import TOOL_SPECS
    try:
        args = TOOL_SPECS[tool][0].model_validate(args, strict=True).model_dump()
    except (ValueError, KeyError):
        pass
    return _key(tool, args)


def _walk(value, inherited=None):
    """Bounded structural diagnostics, never keyword-match quoted source text."""
    inherited = inherited or {}
    if isinstance(value, dict):
        refs = inherited | {k: deepcopy(value[k]) for k in REFS if value.get(k) is not None}
        yield value, refs
        for key in ('items', 'cells', 'unit_resolution', 'measurement'):
            child = value.get(key)
            if isinstance(child, (list, dict)):
                yield from _walk(child, refs)
    elif isinstance(value, list):
        for item in value[:128]:
            yield from _walk(item, inherited)


def _register(data, ids, kind, refs):
    gaps = data.setdefault('gap_recovery', {})
    # Object/table/source identity isolates unrelated successes and attempts.
    identity = {k: refs[k] for k in REFS if k in refs}
    key = _key(kind, [sorted(ids), identity])
    if key not in gaps and len(gaps) < 64:
        gaps[key] = dict(gap_id=key, kind=kind, task_ids=list(ids), refs=identity,
                         status='open', attempts=[], recommendation=None,
                         recommendations=[],
                         resolution=None, stop_reason=None, attempt_limit=ATTEMPT_LIMIT,
                         evidence_at_detection=sorted(data.get('evidence', {})) if kind == 'no_new_evidence' else [])
    return gaps.get(key)


def _same_subject(gap, refs):
    if gap['refs'].get('snapshot_id') and refs.get('snapshot_id') != gap['refs']['snapshot_id']:
        return False
    keys = ('object_id', 'cell_id', 'table_id', 'calculation_key', 'batch_id', 'region_id', 'record_id')
    subject = next((k for k in keys if k in gap['refs']), None)
    return bool(subject) and refs.get(subject) == gap['refs'][subject]


def record_result(session, name, ids, result):
    """Record attempts and observed diagnostics. Safe to call for tool errors too."""
    data = session.task.data
    trace = getattr(session, 'trace', [])
    entry = trace[-1] if trace and trace[-1].get('tool') == name else {}
    args = {k: v for k, v in entry.get('arguments', {}).items() if k != 'task_ids'}
    fingerprint = _call_key(name, args)
    gaps = data.get('gap_recovery', {})
    for gap in gaps.values():
        rec = gap.get('recommendation') or {}
        if (gap['status'] == 'open' and set(ids) & set(gap['task_ids']) and
                rec.get('tool') == name and
                ((rec.get('arguments_required') and all(args.get(k) == v for k, v in rec.get('arguments', {}).items())) or
                 rec.get('call_key') == fingerprint)):
            if not any(a['call_key'] == fingerprint for a in gap['attempts']):
                gap['attempts'].append(dict(tool=name, call_key=fingerprint,
                    outcome='tool_error' if result.get('error') else 'observed_not_resolved',
                    observation_id=result.get('observation_id')))
    if not result.get('error'):
        from cad_agent.analysis.efficiency import evidence_ids
        new_ids = evidence_ids(result)
        for gap in gaps.values():
            if gap['kind'] == 'no_new_evidence' and gap['status'] == 'open' and set(ids) & set(gap['task_ids']):
                added = new_ids - set(gap.get('evidence_at_detection', []))
                progress = [s.get('last_progress', {}) for s in data['subtasks'] if s['id'] in ids]
                meaningful = data.get('execution_policy_version') != 'cad-execution-v2' or any(p.get('kind') in ('verified_progress', 'candidate_progress') for p in progress)
                if added and meaningful:
                    gap.update(status='resolved', stop_reason=None, recommendation=None,
                               resolution=dict(tool=name, evidence_ids=sorted(added),
                                               basis='new_source_evidence_only'))
        base = {k: deepcopy(args[k]) for k in REFS if k in args}
        if name == 'read_assembly' and result.get('view') == 'record' and (result.get('source_binding') or {}).get('state') != 'source_bound':
            title = result.get('title_evidence') or {}
            sources = [i.get('evidence', {}) for i in result.get('items', [])] + [title]
            boxes = [e['bbox'] for e in sources if e.get('bbox')]
            refs = base | {'snapshot_id': result.get('snapshot_id')}
            if boxes:
                refs['bbox'] = [min(b[0] for b in boxes)-500, min(b[1] for b in boxes)-500,
                                max(b[2] for b in boxes)+500, max(b[3] for b in boxes)+500]
                refs['layout_id'] = title.get('layout_id') or session.index.by_id[args['region_id']].layout_id
            tables = result.get('table_candidates', [])
            if tables:
                refs['table_id'] = tables[0]['table_id']
            _register(data, ids, 'assembly_ownership', refs)
        for row, refs in _walk(result, base):
            kinds = []
            if row.get('header_state') == 'unresolved' or 'HEADER_UNRESOLVED' in row.get('diagnostics', []):
                kinds.append('header_unread')
            if row.get('unit_state') in ('unknown', 'conflict') or row.get('status') == 'needs_unit_confirmation':
                kinds.append('unit_missing')
            if name in ('resolve_room_units', 'measure_geometry', 'read_dimensions') and row.get('unit') == 'unknown':
                kinds.append('unit_missing')
            if isinstance(row.get('identity'), dict) and row['identity'].get('engineering_confirmed') is False:
                kinds.append('visual_identity')
                refs = refs | {k: row['geometry'][k] for k in ('entity_ids', 'bbox') if k in row.get('geometry', {})}
            if row.get('passage_status') == 'not_established' and (
                    row.get('connection_status') in ('one_space_candidate', 'single_space_candidate', 'one_side_only') or
                    'two_sided_space_connection_unresolved' in row.get('diagnostics', [])):
                kinds.append('single_side_connection')
            if name == 'calculate_facts' and row.get('missing_operands'):
                kinds.append('calculation_inputs')
            for kind in kinds:
                _register(data, ids, kind, refs)
            for gap in data.get('gap_recovery', {}).values():
                if gap['status'] != 'open' or not set(ids) & set(gap['task_ids']) or not _same_subject(gap, refs):
                    continue
                # Each receipt is deliberately narrow and tool-owned.
                resolved = (
                    gap['kind'] == 'header_unread' and name in ('read_table_semantics', 'read_door_schedule_table') and
                    bool(row.get('header_rows')) and any(c.get('is_header') and c.get('evidence')
                        for item in row.get('items', []) for c in item.get('cells', []))
                ) or (
                    gap['kind'] == 'unit_missing' and name in ('read_table_semantics', 'read_door_schedule_table') and
                    bool(refs.get('cell_id')) and row.get('unit_state') == 'derived' and
                    bool(row.get('unit')) and bool(row.get('unit_evidence'))
                ) or (
                    gap['kind'] == 'unit_missing' and name == 'resolve_room_units' and
                    row.get('status') == 'resolved' and row.get('unit') not in (None, 'unknown')
                ) or (
                    gap['kind'] == 'single_side_connection' and name in ('query_building_relations', 'get_component_evidence') and
                    row.get('passage_status') == 'supported_door_between_two_spaces'
                ) or (
                    gap['kind'] == 'calculation_inputs' and name == 'calculate_facts' and
                    row.get('status') == 'calculated' and not row.get('missing_operands') and
                    row.get('target_binding_status') == 'source_bound'
                )
                if resolved:
                    gap.update(status='resolved', stop_reason=None, recommendation=None,
                               resolution=dict(tool=name, observation_id=result.get('observation_id'),
                                               basis='explicit_tool_postcondition'))
        if getattr(getattr(session, 'policy', None), 'stalled', 0) >= 2:
            _register(data, ids, 'no_new_evidence', {})
    refresh(session)


def _proposals(session, gap):
    r = gap['refs']
    kind = gap['kind']
    cross_table = bool(r.get('table_id') and r.get('snapshot_id') and
                       r['snapshot_id'] != getattr(session.request, 'snapshot_id', r['snapshot_id']))
    table_tool = 'read_door_schedule_table' if cross_table else 'read_table_semantics'
    table_args = {k: r[k] for k in ('table_id',) if k in r} | {'row_offset': 0}
    if cross_table:
        table_args['snapshot_id'] = r['snapshot_id']
    if kind == 'header_unread':
        yield table_tool, table_args
    elif kind == 'assembly_ownership':
        if r.get('table_id'):
            yield table_tool, table_args
        if r.get('layout_id') and r.get('bbox'):
            yield 'render_region', {k: r[k] for k in ('layout_id', 'bbox')}
    elif kind == 'unit_missing':
        if cross_table:
            yield table_tool, table_args
        elif r.get('region_id'):
            yield 'read_source_context', dict(region_id=r['region_id'], neighbors=1)
        elif r.get('table_id'):
            yield 'read_table_semantics', dict(table_id=r['table_id'], row_offset=0)
        elif r.get('batch_id'):
            yield 'resolve_room_units', dict(batch_id=r['batch_id'])
        else:
            yield 'read_dimensions', {k: r[k] for k in ('layout_id', 'bbox') if k in r}
    elif kind == 'visual_identity':
        yield 'read_geometry', {'entity_ids': r['entity_ids'][:12]} if r.get('entity_ids') else {}
        yield 'render_region', {k: r[k] for k in ('layout_id', 'bbox') if k in r}
    elif kind == 'single_side_connection':
        yield 'get_component_evidence', {k: r[k] for k in ('record_id',) if k in r} | {'view': 'relations'}
        yield 'render_region', {k: r[k] for k in ('layout_id', 'bbox') if k in r}
    elif kind == 'calculation_inputs':
        for owner in gap['task_ids']:
            for call in session.task.data.get('calculation_inputs', {}).get(owner, {}).get('next_calls', []):
                yield call['tool'], {k: v for k, v in call['arguments'].items() if k != 'task_ids'}
        yield 'calculate_facts', {k: r[k] for k in ('calculation_key',) if k in r}
    elif kind == 'no_new_evidence':
        from cad_agent.analysis.recall import pending_calls
        for call in pending_calls(session.task.data):
            if call.get('tool') in ('read_source_context', 'read_assembly') and set(call['task_ids']) & set(gap['task_ids']):
                yield call['tool'], call['arguments']


def refresh(session):
    """Select advice using actual transport; never changes execution counters."""
    from cad_agent.analysis.scheduling import execution_state
    from cad_agent.analysis.tool_registry import TOOL_SPECS, runtime_supports, MUTATING_TOOLS
    seen = {_call_key(e['tool'], {k: v for k, v in e.get('arguments', {}).items() if k != 'task_ids'})
            for e in getattr(session, 'trace', []) if e.get('arguments') is not None}
    for gap in session.task.data.get('gap_recovery', {}).values():
        if gap['status'] == 'resolved':
            continue
        gap['recommendation'] = None
        states = {i: execution_state(session.task.data, i) for i in gap['task_ids']}
        owners = [i for i, state in states.items() if state['executable']]
        stop = next((s['blocked_reason'] for s in states.values() if not s['executable']), None) if not owners else None
        if len(gap['attempts']) >= ATTEMPT_LIMIT:
            stop = 'gap_attempt_limit'
        if getattr(getattr(session, 'policy', None), 'stop_reason', None):
            stop = session.policy.stop_reason
        if getattr(session, 'calls', 0) >= getattr(session.w.settings, 'agent_max_tool_calls', 100):
            stop = 'global_tool_budget'
        if stop:
            gap['stop_reason'] = stop
            continue
        unavailable = False
        repeated = False
        for tool, args in _proposals(session, gap):
            if tool not in TOOL_SPECS or not runtime_supports(tool, session.request.runtime, session.w.settings.provider):
                unavailable = True
                continue
            key = _call_key(tool, args)
            if key in seen or any(a['call_key'] == key for a in gap['attempts']):
                repeated = True
                continue
            schema = TOOL_SPECS[tool][0]
            try:
                schema.model_validate(args, strict=True)
                missing = []
            except ValueError as error:
                missing = ['.'.join(map(str, e['loc'])) or '<arguments>' for e in error.errors()]
            gap['recommendation'] = dict(tool=tool, tools=[tool], arguments=deepcopy(args),
                task_ids=owners, call_key=key, arguments_required=bool(missing),
                required_parameters=missing, schema_valid=not missing,
                execution='explicit_model_call', automatic_replay=False,
                changes_session_state=tool in MUTATING_TOOLS,
                reason=RULES[gap['kind']][0])
            history = gap.setdefault('recommendations', [])
            if not any(r['call_key'] == key for r in history) and len(history) < 8:
                history.append(deepcopy(gap['recommendation']))
            break
        gap['stop_reason'] = (None if gap['recommendation'] else
            'runtime_not_supported' if unavailable else 'no_untried_source_action' if repeated else 'no_known_source_action')


def pending_calls(data):
    return [dict(operation='gap_recovery', gap_id=g['gap_id'], gap_kind=g['kind'],
                 priority=2, **{k: deepcopy(g['recommendation'][k]) for k in
                     ('tool', 'arguments', 'task_ids', 'arguments_required', 'required_parameters')})
            for g in data.get('gap_recovery', {}).values()
            if g['status'] == 'open' and g.get('recommendation') and not g.get('stop_reason')]


def context(data):
    return [dict(gap_id=g['gap_id'], kind=g['kind'], task_ids=g['task_ids'],
                 status=g['status'], attempts=len(g['attempts']), attempt_limit=ATTEMPT_LIMIT,
                 stop_reason=g.get('stop_reason'), recommendation=deepcopy(g.get('recommendation')),
                 resolution=deepcopy(g.get('resolution')))
            for g in sorted(data.get('gap_recovery', {}).values(),
                            key=lambda g: g['status'] == 'resolved')[:24]]
