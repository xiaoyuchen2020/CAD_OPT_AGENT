"""Bounded transport batching; every source read keeps the ordinary execution path."""
import json
from typing import Literal

from pydantic import Field, ValidationError

from cad_agent.contracts import Record
from cad_agent.analysis.efficiency import compact, dumps

NOTE = '各项独立校验、计费和分页；失败项未取得证据。按 next_start/next_actions 续读；pending_reads 未执行，可按原工具参数继续。'
WIRE_LIMIT = 50000


def execute_sized(session, name, args):
    """Size a local read before registry/coverage/cache/accounting commit.

    Only assembly has an independently executable row cursor. Source-context
    neighbors/parents are deliberately never dropped to make a packet fit.
    Local attempts do not start another tool call or model request.
    """
    if name == 'read_assembly':
        args.setdefault('limit', 8)
    before = dict(session.evidence)
    initial_limit = args.get('limit')
    attempts = 0
    while True:
        session._remaining()
        result = session.execute(name, args, bounded=session.policy.enabled)
        attempts += 1
        size = len(dumps(compact(result, set(session.policy.seen_evidence)))) if session.policy.enabled else len(json.dumps(result, ensure_ascii=False))
        preferred = getattr(session, '_batch_read_page_target', session._tool_response_limit)
        if size <= preferred or name != 'read_assembly' or args['limit'] <= 1:
            if initial_limit is not None and initial_limit != args['limit']:
                result['batch_pagination'] = dict(requested_limit=initial_limit, effective_limit=args['limit'], local_attempts=attempts)
            return result
        # No observations/coverage/task completion have been recorded yet. Drop
        # evidence from the rejected local page before invoking the same reader.
        session.evidence = dict(before)
        args['limit'] = max(1, min(args['limit'] - 1, int(args['limit'] * preferred / size)))



def defer_source_for_wire(session, read, capacity, original_limit):
    """Pure preflight: defer a valid standalone-sized context, never trim parents.

    This does not register evidence, create observations or start a tool/checkpoint.
    Invalid arguments/owners still enter the ordinary counted failure path.
    """
    if 'task_ids' in read.arguments:
        return False
    if read.tool != 'read_source_context' or capacity >= original_limit - 2000:
        return False
    from cad_agent.analysis.tool_registry import TOOL_SPECS
    from cad_agent.analysis.evidence_ledger import source_context
    from cad_agent.analysis.scope_workflow import bind_scope_call
    try:
        owners = read.task_ids or ([session.task.parts[0]['id']] if not session.task.complex else [])
        session.task.select(owners)
        arguments = dict(read.arguments)
        arguments.pop('task_ids', None)
        args = TOOL_SPECS[read.tool][0].model_validate(arguments, strict=True)
        args = bind_scope_call(session, read.tool, args)
        if session.policy.key(read.tool, args.model_dump()) in session.policy.cache:
            return False
        session._remaining()
        result = source_context(session.w, session.request.snapshot_id,
                                **args.model_dump(), bounded=session.policy.enabled)
        encoded = dumps(compact(result, set(session.policy.seen_evidence))) if session.policy.enabled else json.dumps(result, ensure_ascii=False)
        return capacity < len(encoded) <= original_limit
    except (ValueError, KeyError, TypeError):
        return False


def charge_envelope(session, chars):
    session.policy.response_chars += chars
    limit = 100000 if getattr(session.policy, 'geometry_work', False) else getattr(session.policy, 'context_limit', 28000)
    if session.policy.enabled and session.policy.response_chars >= limit:
        session.policy.stop_reason = session.policy.stop_reason or 'tool_context_budget'


class SourceRead(Record):
    tool: Literal['read_source_context', 'read_assembly']
    arguments: dict = Field(description='同名读取工具的参数；task_ids 必须放在本项外层，与 tool/arguments 同级，不放在 arguments 内。')
    task_ids: list[str] = Field(default_factory=list, max_length=10,
        description='此项独立负责的要求；复杂任务必填，不从其他项继承。')


