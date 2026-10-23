"""One execution contract for task selection, exposure and delivery gates.

Budget exhaustion is a scheduling block, never evidence of missing capability or
completion. A dependency can block numerical synthesis without blocking unrelated
source reads. No counters or acceptance receipts are changed by this module.
"""
from cad_agent.analysis.tool_metadata import OPERATION_TOOLS

SUBTASK_CALL_LIMIT = 12
SUBTASK_PROGRESS_RESERVE = 4
REPETITION_LIMIT = 2
PLAN_REVISION_LIMIT = 2


def execution_state(data, task_id, visiting=None, *, source_read_limit=None):
    nodes={s['id']:s for s in data['subtasks']}
    step=nodes[task_id]
    result={'executable':True,'blocked_reason':None,'gap_kind':None}
    # A host-owned fixed source scan may enumerate more pages than a model
    # exploration subtask. Its transient cap stays below the session tool cap;
    # it grants no evidence/operation credit and is absent from persisted plans.
    fixed_read = type(source_read_limit) is int and step.get('calls',0)<source_read_limit
    if not fixed_read and step.get('calls',0)>=SUBTASK_CALL_LIMIT and not (
            step.get('progress_call', 0) >= step.get('calls', 0)-1 and
            step.get('progress_call', 0) > 0 and
            step.get('calls', 0) < SUBTASK_CALL_LIMIT + SUBTASK_PROGRESS_RESERVE):
        return result|{'executable':False,'blocked_reason':'subtask_tool_budget','gap_kind':'budget_exhausted'}
    if step.get('quality_progress', {}).get('stalled', 0) >= 3:
        return result|{'executable':False,'blocked_reason':'no_requirement_progress','gap_kind':'unread_source'}
    if step.get('stalled',0)>=REPETITION_LIMIT and not step.get('action_progress_tracking'):
        return result|{'executable':False,'blocked_reason':'repeated_no_progress','gap_kind':'budget_exhausted'}
    if step['status'] in ('blocked_capability','partial') and step.get('reason')=='subtask_tool_budget_or_repetition':
        return result|{'executable':False,'blocked_reason':'legacy_budget_or_repetition','gap_kind':'budget_exhausted'}
    if step['status']=='waiting_input':
        return result|{'executable':False,'blocked_reason':'waiting_input','gap_kind':'missing_input'}
    if step['status']=='blocked_capability':
        return result|{'executable':False,'blocked_reason':'unsupported_capability','gap_kind':step.get('gap_kind') or 'unsupported_capability'}
    if len(nodes)>1 and step['status']=='completed' and step.get('delivered'):
        return result|{'executable':False,'blocked_reason':'already_delivered','gap_kind':None}
    visiting=set(visiting or ())|{task_id}
    for dep in step.get('depends_on',[]):
        upstream=nodes[dep]
        if upstream['kind']=='constraint' or (upstream['status']=='completed' and upstream.get('delivered')):continue
        if dep in visiting:
            return result|{'executable':False,'blocked_reason':'dependency_cycle','gap_kind':'unsupported_capability'}
        state=execution_state(data,dep,visiting)
        if not state['executable']:
            return result|{'executable':False,'blocked_reason':'dependency_blocked','gap_kind':state['gap_kind'],'blocked_by':dep,'dependency_reason':state['blocked_reason']}
    return result


def route_tools(session, item):
    """Next registered operation or prerequisite; never invented arguments."""
    if item.get('tool'):return {item['tool']}
    op=item['operation']
    # Persisted prerequisites may use the exact registered tool as operation.
    # Such a concrete route must not become "unsupported" after migration.
    from cad_agent.analysis.tool_registry import TOOL_SPECS
    if op in TOOL_SPECS:
        return {op}
    if op=='read_requested_fact':return {'search_sources','read_source_context'}
    if op=='boundary_review':return {'prepare_room_boundaries','select_room_boundaries'}
    if op=='resolve_units':return {'resolve_room_units'}
    if op=='units' and not session.room_batches and any(
            'arithmetic' in s.get('operations', []) and s['id'] in item.get('task_ids', [])
            for s in session.task.data['subtasks']):
        return {'calculate_facts', 'read_source_context'}
    if op=='units' and not session.room_batches:
        # A room-only unit resolver requires a batch. General geometry checks
        # expose drawing units first and obtain applied-unit receipts on measure.
        seen={o['tool'] for o in session.observations.values()}
        return {'get_drawing_context'} if 'get_drawing_context' not in seen else {'inspect_scope_objects','measure_geometry','read_dimensions'}
    tools=set(OPERATION_TOOLS.get(op,()))
    if op in ('area', 'perimeter', 'contours', 'exclusions', 'relations', 'geometry'):
        # Spatial methods require persistent source inputs. Expose their actual
        # prerequisite path even after optional tool-family expansions run out.
        tools.update({'resolve_drawing_scope', 'inspect_scope_objects', 'query_entities',
                      'read_geometry', 'bind_spatial_object', 'get_spatial_object'})
        if op in ('area', 'perimeter', 'exclusions', 'relations'):
            tools.add('inspect_spatial_boundary')
    if op in ('geometry','vision') and getattr(session,'visual_images',{}):
        tools.add('bind_visual_objects')
    return tools


def schedule(session):
    data=session.task.data
    from cad_agent.analysis.tool_registry import TOOL_SPECS, runtime_supports
    from cad_agent.analysis.gap_routing import refresh, pending_calls
    refresh(session)
    recovery=pending_calls(data)
    work=[]
    for item in session.task.pending_work():
        ids=item.get('task_ids',[])
        states={i:execution_state(data,i) for i in ids}
        owners=[i for i,state in states.items() if state['executable']]
        registered=route_tools(session,item)&set(TOOL_SPECS)
        tools={tool for tool in registered if runtime_supports(tool,session.request.runtime,session.w.settings.provider)}
        if not session.request.source_read_batching:tools.discard('batch_read_sources')
        executable=bool(owners and tools)
        blocked=next((state for state in states.values() if not state['executable']),None)
        record=dict(item,executable=executable)
        # Advice annotates existing obligations. It never creates an acceptance
        # gate, resets budgets, or treats a successful call as gap resolution.
        actions=[action for action in recovery if set(action['task_ids']) & set(ids)]
        if actions:record['recovery_actions']=actions
        if executable:
            record['next_action']={'tools':sorted(tools),'task_ids':owners}
            if item.get('arguments') is not None:record['next_action']['arguments']=item['arguments']
        else:
            record.update(blocked or {'blocked_reason':'runtime_not_supported' if registered else 'no_registered_route','gap_kind':'unsupported_capability'})
            record['executable']=False
            record['next_action']={'tool':'finish_analysis','requirement_status':'unresolved','gap_kind':record.get('gap_kind')}
        work.append(record)
    return work
