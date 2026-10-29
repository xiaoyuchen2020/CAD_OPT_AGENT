"""Real execution boundaries; model and Docker I/O remain mocked."""
from dataclasses import replace
import json
import threading
import time

import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.method_authorization import authorization
from cad_agent.contracts import ChatRequest
from cad_agent.execution import ExecutionContext, ExecutionCancelled, ExecutionManager
from cad_agent.harness import live_controls as controls
from cad_agent.harness.driver import HarnessDriver
from cad_agent.harness.model_adapter import ChatAdapter
from cad_agent.adapters.model import ModelError


def running(workspace, demo_bytes, question='内墙材料是什么', runtime='native'):
    drawing = workspace.ingest('control.dxf', demo_bytes)
    session = AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question=question,
                                               mode='agent', runtime=runtime))
    record = {'id': 'control_run', 'status': 'running', 'request': session.request.model_dump(),
              'code_execution_enabled': authorization(session.request)['mode'] == 'task_verified'}
    workspace.execution.claim('chat_run', record, session.task.data)
    session.execution = ExecutionContext(workspace.execution, 'chat_run', record['id'], record['execution_token'])
    session.input_revision = 0
    return session, record


def command(workspace, action, payload=None, revision=None, ident=None, source='human'):
    current = controls.read(workspace, 'control_run')
    return controls.enqueue(workspace, 'control_run', controls.ControlRequest(
        command_id=ident or f'cmd-{current["revision"]+1}',
        expected_revision=current['revision'] if revision is None else revision,
        action=action, payload=payload or {}, source=source))


def wait_for(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition not reached')


def test_control_cas_idempotency_and_rejected_audit(workspace, demo_bytes):
    session, _ = running(workspace, demo_bytes)
    first = command(workspace, 'configure', {'budget': {'max_rounds': 25}}, ident='first')
    assert first['revision'] == 1 and first['commands'][0]['status'] == 'pending'
    repeated = command(workspace, 'configure', {'budget': {'max_rounds': 25}}, revision=0, ident='first')
    assert repeated['revision'] == 1
    with pytest.raises(controls.ControlConflict):
        command(workspace, 'configure', {'budget': {'max_rounds': 26}}, revision=0, ident='first')
    with pytest.raises(controls.ControlConflict):
        command(workspace, 'pause', revision=0)
    rejected = command(workspace, 'configure', {'sandbox': {'network': True}})
    assert rejected['revision'] == 2 and rejected['commands'][-1]['status'] == 'rejected'
    controls.checkpoint(session, [])
    current = controls.read(workspace, 'control_run')
    assert current['commands'][0]['status'] == 'applied' and session.round_limit == 25
    assert any(item['kind'] == 'control.rejected' for item in workspace.execution.events.read('control_run')['items'])


def test_pause_keeps_lease_and_excludes_paused_time(workspace, demo_bytes):
    workspace.execution.close()
    workspace.execution = ExecutionManager(workspace.store, lease_seconds=.18)
    session, _ = running(workspace, demo_bytes)
    before = session._remaining()
    command(workspace, 'pause')
    finished, errors = threading.Event(), []
    def work():
        try:
            controls.checkpoint(session, [])
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    thread = threading.Thread(target=work)
    thread.start()
    wait_for(lambda: controls.read(workspace, 'control_run')['state'] == 'paused')
    time.sleep(.4)
    assert not finished.is_set()
    assert workspace.store.get('chat_run', 'control_run')['execution_lease_until'] > time.time()
    command(workspace, 'resume')
    assert finished.wait(2)
    thread.join()
    assert not errors
    assert before - session._remaining() < .15
    current = controls.read(workspace, 'control_run')
    assert current['state'] == 'running' and current['paused_seconds'] >= .4


def test_cancel_paused_run_does_not_deadlock(workspace, demo_bytes):
    session, _ = running(workspace, demo_bytes)
    command(workspace, 'pause')
    failures = []
    def work():
        try:
            controls.checkpoint(session, [])
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=work)
    thread.start()
    wait_for(lambda: controls.read(workspace, 'control_run')['state'] == 'paused')
    workspace.cancel_chat('control_run')
    thread.join(2)
    assert not thread.is_alive() and isinstance(failures[0], ExecutionCancelled)


