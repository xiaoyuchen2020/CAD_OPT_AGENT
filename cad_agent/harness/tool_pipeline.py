"""Durable invocation boundaries around the CAD-owned validation/commit path."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from uuid import uuid4

_active = ContextVar('cad_tool_invocation', default=None)


def record_result(session, result):
    """Remember a settled tool result before cancellation/publication checks."""
    current = _active.get()
    if current is not None and current['session'] is session:
        current['result'] = result


def _outcome(current):
    result = current['result']
    packet = json.loads(result) if isinstance(result, str) else result
    status = ('failed' if current['dispatched'] else 'rejected') if isinstance(packet, dict) and packet.get('error') else 'completed'
    return status, packet


def mark_dispatch(session, name):
    current = _active.get()
    if current is None or current['session'] is not session or current['name'] != name:
        return
    if current['dispatched']:
        return
    execution = getattr(session, 'execution', None)
    if execution and hasattr(execution, 'admit_call'):
        execution.admit_call(current['id'])
        execution.dispatch_call(current['id'])
    current['dispatched'] = True


@contextmanager
def invocation(session, name, arguments):
    execution = getattr(session, 'execution', None)
    parent = _active.get()
    call_id = (getattr(session, 'harness_call_id', None) if parent is None else None) or 'call_' + uuid4().hex
    registered = parent is None and getattr(session, 'harness_call_registered', False) and getattr(session, 'harness_call_id', None) == call_id
    if not registered and execution and hasattr(execution, 'begin_call'):
        call_id = execution.begin_call(name, arguments, call_id=call_id)
    current = dict(session=session, name=name, id=call_id, dispatched=False, result=None)
    token = _active.set(current)
    try:
        yield current
    except BaseException:
        if execution and hasattr(execution, 'settle_call'):
            # Fencing may already have transferred ownership; recovery owns that case.
            try:
                if current['result'] is not None:
                    status, packet = _outcome(current)
                    execution.settle_call(call_id, status=status, result=packet)
                else:
                    execution.settle_call(call_id, status='unknown' if current['dispatched'] else 'not_started')
            except Exception:
                pass
        raise
    else:
        status, packet = _outcome(current)
        if execution and hasattr(execution, 'settle_call'):
            execution.settle_call(call_id, status=status, result=packet)
    finally:
        _active.reset(token)