class BatchReadSources(Record):
    reads: list[SourceRead] = Field(min_length=1, max_length=4)


def batch_schema(session, specs):
    """Advertise the exact child shape, including dynamic owner requirements."""
    schema = BatchReadSources.model_json_schema()
    variants = []
    for name in ('read_source_context', 'read_assembly'):
        child = SourceRead.model_json_schema()
        child['properties']['tool'] = {'type': 'string', 'const': name}
        child['properties']['arguments'] = specs[name][0].model_json_schema()
        owner = child['properties']['task_ids']
        owner['description'] = ('此项负责的原始任务 ID；与 tool/arguments 同级。'
                                '按本项实际用途明确选择，不继承其他项，不默认关联全部任务。')
        if session.task.complex:
            owner['minItems'] = 1
            owner['items']['enum'] = [p['id'] for p in session.task.parts]
            child['required'].append('task_ids')
        variants.append(child)
    schema['properties']['reads']['items'] = {'anyOf': variants}
    schema.pop('$defs', None)
    return schema


class BatchOwnerError(ValueError):
    def __init__(self, issue, field='task_ids'):
        super().__init__(issue)
        self.issue, self.field = issue, field


def validate_batch_owner(session):
    """Run inside the counted child call, before task selection/filtering."""
    context = getattr(session, '_batch_read_context', None)
    if context is None:
        return
    read = context['read']
    if 'task_ids' in read.arguments:
        raise BatchOwnerError('misplaced_task_ids', 'arguments.task_ids')
    if session.task.complex and not read.task_ids:
        raise BatchOwnerError('missing_task_ids')
    known = {p['id'] for p in session.task.parts}
    if any(owner not in known for owner in read.task_ids):
        raise BatchOwnerError('unknown_task_ids')


def batch_error_feedback(session, error, fallback):
    """Preserve the original code, attaching a precise child-relative location."""
    context = getattr(session, '_batch_read_context', None)
    if context is None:
        return fallback
    position = context['position']
    prefix = f'reads.{position}'
    result = dict(fallback, batch_position=position, tool=context['read'].tool)
    if isinstance(error, BatchOwnerError):
        messages = {
            'missing_task_ids': '必须在此项外层填写非空 task_ids，与 tool/arguments 同级。',
            'misplaced_task_ids': 'task_ids 层级错误：移到本项外层，与 tool/arguments 同级；不会自动继承或合并。',
            'unknown_task_ids': 'task_ids 含当前任务中不存在的 ID；仅选择此项实际负责的有效任务。',
        }
        result.update(path=f'{prefix}.{error.field}', issue=error.issue,
                      expected_path=f'{prefix}.task_ids',
                      allowed_task_ids=[p['id'] for p in session.task.parts],
                      message=messages[error.issue])
    elif isinstance(error, ValidationError):
        result['path'] = f'{prefix}.arguments'
        result['field_errors'] = [
            dict(path=f"{prefix}.arguments." + '.'.join(map(str, e['loc'])),
                 code=e['type'], message=e['msg'])
            for e in error.errors(include_input=False, include_url=False)[:8]
        ]
        result['message'] = '本项读取参数无效；按 field_errors 修正，仅重试失败项。'
    else:
        result['path'] = f'{prefix}.arguments'
    return result


