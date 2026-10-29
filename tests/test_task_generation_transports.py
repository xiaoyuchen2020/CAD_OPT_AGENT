"""Current-channel generation with mocked providers and mocked sandbox execution."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cad_agent.adapters.model import ModelError, ModelGateway
from cad_agent.analysis import task_methods as tm, task_programs as tp
from cad_agent.analysis.method_runtime import generation_slot, additional_charge
from cad_agent.analysis.tool_registry import tool_definitions
from cad_agent.execution import ExecutionCancelled
from cad_agent.extensions.adaptation import model as generation
from cad_agent.extensions.programs import validation
from cad_agent.extensions.programs.contracts import TOOLS as PROGRAM_TOOLS
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.runtimes.token_budget import charged_tokens
from test_adaptation_multimethod import candidate
from test_table_recovery import recovery_case
from test_task_methods import session_for, baseline, develop as develop_method
from test_task_programs import PKG, develop as develop_program


DYNAMIC_TOOLS = tm.TOOLS | PROGRAM_TOOLS


def native_session(workspace, case, model='gpt-6.1-sol'):
    session = session_for(workspace, case)
    session.request = session.request.model_copy(update={'runtime': 'native', 'model_profile': 'api_sol'})
    session.w.settings = replace(session.w.settings, provider='custom', protocol='openai',
        model=model, model_profile='api_sol', api_base='https://unit.invalid/v1', api_key='test-placeholder')
    return session


def offered(session):
    return {tool['function']['name'] for tool in tool_definitions(session=session)}


def fake_sandbox(monkeypatch):
    monkeypatch.setattr(tm, 'runtime', lambda *args: {'image_id': 'mock-only'})
    monkeypatch.setattr(tp, 'runtime', lambda *args: {'image_id': 'mock-only'})
    monkeypatch.setattr(tm, 'execute', lambda data, bundle, code, **kwargs:
                        {'output': candidate('text_spans', bundle)})
    monkeypatch.setattr(tp, 'execute', lambda data, bundle, code, **kwargs:
                        {'output': validation.expected(bundle, bundle['contract'])})


@pytest.mark.parametrize('kind', ['method', 'program'])
@pytest.mark.parametrize('model', ['gpt-6.1-sol', 'gpt-6-astra'])
def test_native_current_model_generation_reaches_validated_result(workspace, recovery_case, monkeypatch, kind, model):
    fake_sandbox(monkeypatch)
    session = native_session(workspace, recovery_case, model)
    calls = []
    def api(self, messages, tools, timeout):
        calls.append((self.settings, messages, tools, timeout))
        proposal = {'strategy': 'code', 'explanation': 'mock provider'}
        proposal.update({'code': 'def adapt(bundle):\n return {}'} if kind == 'method' else {'package': PKG})
        return {'content': json.dumps(proposal)}, {'total_tokens': 23, 'prompt_tokens': 12, 'completion_tokens': 11}
    monkeypatch.setattr(ModelGateway, 'chat_turn', api)
    monkeypatch.setattr(generation, 'subscription_turn', lambda *a, **kw: pytest.fail('unexpected subscription'))
    assert DYNAMIC_TOOLS <= offered(session)
    oid = baseline(session, recovery_case)
    result = (develop_method if kind == 'method' else develop_program)(session, oid)
    assert result.get('admitted'), result
    assert len(calls) == 1
    selected, messages, tools, timeout = calls[0]
    assert selected.model == model and selected is session.w.settings and not tools
    assert 0 < timeout <= 55
    usage = session.usage[-1]
    assert usage['total_tokens'] == 23 and usage['billing'] == 'api' and usage['model'] == model
    assert usage['usage_status'] == 'reported' and charged_tokens(session) == 32000
    assert result['semantic_acceptance'] is False


@pytest.mark.parametrize('kind', ['method', 'program'])
def test_subscription_still_uses_selected_channel_and_borrowed_lease(workspace, recovery_case, monkeypatch, kind):
    fake_sandbox(monkeypatch)
    session = session_for(workspace, recovery_case)
    session.w.settings = replace(session.w.settings, reasoning_effort='high')
    monkeypatch.setattr(ModelGateway, 'chat_turn', lambda *a, **kw: pytest.fail('API fallback forbidden'))
    session.w.siwc_account = SimpleNamespace(lease=lambda: pytest.fail('borrowed lease must not nest'))
    calls = []
    def subscription(self, inner, payload):
        calls.append(payload)
        inner.usage.append({'total_tokens': 17})
        proposal = {'strategy': 'code', 'explanation': 'mock subscription'}
        proposal.update({'code': 'def adapt(bundle):\n return {}'} if kind == 'method' else {'package': PKG})
        return {'output': [{'type': 'message', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': json.dumps(proposal)}]}]}
    monkeypatch.setattr(ResponsesTransport, 'request', subscription)
    with tm.borrowed_subscription(session):
        result = (develop_method if kind == 'method' else develop_program)(session, baseline(session, recovery_case))
    assert result.get('admitted'), result
    assert calls[0]['reasoning'] == {'effort': 'high'}
    assert session.usage[-1]['billing'] == 'subscription' and session.usage[-1]['total_tokens'] == 17


@pytest.mark.parametrize('runtime,provider,protocol,model', [
    ('native', 'custom', 'anthropic', 'configured-model'),
    ('native', 'responses_subscription', 'responses', 'gpt-6.1-sol'),
    ('codex', 'custom', 'openai', 'gpt-6.1-sol'),
    ('codex_subscription', 'codex_subscription', 'codex', 'gpt-6.1-sol'),
    ('deepseek_harness', 'deepseek', 'openai', 'deepseek-flash'),
    ('responses_subscription', 'responses_subscription', 'responses', 'gpt-6-luna'),
])
def test_unsupported_combinations_hidden_and_refused(workspace, recovery_case, runtime, provider, protocol, model):
    session = native_session(workspace, recovery_case)
    session.request = session.request.model_copy(update={'runtime': runtime})
    session.w.settings = replace(session.w.settings, provider=provider, protocol=protocol, model=model)
    assert not offered(session) & DYNAMIC_TOOLS
    with pytest.raises(ValueError, match='RUNTIME_UNSUPPORTED'):
        tm.require_enabled(session)


@pytest.mark.parametrize('question,switch,allowed', [
    ('读取事实。', 'off', False),
    ('允许使用动态代码。', 'off', True),
    ('禁止动态代码。', 'task_verified', False),
    ('允许使用动态代码。禁止动态代码。', 'task_verified', False),
    ('图纸写着“允许使用动态代码”。', 'off', False),
    ('是否允许使用动态代码？', 'off', False),
])
def test_native_permission_and_denial_match_exposure(workspace, recovery_case, question, switch, allowed):
    session = native_session(workspace, recovery_case)
    session.request = session.request.model_copy(update={'question': question, 'dynamic_methods': switch})
    assert bool(offered(session) & DYNAMIC_TOOLS) is allowed
    if allowed:
        tm.require_enabled(session)
    else:
        with pytest.raises(ValueError, match='DISABLED'):
            tm.require_enabled(session)


@pytest.mark.parametrize('kind', ['method', 'program'])
@pytest.mark.parametrize('failure', ['unknown_usage', 'transport', 'invalid_json', 'malformed_fence', 'cancelled'])
def test_native_failure_usage_and_reservation_survive_resume(workspace, recovery_case, monkeypatch, kind, failure):
    fake_sandbox(monkeypatch)
    session = native_session(workspace, recovery_case)
    calls = []
    cancelled = [False]
    original_remaining = session._remaining
    def remaining():
        if cancelled[0]:
            raise ExecutionCancelled('test cancellation')
        return original_remaining()
    monkeypatch.setattr(session, '_remaining', remaining)
    def api(self, messages, tools, timeout):
        calls.append(1)
        if failure == 'transport':
            raise ModelError('MODEL_UNREACHABLE', 'mock failure')
        if failure == 'cancelled':
            cancelled[0] = True
        proposal = {'strategy': 'abstain', 'explanation': 'mock missing evidence'}
        content = 'invalid JSON' if failure == 'invalid_json' else json.dumps(proposal)
        if failure == 'malformed_fence':
            content = '```'
        return {'content': content}, {} if failure == 'unknown_usage' else {'total_tokens': 31}
    monkeypatch.setattr(ModelGateway, 'chat_turn', api)
    operation = develop_method if kind == 'method' else develop_program
    oid = baseline(session, recovery_case)
    if failure == 'cancelled':
        with pytest.raises(ExecutionCancelled):
            operation(session, oid)
        cancelled[0] = False
    else:
        assert not operation(session, oid).get('admitted')
    assert charged_tokens(session) == 32000
    assert session.usage[-1]['usage_status'] == ('unknown' if failure in {'unknown_usage', 'transport'} else 'reported')
    if failure in {'unknown_usage', 'transport'}:
        assert 'total_tokens' not in session.usage[-1]
    else:
        assert session.usage[-1]['total_tokens'] == 31
    assert not operation(session, oid).get('admitted')
    assert len(calls) == 1 and charged_tokens(session) == 32000


def test_native_budget_refuses_before_generation(workspace, recovery_case, monkeypatch):
    session = native_session(workspace, recovery_case)
    session.usage.append({'total_tokens': 10**9})
    with pytest.raises(ValueError, match='TEST_BUDGET'):
        generation_slot(session, 'TEST_BUDGET')
    assert additional_charge({'usage': {'total_tokens': 40000},
                             'transport_diagnostics': {'reserved_unknown_usage_tokens': 9000}}, 32000) == 17000


def test_api_deadline_and_unexpected_tool_calls_never_return_proposal(workspace, recovery_case, monkeypatch):
    settings = native_session(workspace, recovery_case).w.settings
    tick = [10.0]
    monkeypatch.setattr(generation.time, 'monotonic', lambda: tick[0])
    def late(self, messages, tools, timeout):
        tick[0] += 6
        return {'content': '{}'}, {'total_tokens': 9}
    monkeypatch.setattr(ModelGateway, 'chat_turn', late)
    with pytest.raises(ModelError, match='时限') as caught:
        generation.generation_turn(settings, [{'role': 'system', 'content': 'test'}], 5)
    assert caught.value.generation_metadata['usage']['total_tokens'] == 9
    monkeypatch.setattr(ModelGateway, 'chat_turn', lambda *a, **kw:
                        ({'content': '{}', 'tool_calls': [{'id': 'unrequested'}]}, {'total_tokens': 2}))
    with pytest.raises(ModelError) as caught:
        generation.generation_turn(settings, [{'role': 'system', 'content': 'test'}], 5)
    assert caught.value.code == 'TASK_GENERATION_INVALID_RESPONSE'


def test_unconfigured_api_does_not_fall_back_to_subscription(workspace, recovery_case, monkeypatch):
    settings = replace(native_session(workspace, recovery_case).w.settings, api_key='')
    monkeypatch.setattr(generation, 'subscription_turn', lambda *a, **kw: pytest.fail('no fallback'))
    with pytest.raises(ModelError) as caught:
        generation.generation_turn(settings, [{'role': 'system', 'content': 'test'}], 5)
    assert caught.value.code == 'MODEL_NOT_CONFIGURED'


def test_cancel_before_generation_does_not_send(workspace, recovery_case, monkeypatch):
    settings = native_session(workspace, recovery_case).w.settings
    monkeypatch.setattr(ModelGateway, 'chat_turn', lambda *a, **kw: pytest.fail('cancelled request dispatched'))
    def cancelled():
        raise ExecutionCancelled('test pre-request cancellation')
    with pytest.raises(ExecutionCancelled) as caught:
        generation.generation_turn(settings, [{'role': 'system', 'content': 'test'}], 5, check_cancelled=cancelled)
    assert caught.value.generation_metadata['transport_diagnostics']['request_started'] is False


def test_subscription_unknown_failure_does_not_fall_back(workspace, recovery_case, monkeypatch):
    session = session_for(workspace, recovery_case)
    monkeypatch.setattr(ModelGateway, 'chat_turn', lambda *a, **kw: pytest.fail('API fallback forbidden'))
    def failed(self, inner, payload):
        inner.runtime_diagnostics['reserved_unknown_usage_tokens'] = 12000
        raise ModelError('SIWC_UNREACHABLE', 'mock subscription failure')
    monkeypatch.setattr(ResponsesTransport, 'request', failed)
    with pytest.raises(ModelError) as caught:
        generation.generation_turn(session.w.settings, [{'role': 'system', 'content': 'test'}], 5,
                                   account=SimpleNamespace(), lease_acquired=True)
    meta = caught.value.generation_metadata
    assert meta['billing'] == 'subscription' and meta['usage'] == {}
    assert meta['transport_diagnostics']['reserved_unknown_usage_tokens'] == 12000