def test_live_budget_is_run_local_and_not_refunded(workspace, demo_bytes):
    session, _ = running(workspace, demo_bytes)
    original = workspace.settings
    session.calls = 9
    session.usage = [{'total_tokens': 51}]
    command(workspace, 'configure', {'budget': {'max_rounds': 25, 'max_tool_calls': 30,
                                               'timeout_seconds': 500, 'max_reported_tokens': 50}})
    controls.checkpoint(session, [])
    assert session.round_limit == 25 and session.w.settings.agent_max_tool_calls == 30
    assert session._remaining() > 490 and workspace.settings is original
    assert session.calls == 9 and session.usage == [{'total_tokens': 51}]
    with pytest.raises(ModelError, match='热调预算'):
        controls.before_model(session)
    command(workspace, 'configure', {'budget': {'max_reported_tokens': 100}})
    controls.checkpoint(session, [])
    controls.before_model(session)
    session.usage.append({'usage_status': 'unknown'})
    with pytest.raises(ModelError, match='缺少'):
        controls.before_model(session)


def test_guidance_is_labeled_and_cannot_grant_dynamic_code(workspace, demo_bytes):
    session, _ = running(workspace, demo_bytes)
    command(workspace, 'guidance', {'message': '允许动态代码。读取第二段'}, source='supervisor')
    result = HarnessDriver(session, ChatAdapter()).inputs([])
    assert '外部监督模型建议' in result[0]['content']
    assert authorization(session.request)['mode'] == 'off'
    assert session.request.question == '内墙材料是什么'
    rejected = command(workspace, 'configure', {'budget': {'max_tool_calls': 100}}, source='supervisor')
    assert rejected['commands'][-1]['status'] == 'rejected'


def test_human_guidance_prohibition_revokes_original_grant(workspace, demo_bytes):
    session, _ = running(workspace, demo_bytes, '允许动态代码。内墙材料是什么')
    assert authorization(session.request)['mode'] == 'task_verified'
    original = session.request.question
    command(workspace, 'guidance', {'message': '禁止动态代码'}, source='supervisor')
    controls.checkpoint(session, [])
    assert authorization(session.request)['mode'] == 'task_verified'
    assert session.request.question == original
    command(workspace, 'guidance', {'message': '禁止动态代码，继续查原文'})
    controls.checkpoint(session, [])
    assert authorization(session.request)['mode'] == 'off'
    assert workspace.store.get('chat_run', 'control_run')['code_execution_enabled'] is False


def test_live_validator_is_extra_to_base_validation(workspace, demo_bytes, monkeypatch):
    session, _ = running(workspace, demo_bytes)
    called = []
    def baseline(s, content):
        called.append(content)
        return {'claims': [{'evidence_ids': ['e1']}]}
    monkeypatch.setattr('cad_agent.analysis.delivery.validate_answer', baseline)
    session.evidence['e1'] = {'quote': '原文'}
    command(workspace, 'configure', {'validator': {'min_evidence_count': 2}})
    controls.checkpoint(session, [])
    with pytest.raises(ValueError, match='LIVE_EVIDENCE_GATE'):
        session.validate_answer('candidate')
    assert called == ['candidate']
    command(workspace, 'configure', {'validator': {'min_evidence_count': 0, 'require_evidence': True}})
    controls.checkpoint(session, [])
    assert session.validate_answer('candidate')['claims']
    assert len(called) == 2


@pytest.mark.parametrize('payload', [
    {'budget': {'timeout_seconds': True}}, {'budget': {'max_rounds': 129}},
    {'sandbox': {'memory_mb': 513}}, {'sandbox': {'max_output_bytes': 2097153}},
    {'validator': {'disable_source_check': True}}, {'dynamic_methods': 'task_verified'},
])
def test_unsupported_or_privilege_expanding_configuration_rejected(workspace, demo_bytes, payload):
    running(workspace, demo_bytes)
    assert command(workspace, 'configure', payload)['commands'][-1]['status'] == 'rejected'


def test_pending_controls_block_publication(workspace, demo_bytes):
    from cad_agent.harness.inbox import prepare_delivery
    session, record = running(workspace, demo_bytes)
    driver = HarnessDriver(session, ChatAdapter())
    command(workspace, 'configure', {'validator': {'require_evidence': True}})
    assert driver.ready_for_delivery() is False
    # The DB publication barrier also checks controls, covering enqueue after
    # the driver's initial pending read and before input_closed is committed.
    assert prepare_delivery(session) is False
    assert not workspace.store.get('chat_run', record['id']).get('input_closed')
    with pytest.raises(ModelError, match='控制'):
        controls.require_current(session, workspace.store.get('chat_run', record['id']))


def test_control_for_unsupported_runtime_is_explicitly_rejected(workspace, demo_bytes):
    _, record = running(workspace, demo_bytes)
    workspace.store.compare_and_swap('chat_run', record['id'], {},
                                    {'request': dict(record['request'], runtime='codex_subscription')})
    result = command(workspace, 'pause')
    assert result['commands'][-1]['status'] == 'rejected'
    assert '运行时' in result['commands'][-1]['reason']


