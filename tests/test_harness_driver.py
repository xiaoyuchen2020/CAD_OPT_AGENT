"""Execution contract parity and failure boundaries across wire protocols."""
from dataclasses import FrozenInstanceError
import json
import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.execution import ExecutionCancelled
from cad_agent.harness.controls import BudgetController
from cad_agent.harness.driver import HarnessDriver
from cad_agent.harness.model_adapter import ChatAdapter, ResponsesAdapter, ToolCall
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime


@pytest.fixture
def cad_session(workspace, demo_bytes):
    drawing = workspace.ingest('driver.dxf', demo_bytes)
    return AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question='外墙做法', mode='agent'))


@pytest.fixture(params=[ChatAdapter, ResponsesAdapter])
def driver(request, cad_session, monkeypatch):
    value = HarnessDriver(cad_session, request.param())
    monkeypatch.setattr(value, 'pending', lambda: False)
    value.begin_step(0, [{'function': {'name': 'search_sources'}}], False)
    return value


def calls(driver, *names):
    return [ToolCall(f'c{i}', name, '{}', 'cad' if driver.adapter.protocol == 'responses' else None)
            for i, name in enumerate(names)]


def packets(messages):
    return [json.loads(m.get('content', m.get('output'))) for m in messages]


def test_admission_snapshot_cannot_change_with_live_task(cad_session):
    driver = HarnessDriver(cad_session, ChatAdapter())
    tools = [{'function': {'name': 'search_sources', 'parameters': {'type': 'object'}}}]
    step = driver.begin_step(0, tools, False)
    original = step.public()
    tools[0]['function']['name'] = 'forbidden'
    cad_session.task.data['subtasks'][0]['requirement'] = 'changed'
    assert step.public() == original
    assert step.source_hashes == ((cad_session.request.snapshot_id, cad_session.drawing['sha256']),)
    with pytest.raises(FrozenInstanceError):
        step.final_only = True


def test_entire_batch_validated_before_any_tool_dispatch(driver, monkeypatch):
    executed = []
    monkeypatch.setattr(driver.session, 'invoke_tool', lambda *a: executed.append(a))
    invalid = calls(driver, 'search_sources', 'shell')
    invalid[1] = ToolCall(invalid[0].call_id, 'shell', '{}', invalid[1].namespace)
    with pytest.raises(Exception, match='未授权'):
        driver.execute_calls(invalid, [],
                             allowed={'search_sources'}, advertised={'search_sources'})
    assert executed == []


def test_failed_dispatch_preserves_pairs_and_does_not_run_sibling(driver, monkeypatch):
    executed = []
    def abort(name, args):
        executed.append(name)
        raise ExecutionCancelled('cancel')
    monkeypatch.setattr(driver.session, 'invoke_tool', abort)
    messages = []
    with pytest.raises(ExecutionCancelled):
        driver.execute_calls(calls(driver, 'search_sources', 'read_source_context'), messages,
                             allowed={'search_sources', 'read_source_context'},
                             advertised={'search_sources', 'read_source_context'})
    assert executed == ['search_sources']
    assert [p['error']['code'] for p in packets(messages)] == ['TOOL_OUTCOME_UNKNOWN', 'TOOL_NOT_STARTED']
    assert driver.session.harness_call_id is None


def test_prepared_delivery_settles_but_does_not_execute_siblings(driver, monkeypatch):
    executed = []
    def finish(name, args):
        executed.append(name)
        driver.session.prepared_answer = {'claims': ['validated']}
        return '{"accepted":true}'
    monkeypatch.setattr(driver.session, 'invoke_tool', finish)
    messages = []
    result = driver.execute_calls(calls(driver, 'finish_analysis', 'search_sources'), messages,
                                 allowed={'finish_analysis', 'search_sources'},
                                 advertised={'finish_analysis', 'search_sources'})
    assert executed == ['finish_analysis']
    assert result == {'claims': ['validated']}
    assert packets(messages)[1]['error']['code'] == 'DELIVERY_ALREADY_PREPARED'


