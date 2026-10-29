import json
import threading
from types import SimpleNamespace

import pytest

from cad_agent.harness.tool_contract import contract_for, InvalidToolOutput
from cad_agent.harness.tool_pipeline import invocation, mark_dispatch, record_result
from cad_agent.harness.scheduler import ordered_compute


def test_all_model_tools_are_stateful_and_have_contracts():
    from cad_agent.analysis.tool_registry import TOOL_SPECS, capability_manifest
    manifest = {row['name']: row for row in capability_manifest()}
    for name, (schema, _) in TOOL_SPECS.items():
        contract = contract_for(name)
        assert contract.input_model is schema
        assert not contract.parallel_safe
        assert manifest[name]['changes_session_state']
        assert 'current_requirement_scope' in contract.prerequisites


@pytest.mark.parametrize('value', [[1], {'width': float('nan')}, {'width': float('inf')}, {'data': object()}])
def test_invalid_output_never_enters_projection(value):
    with pytest.raises(InvalidToolOutput):
        contract_for('read_source_context').validate_output(value)


def test_output_snapshot_cannot_be_mutated_by_provider():
    result = {'items': [{'state': 'candidate', 'unit': 'unknown'}]}
    frozen = contract_for('read_source_context').validate_output(result)
    result['items'][0]['state'] = 'confirmed'
    assert frozen['items'][0]['state'] == 'candidate'


class Execution:
    def __init__(self): self.rows = []
    def begin_call(self, name, args, call_id=None):
        self.rows.append(('received', call_id)); return call_id
    def admit_call(self, key): self.rows.append(('admitted', key))
    def dispatch_call(self, key): self.rows.append(('dispatched', key))
    def settle_call(self, key, status, result=None): self.rows.append((status, key))


def test_denial_and_dispatch_failure_are_distinct():
    execution = Execution()
    session = SimpleNamespace(execution=execution, harness_call_id='provider_call')
    with invocation(session, 'read_source_context', {}) as item:
        item['result'] = json.dumps({'error': {'code': 'NO_SOURCE'}})
    assert [r[0] for r in execution.rows] == ['received', 'rejected']
    execution.rows.clear()
    with pytest.raises(RuntimeError):
        with invocation(session, 'read_source_context', {}):
            mark_dispatch(session, 'read_source_context')
            raise RuntimeError('worker lost')
    assert [r[0] for r in execution.rows] == ['received', 'admitted', 'dispatched', 'unknown']


def test_nested_batch_has_distinct_child_ids():
    execution = Execution()
    session = SimpleNamespace(execution=execution, harness_call_id='batch')
    with invocation(session, 'batch_read_sources', {}) as outer:
        with invocation(session, 'read_source_context', {}) as inner:
            inner['result'] = '{}'
            assert inner['id'] != outer['id']
        outer['result'] = '{}'


@pytest.mark.parametrize('result,status', [({'answer': 42}, 'completed'), ({'error': {'code': 'INVALID'}}, 'failed')])
def test_result_survives_cancellation_after_tool_return(result, status):
    execution = Execution()
    session = SimpleNamespace(execution=execution)
    with pytest.raises(RuntimeError, match='cancelled'):
        with invocation(session, 'read_source_context', {}):
            mark_dispatch(session, 'read_source_context')
            record_result(session, json.dumps(result))
            raise RuntimeError('cancelled before publication')
    assert execution.rows[-1][0] == status


def test_batch_pre_registered_call_does_not_register_twice():
    execution = Execution()
    session = SimpleNamespace(execution=execution, harness_call_id='registered', harness_call_registered=True)
    with invocation(session, 'batch_read_sources', {}) as outer:
        with invocation(session, 'read_source_context', {}) as inner:
            inner['result'] = '{}'
        outer['result'] = '{}'
    assert [r for r in execution.rows if r[0] == 'received'] == [('received', inner['id'])]