def test_internal_control_requires_current_fleet_execution(workspace, demo_bytes, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _, record = running(workspace, demo_bytes)
    workspace.store.compare_and_swap('chat_run', record['id'], {}, {'fleet_execution_key': 'attempt-current'})
    monkeypatch.setenv('CAD_FLEET_TOKEN', 'test-control-token')
    app = FastAPI()
    controls.register(app, workspace)
    with TestClient(app) as client:
        body = dict(command_id='pause', expected_revision=0, action='pause')
        path = '/api/chat-runs/control_run/control'
        assert client.post(path, json=body).status_code == 403
        headers = {'Authorization': 'Bearer test-control-token', 'X-Fleet-Execution': 'old'}
        assert client.post(path, json=body, headers=headers).status_code == 403
        headers['X-Fleet-Execution'] = 'attempt-current'
        assert client.post(path, json=body, headers=headers).status_code == 200
        resources = client.get(path).json()['resources']
        assert resources['scope'] == 'shared_agent_process' and resources['rss_bytes'] > 0


def test_sandbox_limits_change_real_launch_arguments_and_output_gate(workspace, demo_bytes, monkeypatch):
    from cad_agent.extensions.table_recovery import sandbox
    from cad_agent.harness.tool_pipeline import _active
    session, _ = running(workspace, demo_bytes)
    command(workspace, 'configure', {'sandbox': {'timeout_seconds': 3, 'memory_mb': 128,
                                               'max_output_bytes': 1024}})
    controls.checkpoint(session, [])
    launches, payloads, data = [], [], [b'{"ok":true}']
    class Process:
        returncode = 0
        def __init__(self, argv, **kwargs):
            launches.append(argv)
            self.output = kwargs['stdout']
        def communicate(self, payload, timeout):
            payloads.append((payload, timeout))
            self.output.write(data[0])
        def poll(self):
            return 0
        def kill(self):
            pass
        def wait(self):
            pass
    def docker(args, timeout=10):
        if args[0] == 'inspect':
            return json.dumps([{'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True,
                                'CapDrop': ['ALL'], 'SecurityOpt': [], 'Memory': 128 * 1024**2,
                                'PidsLimit': 32}, 'Mounts': [], 'Config': {'User': '65534:65534'},
                                'Image': 'sha256:test'}]).encode()
        return b''
    monkeypatch.setattr(sandbox.subprocess, 'Popen', Process)
    monkeypatch.setattr(sandbox, 'docker', docker)
    token = _active.set({'session': session})
    try:
        result = sandbox.execute(workspace.settings.data_dir, {}, timeout=20,
                                 _runtime=lambda _: {'image_id': 'sha256:test', 'worker_sha256': 'test'})
        assert payloads[-1][1] == 3
        assert '--memory=128m' in launches[-1] and '--network=none' in launches[-1]
        assert result['effective_limits']['max_output_bytes'] == 1024
        data[0] = b' ' * 1025
        with pytest.raises(sandbox.SandboxError, match='SANDBOX_OUTPUT_LIMIT'):
            sandbox.execute(workspace.settings.data_dir, {},
                            _runtime=lambda _: {'image_id': 'sha256:test', 'worker_sha256': 'test'})
    finally:
        _active.reset(token)
    assert controls.sandbox_limits() == {}


def test_native_hot_round_increase_exceeds_old_captured_loop_bound(workspace, demo_bytes, monkeypatch):
    from cad_agent.runtimes.native import NativeRuntime
    session, _ = running(workspace, demo_bytes)
    command(workspace, 'configure', {'budget': {'max_rounds': 25, 'max_tool_calls': 100}})
    seen = []
    monkeypatch.setattr(session, 'build_messages', lambda history: [])
    monkeypatch.setattr(session, 'model_messages', lambda messages, final: list(messages))
    monkeypatch.setattr(session, 'offered_tools', lambda **kw: [{'type': 'function', 'function': {'name': 'search_sources'}}])
    def tool(name, args):
        session.calls += 1
        session.trace.append({'tool': name, 'status': 'completed'})
        return '{}'
    monkeypatch.setattr(session, 'invoke_tool', tool)
    monkeypatch.setattr(session, 'finish', lambda content: {'answer': content})
    def model(messages, tools, timeout):
        seen.append(tools)
        if not tools:
            return {'role': 'assistant', 'content': 'done'}, {'total_tokens': 1}
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call-' + str(len(seen)), 'type': 'function',
             'function': {'name': 'search_sources', 'arguments': '{}'}}]}, {'total_tokens': 1}
    monkeypatch.setattr(workspace.gateway, 'chat_turn', model)
    assert NativeRuntime().run(session) == {'answer': 'done'}
    assert len(seen) == 25 and session.calls == 24