def invoke_batch(session, args):
    allowed = {t['function']['name'] for t in session.offered_tools(planning_only=True)}
    results, pending = [], []
    batch_number = len(getattr(session, 'source_read_batches', [])) + 1
    original_limit = getattr(session, '_tool_response_limit', WIRE_LIMIT)
    original_allowed = getattr(session, '_batch_read_allowed_tools', None)
    original_target = getattr(session, '_batch_read_page_target', None)
    original_context = getattr(session, '_batch_read_context', None)
    session._batch_read_allowed_tools = allowed
    progress = dict(batch=batch_number, completed_positions=[], pending_reads=[r.model_dump() for r in args.reads])
    session.task.data['source_read_batch_progress'] = progress
    session.task.save()
    starting_chars = session.policy.response_chars
    charge_envelope(session, len(dumps(dict(items=[], pending_reads=[], note=NOTE))))
    try:
        for position, read in enumerate(args.reads):
            if session.calls >= session.w.settings.agent_max_tool_calls or session.policy.stop_reason:
                pending = [r.model_dump() for r in args.reads[position:]]
                break
            session._remaining()
            # Reserve the exact unexecuted request tail and current item framing.
            # A large read can use the remaining packet instead of an arbitrary
            # 7.5k slot; subsequent reads remain explicit unexecuted requests.
            tail = [r.model_dump() for r in args.reads[position+1:]]
            shell = dict(position=position, tool=read.tool, task_ids=read.task_ids, result={})
            reserved = len(dumps(dict(items=results+[shell], pending_reads=tail, note=NOTE))) - 2
            capacity = min(original_limit, WIRE_LIMIT - reserved)
            if capacity < 2000 or (read.tool in allowed and defer_source_for_wire(session, read, capacity, original_limit)):
                pending = [r.model_dump() for r in args.reads[position:]]
                break
            session._tool_response_limit = capacity
            context_limit = 100000 if getattr(session.policy, 'geometry_work', False) else getattr(session.policy, 'context_limit', 28000)
            remaining = max(0, context_limit - session.policy.response_chars)
            session._batch_read_page_target = min(capacity, max(2000, remaining // (len(args.reads)-position))) if session.policy.enabled else capacity
            session._batch_read_context = dict(position=position, read=read)
            raw = dict(read.arguments)
            raw['task_ids'] = read.task_ids
            before = len(session.trace)
            before_chars = session.policy.response_chars
            try:
                value = json.loads(session.invoke_tool(read.tool, raw))
            finally:
                children = session.trace[before:]
                for entry in children:
                    entry.update(source_read_batch=batch_number, source_read_position=position)
                # A durable completed/failed call must never be replayed after
                # after_tool raises cancellation. Keep only unexecuted requests.
                if children and children[-1]['status'] in ('completed', 'failed'):
                    progress['completed_positions'].append(position)
                    progress['pending_reads'] = tail
                    session.task.save()
            item = dict(position=position, tool=read.tool, task_ids=read.task_ids, result=value)
            # Reconcile actual model-visible bytes, including failed/legacy calls.
            charge_envelope(session, len(dumps(item)) + int(bool(results)) - (session.policy.response_chars-before_chars))
            results.append(item)
    finally:
        session._tool_response_limit = original_limit
        session._batch_read_allowed_tools = original_allowed
        session._batch_read_page_target = original_target
        session._batch_read_context = original_context
    output = dict(items=results, pending_reads=pending, note=NOTE)
    # Shared context appears once and only if it fits. It is supplemental; all
    # source text, source IDs, owner IDs and continuation cursors stay in items.
    if session.policy.enabled and session.task.complex:
        shared = dict(task=session.focus.state() if session.focused_enabled else session.task.context(),
                      evidence_coverage=session.coverage.summary(), next_action=session.policy.hint())
        if len(dumps(output | {'context': shared})) <= WIRE_LIMIT:
            output['context'] = shared
    encoded = dumps(output)
    assert len(encoded) <= WIRE_LIMIT, 'Batch source-read packet exceeds the wire limit'
    charge_envelope(session, len(encoded) - (session.policy.response_chars-starting_chars))
    if not hasattr(session, 'source_read_batches'):
        session.source_read_batches = []
    session.source_read_batches.append(dict(batch=batch_number, requested=len(args.reads),
        executed=len(results), pending=len(pending), response_chars=len(encoded)))
    return encoded
