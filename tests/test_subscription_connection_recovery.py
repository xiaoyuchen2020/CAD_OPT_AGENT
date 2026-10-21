"""Only trace-proven, unsent connection failures may reconnect without usage debt."""
import json
import ssl
from types import SimpleNamespace

import httpx
import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from scripts.run_controlled_batch import infrastructure_failure_streak


def setup_transport(monkeypatch, handler):
    s = SimpleNamespace(_remaining=lambda: 60, usage=[], trace=[], system_prompt='',
        task=SimpleNamespace(data={'subtasks': []}),
        w=SimpleNamespace(settings=SimpleNamespace(model='gpt-6.1-sol')),
        runtime_diagnostics={'model_requests': [{}]}, progress=lambda **kw: None)
    t = ResponsesTransport(SimpleNamespace(access_token=lambda: 'private-fixture-token'))
    monkeypatch.setattr(t, 'client', lambda timeout: httpx.Client(
        transport=httpx.MockTransport(handler), timeout=timeout, trust_env=False))
    return s, t


def completed():
    response = dict(status='completed', model='gpt-6.1-sol', output=[],
                    usage=dict(input_tokens=10, output_tokens=5, total_tokens=15))
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
        text='data: '+json.dumps(dict(type='response.completed', response=response))+'\n\n')


def connection_error(req, phase='start_tls', *, certificate=False):
    trace = req.extensions['trace']
    trace('connection.'+phase+'.started', {'private': 'private-fixture-token'})
    trace('connection.'+phase+'.failed', {'exception': 'secret-error-text'})
    if certificate:
        raise httpx.ConnectError('secret-error-text') from ssl.SSLCertVerificationError('private')
    raise httpx.ConnectError('secret-error-text')


@pytest.mark.parametrize('phase', ['connect_tcp', 'start_tls'])
def test_unsent_failure_reconnects_without_debt_and_keeps_safe_diagnostics(monkeypatch, phase):
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1: connection_error(req, phase)
        return completed()
    s, t = setup_transport(monkeypatch, handler)
    t.request(s, {'model':'gpt-6.1-sol', 'input':[{'type':'input_image','image_url':'fixture'}]})
    a, b = s.runtime_diagnostics['model_requests'][0]['transport_attempts']
    assert len(calls) == 2 and a['retry_reason'] == 'connection_failed_before_send'
    assert a['usage_status'] == 'not_requested' and a['reserved_tokens'] == 0
    assert not a['request_started'] and not a['unknown_usage']
    assert a['network_failure_phase'] == 'connection.'+phase
    assert a['exception_type'] == 'ConnectError' and a['configured_timeout_seconds']['connect'] == 60
    assert b['status'] == 'completed' and s.usage[0]['total_tokens'] == 15
    assert not s.runtime_diagnostics['unknown_usage']
    assert 'secret-error-text' not in json.dumps(s.runtime_diagnostics)
    assert 'private-fixture-token' not in json.dumps(s.runtime_diagnostics)


def test_reconnect_limit_and_probe_single_attempt(monkeypatch):
    calls = []
    def handler(req):
        calls.append(req); connection_error(req)
    s, t = setup_transport(monkeypatch, handler)
    with pytest.raises(ModelError): t.request(s, {}, max_attempts=1)
    assert len(calls) == 1 and not s.runtime_diagnostics['unknown_usage']
    s, t = setup_transport(monkeypatch, handler)
    with pytest.raises(ModelError): t.request(s, {})
    assert len(calls) == 3 and not s.runtime_diagnostics['unknown_usage']


@pytest.mark.parametrize('mode', ['read_timeout', 'missing_trace', 'sent_headers', 'certificate'])
def test_ambiguous_sent_and_certificate_failures_do_not_retry(monkeypatch, mode):
    calls = []
    def handler(req):
        calls.append(req)
        if mode == 'certificate': connection_error(req, certificate=True)
        if mode == 'read_timeout': raise httpx.ReadTimeout('private')
        if mode == 'sent_headers':
            req.extensions['trace']('http11.send_request_headers.started', {})
            connection_error(req)
        raise httpx.ConnectError('private')
    s, t = setup_transport(monkeypatch, handler)
    with pytest.raises(ModelError): t.request(s, {})
    assert len(calls) == 1
    d = s.runtime_diagnostics['model_requests'][0]['transport']
    assert not d['retry_candidate'] and d['unknown_usage']
    assert d['certificate_verification_failed'] is (mode == 'certificate')


def test_single_probe_does_not_retry_service_rejection(monkeypatch):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(503, json={'error':{'code':'server_error'}})
    s, t = setup_transport(monkeypatch, handler)
    with pytest.raises(ModelError): t.request(s, {}, max_attempts=1)
    assert len(calls) == 1 and s.runtime_diagnostics['unknown_usage']


def test_batch_streak_resets_after_recovery_and_ignores_answer_quality():
    streak = 0
    for _ in range(3):
        streak = infrastructure_failure_streak(streak, {'error':{'code':'SIWC_TRANSPORT_ERROR'}})
    assert streak == 3
    assert infrastructure_failure_streak(streak, {'status':'completed'}) == 0
    assert infrastructure_failure_streak(streak, {'error':{'code':'SIWC_ANSWER_VALIDATION'}}) == 0


@pytest.mark.parametrize('failed', [False, True])
def test_probe_has_complete_budget_contract_and_failure_exit_status(monkeypatch, tmp_path, failed):
    import scripts.check_siwc_transport as probe
    from contextlib import nullcontext
    account = SimpleNamespace(lease=lambda: nullcontext(), close=lambda: None)
    monkeypatch.setattr(probe, 'SiwcAccount', lambda data: account)
    def request(self, session, payload, *, max_attempts):
        assert session.task.data == {'subtasks': []} and session.system_prompt == ''
        assert max_attempts == 1
        if failed: raise ModelError('SIWC_TRANSPORT_ERROR', 'fixture')
        return {'model': payload['model']}
    monkeypatch.setattr(probe.ResponsesTransport, 'request', request)
    monkeypatch.setattr(probe.sys, 'argv', ['probe', '--data-dir', str(tmp_path/'data'),
        '--output', str(tmp_path/'out'), '--execute'])
    assert probe.main() == (1 if failed else 0)
    result = json.loads((tmp_path/'out/result.json').read_text())
    assert result['status'] == ('failed' if failed else 'completed')
    assert not result['paid_api_fallback']