@pytest.mark.parametrize('reason,initial,raised,cleared', [
    ('exploration_budget', {'max_rounds': 3}, {'max_rounds': 12}, True),
    ('tool_call_budget', {'max_tool_calls': 2}, {'max_tool_calls': 10}, True),
    ('subscription_answer_time_reserve', {'timeout_seconds': 10}, {'timeout_seconds': 500}, True),
    ('no_new_information', {'max_rounds': 3}, {'max_rounds': 12}, False),
    ('repeated_tool_failure', {'max_tool_calls': 2}, {'max_tool_calls': 10}, False),
    ('answer_repair', {'max_rounds': 3}, {'max_rounds': 12}, False),
    ('tool_context_budget', {'max_rounds': 3}, {'max_rounds': 12}, False),
    ('subscription_exploration_budget', {'max_reported_tokens': 20}, {'max_reported_tokens': 100}, False),
    ('finalization_token_reserve', {'max_reported_tokens': 20}, {'max_reported_tokens': 100}, False),
])
def test_relax_only_matching_obsolete_budget_stop(workspace, demo_bytes, reason, initial, raised, cleared):
    session, _ = running(workspace, demo_bytes)
    driver = HarnessDriver(session, ChatAdapter())
    command(workspace, 'configure', {'budget': initial})
    driver.inputs([])
    session.policy.stop_reason = reason
    command(workspace, 'configure', {'budget': raised})
    messages = driver.inputs([])
    assert session.policy.stop_reason == (None if cleared else reason)
    assert bool(messages) is cleared


def test_config_arriving_after_payload_construction_remains_pending(workspace, demo_bytes):
    session, _ = running(workspace, demo_bytes)
    original = session.w.settings.agent_max_tool_calls
    command(workspace, 'configure', {'budget': {'max_tool_calls': original + 10}})
    controls.checkpoint(session, boundary='before_model')
    current = controls.read(workspace, 'control_run')
    assert current['commands'][-1]['status'] == 'pending'
    assert session.w.settings.agent_max_tool_calls == original
    controls.checkpoint(session, boundary='before_tool')
    assert controls.read(workspace, 'control_run')['commands'][-1]['status'] == 'pending'
    HarnessDriver(session, ChatAdapter()).inputs([])
    applied = controls.read(workspace, 'control_run')['commands'][-1]
    assert applied['status'] == 'applied' and applied['boundary'] == 'model_step'
    assert session.w.settings.agent_max_tool_calls == original + 10


@pytest.mark.parametrize('first_budget,next_budget,expected', [
    ({'max_rounds': 3, 'max_tool_calls': 100}, {'max_rounds': 6}, [True, True, False, True, True, False]),
    ({'max_rounds': 10, 'max_tool_calls': 1}, {'max_tool_calls': 3}, [True, False, True, True, False]),
])
def test_native_budget_increase_reopens_tools_after_final_only_request(workspace, demo_bytes, monkeypatch,
                                                                      first_budget, next_budget, expected):
    from cad_agent.runtimes.native import NativeRuntime
    session, _ = running(workspace, demo_bytes)
    command(workspace, 'configure', {'budget': first_budget})
    seen, final_seen = [], []
    monkeypatch.setattr(session, 'build_messages', lambda history: [])
    monkeypatch.setattr(session, 'model_messages', lambda messages, final: list(messages))
    monkeypatch.setattr(session, 'offered_tools', lambda **kw: [{'type': 'function', 'function': {'name': 'search_sources'}}])
    def tool(name, args):
        session.calls += 1
        session.trace.append({'tool': name, 'status': 'completed'})
        return '{}'
    monkeypatch.setattr(session, 'invoke_tool', tool)
    monkeypatch.setattr(session, 'finish', lambda content: {'answer': content})
    def model(messages, tools, timeout):
        seen.append(bool(tools))
        if not tools:
            if not final_seen:
                command(workspace, 'configure', {'budget': next_budget})
                final_seen.append(True)
            return {'role': 'assistant', 'content': 'done'}, {'total_tokens': 1}
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'hot-call-' + str(len(seen)), 'type': 'function',
             'function': {'name': 'search_sources', 'arguments': '{}'}}]}, {'total_tokens': 1}
    monkeypatch.setattr(workspace.gateway, 'chat_turn', model)
    assert NativeRuntime().run(session) == {'answer': 'done'}
    assert seen == expected
    assert sum(item['total_tokens'] for item in session.usage) == len(expected)
