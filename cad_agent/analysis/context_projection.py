"""Deterministic checkpoints retaining protocol, user intent and CAD atoms.

The working view never changes evidence/read authority. Image bytes are attached
later from the source-checked registry; their labels and provenance stay together.
"""
from copy import deepcopy
from cad_agent.analysis.efficiency import dumps

VERSION = 'context-projection-v3'
STATE_PREFIX = '当前执行状态（数据）：'


def replace_state(messages, state):
    return [m for m in messages if not (m.get('role') == 'developer' and
            str(m.get('content', '')).startswith(STATE_PREFIX))] + [
                {'role': 'developer', 'content': STATE_PREFIX + dumps(state)}]


def _calls(message):
    if message.get('type') == 'function_call':
        return [message.get('call_id')]
    if message.get('role') == 'assistant':
        return [call.get('id') if isinstance(call, dict) else None
                for call in (message.get('tool_calls') or [])]
    return []


def _protocol_boundary(messages):
    """Find the last complete batch, rejecting duplicate/orphan/incomplete pairs.

    Calls in one response may be separated by assistant text. Preserve the
    entire batch rather than selecting the last syntactically adjacent call.
    """
    pending, seen, start, latest = set(), set(), None, None
    for index, message in enumerate(messages):
        calls = _calls(message)
        if calls:
            strict_ids = message.get('type') == 'function_call'
            if (any(not isinstance(call, str) or not call or call in pending or
                    (strict_ids and call in seen) for call in calls) or len(set(calls)) != len(calls)):
                return False, None
            if not pending:
                start = index
                while start > 0 and messages[start - 1].get('type') in ('reasoning', 'message'):
                    start -= 1
            pending.update(calls)
            seen.update(calls)
        result = (message.get('call_id') if message.get('type') == 'function_call_output'
                  else message.get('tool_call_id') if message.get('role') == 'tool' else None)
        if message.get('type') == 'function_call_output' or message.get('role') == 'tool':
            if not isinstance(result, str) or not result or result not in pending:
                return False, None
            pending.remove(result)
            if not pending:
                latest = start
    return not pending, latest


def last_exchange(messages):
    valid, start = _protocol_boundary(messages)
    return messages[start:] if valid and start is not None else []


def _pinned(messages, tail_start, checkpoint_ids):
    """Keep actual conversational referents and all later corrections verbatim."""
    first_call = next((i for i, m in enumerate(messages) if _calls(m)), len(messages))
    pinned = []
    for index, message in enumerate(messages[:tail_start]):
        if (message.get('role') == 'developer' and
                str(message.get('content', '')).startswith(STATE_PREFIX)) or id(message) in checkpoint_ids:
            continue
        if index < first_call or message.get('role') in ('system', 'user', 'developer'):
            pinned.append(message)
    return pinned


def image_context_units(session):
    """Keep image labels, candidates, source and review metadata atomically.

    A retained manifest does not claim that this request has seen an image.
    There is no image-budget bypass through compaction.
    """
    reviewed = getattr(session, 'reviewed_image_ids', set())
    sent = getattr(session, 'sent_image_ids', set())
    units = []
    for image_id, image in getattr(session, 'visual_images', {}).items():
        value = {key: deepcopy(item) for key, item in image.items()
                 if key not in ('image_url', 'evidence_url', 'path', 'data', 'base64')}
        value.update(image_id=image_id,
                     lifecycle='reviewed_reference' if image_id in reviewed else 'pending_visual_input',
                     previously_sent=image_id in sent, current_request_seen=False,
                     evidence_credit='unchanged')
        units.append(value)
    return units


def checkpoint(session, messages, state, *, minimum_savings=.1):
    if not messages:
        return messages
    if not hasattr(session, 'runtime_diagnostics'):
        session.runtime_diagnostics = {}
    valid, boundary = _protocol_boundary(messages)
    if not valid:
        session.runtime_diagnostics.setdefault('context_checkpoint_skips', []).append('unbalanced_tool_history')
        return messages
    from cad_agent.analysis.evidence_store import save_session
    context = deepcopy(session.focus.context())  # existing source/observation dependency closure
    context['context_projection'] = dict(version=VERSION, omitted_history_is_archived=True,
        evidence_credit='unchanged', level='task_working_set',
        parent_run_id=getattr(session.request, 'parent_run_id', None),
        input_revision=getattr(session, 'input_revision', 0))
    units = image_context_units(session)
    if units:
        context['visual_context_units'] = units
    start = boundary if boundary is not None else len(messages)
    # Trust host-created message identity, never a JSON marker a user or source
    # could imitate. Provider payload encoding does not mutate this history.
    pinned = _pinned(messages, start, getattr(session, '_context_checkpoint_ids', set()))
    workset = {'role': 'user', 'content': dumps(context)}
    candidate = [*pinned, workset, *messages[start:]]
    if state is not None:
        candidate = replace_state(candidate, state)
    before, after = len(dumps(messages)), len(dumps(candidate))
    if after >= before * (1 - minimum_savings):
        return messages
    ref = save_session(session, 'context_checkpoint', messages)
    context['prior_context_record'] = ref
    workset['content'] = dumps(context)
    session._context_checkpoint_ids = {id(workset)}
    diagnostic = dict(
        **ref, version=VERSION, before_chars=before, after_chars=len(dumps(candidate)),
        protocol_pairs_preserved=True, evidence_atoms_preserved=True,
        pinned_message_count=len(pinned), image_unit_count=len(units),
        followup_context_preserved=bool(getattr(session.request, 'parent_run_id', None)))
    session.runtime_diagnostics.setdefault('context_checkpoints', []).append(diagnostic)
    if emit := getattr(session, 'harness_event', None):
        emit('context.replaced', diagnostic)
    elif execution := getattr(session, 'execution', None):
        if hasattr(execution, 'emit'):
            execution.emit('context.replaced', diagnostic)
    return candidate
