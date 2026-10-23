"""Deterministic checkpoints with protocol pairs and domain evidence atoms."""
from cad_agent.analysis.efficiency import dumps

VERSION = 'context-projection-v2'
STATE_PREFIX = '当前执行状态（数据）：'


def replace_state(messages, state):
    return [m for m in messages if not (m.get('role') == 'developer' and
            str(m.get('content', '')).startswith(STATE_PREFIX))] + [
                {'role': 'developer', 'content': STATE_PREFIX + dumps(state)}]


def last_exchange(messages):
    """Responses function calls and all outputs from their response stay together."""
    start = next((i for i in range(len(messages)-1, -1, -1)
                  if messages[i].get('type') == 'function_call'), None)
    if start is None:
        return []
    while start > 0 and messages[start-1].get('type') in ('function_call', 'reasoning'):
        start -= 1
    block = [m for m in messages[start:] if m.get('role') != 'developer']
    calls = {m.get('call_id') for m in block if m.get('type') == 'function_call'}
    outputs = {m.get('call_id') for m in block if m.get('type') == 'function_call_output'}
    return block if calls and calls == outputs else []


def checkpoint(session, messages, state):
    # Visual/reference-heavy sessions retain existing conservative behavior.
    if session.visual_images or getattr(session.request, 'parent_run_id', None):
        return messages
    from cad_agent.analysis.evidence_store import save_session
    context = session.focus.context()  # bounded_packet preserves source binding + whole quotes
    context['context_projection'] = dict(version=VERSION, omitted_history_is_archived=True,
        evidence_credit='unchanged', level='task_working_set')
    candidate = [messages[0], {'role': 'user', 'content': dumps(context)}, *last_exchange(messages)]
    candidate = replace_state(candidate, state)
    before, after = len(dumps(messages)), len(dumps(candidate))
    if after >= before * .9:
        return messages
    ref = save_session(session, 'context_checkpoint', messages)
    context['prior_context_record'] = ref
    candidate[1]['content'] = dumps(context)
    session.runtime_diagnostics.setdefault('context_checkpoints', []).append(dict(
        **ref, version=VERSION, before_chars=before, after_chars=len(dumps(candidate)),
        protocol_pairs_preserved=True, evidence_atoms_preserved=True))
    return candidate