def test_stage_denial_does_not_call_handler(driver, monkeypatch):
    monkeypatch.setattr(driver.session, 'invoke_tool', lambda *a: pytest.fail('denied tool executed'))
    messages = []
    driver.execute_calls(calls(driver, 'read_source_context'), messages,
                         allowed={'search_sources'}, advertised={'read_source_context', 'search_sources'})
    assert packets(messages)[0]['error']['code'] == 'TOOL_STAGE_NOT_READY'


def test_steering_during_last_delivery_handler_keeps_loop_running(driver, monkeypatch):
    pending = []
    monkeypatch.setattr(driver, 'pending', lambda: bool(pending))
    def finish(name, args):
        driver.session.prepared_answer = {'old_question': 'validated'}
        pending.append('accepted new question')
        return '{"accepted":true}'
    monkeypatch.setattr(driver.session, 'invoke_tool', finish)
    messages = []
    result = driver.execute_calls(calls(driver, 'finish_analysis'), messages,
                                 allowed={'finish_analysis'}, advertised={'finish_analysis'})
    assert result is None
    assert packets(messages) == [{'accepted': True}]


def test_shared_controls_keep_legacy_adapter_compatibility(cad_session):
    shared, legacy = BudgetController(), CodexSubscriptionRuntime()
    assert shared.total_budget(cad_session) == legacy.total_budget(cad_session)
    assert shared.exploration_budget(cad_session) == legacy.exploration_budget(cad_session)


def test_step_scoped_chat_call_ids_do_not_alias_responses_replays(driver, monkeypatch):
    monkeypatch.setattr(driver.session, 'invoke_tool', lambda *a: '{}')
    batch = calls(driver, 'search_sources')
    driver.execute_calls(batch, [], allowed={'search_sources'}, advertised={'search_sources'})
    driver.begin_step(1, [], False)
    if driver.adapter.protocol == 'responses':
        with pytest.raises(Exception, match='重复编号'):
            driver.execute_calls(batch, [], allowed={'search_sources'}, advertised={'search_sources'})
    else:
        driver.execute_calls(batch, [], allowed={'search_sources'}, advertised={'search_sources'})


def test_entire_batch_is_durable_before_first_handler_and_skips_do_not_reregister(driver, monkeypatch):
    class Execution:
        def __init__(self): self.received, self.settled = [], []
        def begin_call(self, name, args, call_id=None):
            assert call_id not in self.received
            self.received.append(call_id)
            return call_id
        def settle_call(self, call_id, status='completed', result=None):
            self.settled.append((call_id, status))
        def emit(self, *a, **kw): pass
    execution = Execution()
    driver.session.execution = execution
    monkeypatch.setattr(driver, 'ready_for_delivery', lambda: True)
    monkeypatch.setattr(driver.session, '_remaining', lambda: 100)
    def finish(name, args):
        assert len(execution.received) == 2
        assert driver.session.harness_call_registered
        assert driver.session.harness_call_id == execution.received[0]
        driver.session.prepared_answer = {'validated': True}
        return '{"accepted":true}'
    monkeypatch.setattr(driver.session, 'invoke_tool', finish)
    driver.execute_calls(calls(driver, 'finish_analysis', 'search_sources'), [],
                         allowed={'finish_analysis', 'search_sources'},
                         advertised={'finish_analysis', 'search_sources'})
    assert len(execution.received) == 2
    assert execution.settled == [(execution.received[1], 'not_started')]
    assert not driver.session.harness_call_registered


def test_native_steering_during_final_validation_applies_before_return(workspace, demo_bytes, monkeypatch):
    from cad_agent.contracts import ChatSteerRequest
    from cad_agent.harness.inbox import enqueue
    from cad_agent.runtimes.native import NativeRuntime
    from test_harness_lifecycle import session_run
    session, run = session_run(workspace, demo_bytes)
    validated = []
    def finish(answer):
        validated.append(session.request.question)
        if len(validated) == 1:
            enqueue(workspace, run['id'], ChatSteerRequest(question='只读取内墙材料'))
        return {'question': session.request.question}
    monkeypatch.setattr(session, 'finish', finish)
    monkeypatch.setattr(workspace.gateway, 'chat_turn', lambda *a, **kw: ({'content': '{}'}, {}))
    result = NativeRuntime().run(session)
    assert len(validated) == 2
    assert result == {'question': '只读取内墙材料'}
    assert session.input_revision == 1