def test_invalid_handler_output_rolls_back_ungranted_evidence(workspace, demo_bytes, monkeypatch):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    drawing = workspace.ingest('demo.dxf', demo_bytes)
    session = AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question='内墙材料是什么', mode='agent'))
    def invalid(*args, **kwargs):
        session.evidence['forged'] = {'evidence_id': 'forged'}
        session.observations['forged'] = {'observation_id': 'forged'}
        session.task.data['geometry_workset'] = {'forged': True}
        return {'value': float('nan')}
    monkeypatch.setattr(session, 'execute', invalid)
    result = json.loads(session.invoke_tool('search_sources', {'query': '内墙'}))
    assert result['error']['code'] == 'TOOL_OUTPUT_INVALID'
    assert 'forged' not in session.evidence and 'forged' not in session.observations
    assert 'geometry_workset' not in session.task.data


def test_completed_receipt_persisted_even_if_run_cancelled(tmp_path):
    from cad_agent.adapters.store import Store
    from cad_agent.execution import ExecutionManager, ExecutionContext, ExecutionCancelled
    manager = ExecutionManager(Store(tmp_path))
    record = {'id': 'run', 'status': 'running'}
    manager.claim('chat_run', record)
    execution = ExecutionContext(manager, 'chat_run', 'run', record['execution_token'])
    session = SimpleNamespace(execution=execution)
    try:
        with pytest.raises(ExecutionCancelled):
            with invocation(session, 'read_source_context', {}) as call:
                mark_dispatch(session, 'read_source_context')
                record_result(session, '{"items":[]}')
                manager.store.compare_and_swap('chat_run', 'run', {}, {'cancel_requested': True})
                execution.check_cancelled()
        assert manager.events.calls('run')[call['id']]['status'] == 'completed'
    finally:
        manager.close()


def test_legacy_native_refreshes_prompt_after_permission_revocation(workspace, demo_bytes):
    from dataclasses import replace
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.task_methods import PROMPT as METHOD_PROMPT
    drawing = workspace.ingest('demo.dxf', demo_bytes)
    session = AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question='读取内墙材料', mode='agent',
        agent_policy='legacy', dynamic_methods='task_verified'))
    session.w.settings = replace(session.w.settings, provider='custom', protocol='openai', model='test-api-model')
    messages = [{'role': 'system', 'content': session.system_prompt}, {'role': 'user', 'content': '读取内墙材料'}]
    assert METHOD_PROMPT in messages[0]['content']
    session.request = session.request.model_copy(update={'dynamic_methods': 'off'})
    refreshed = session.model_messages(messages)
    assert METHOD_PROMPT not in refreshed[0]['content']


def test_compute_bounded_order_and_input_isolation(monkeypatch):
    import cad_agent.harness.scheduler as scheduler
    lock, release, entered = threading.Lock(), threading.Event(), threading.Event()
    active = peak = 0
    def work(operation, payload):
        nonlocal active, peak
        with lock:
            active += 1; peak = max(peak, active)
            if active == 2: entered.set()
        if payload['i'] == 0:
            assert entered.wait(2)
        original = payload['i']; payload['i'] = -1
        with lock: active -= 1
        return original
    monkeypatch.setattr(scheduler, '_dispatch', work)
    payloads = [{'i': i} for i in range(8)]
    assert list(ordered_compute('measure_pair', payloads)) == list(range(8))
    assert peak == 2 and payloads[0]['i'] == 0


def test_cancelled_compute_does_not_yield_late_result(monkeypatch):
    import cad_agent.harness.scheduler as scheduler
    cancelled = threading.Event()
    def work(*_): cancelled.set(); return 'late'
    def check():
        if cancelled.is_set(): raise RuntimeError('cancelled')
    monkeypatch.setattr(scheduler, '_dispatch', work)
    with pytest.raises(RuntimeError, match='cancelled'):
        list(ordered_compute('measure_pair', [{}], check=check))
    with pytest.raises(ValueError, match='DENIED'):
        list(ordered_compute('shell', []))
