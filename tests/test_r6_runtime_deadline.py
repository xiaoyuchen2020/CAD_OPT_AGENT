"""Offline saved-failure reconstruction and real loopback slow-stream checks."""
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.execution import ExecutionCancelled
from cad_agent.runtimes.responses_subscription import ResponsesTransport


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'artifacts/designer-facts-b-final-20261019'


def session(seconds=3, cancellation=None):
    started = time.monotonic()
    def remaining():
        if cancellation is not None and cancellation.is_set():
            raise ExecutionCancelled('fixture cancellation')
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            raise ModelError('AGENT_TIME_LIMIT', 'fixture absolute deadline')
        return left
    return SimpleNamespace(_remaining=remaining, usage=[], trace=[], system_prompt='',
        task=SimpleNamespace(data={'subtasks': []}),
        w=SimpleNamespace(settings=SimpleNamespace(model='gpt-6.1-sol')),
        runtime_diagnostics={'model_requests': [{}]}, progress=lambda **kw: None)


def transport():
    return ResponsesTransport(SimpleNamespace(access_token=lambda: 'local-fixture-only'))


def diagnostic(s):
    return s.runtime_diagnostics['model_requests'][0]['transport']


@contextmanager
def local_server(monkeypatch, mode):
    stop = threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            requests.append(1)
            self.send_response(503 if mode == 'failure_body' else 200)
            self.send_header('Content-Type', 'application/json' if mode == 'failure_body' else 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                if mode == 'idle':
                    self.wfile.write(b': ready\n\n'); self.wfile.flush()
                    stop.wait(2)
                else:
                    # Frequent bytes defeat per-read idle timeout; no newline
                    # means iter_lines() never yields to the original guard.
                    while not stop.wait(0.02):
                        self.wfile.write(b' '); self.wfile.flush()
            except OSError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    monkeypatch.setattr('cad_agent.runtimes.responses_subscription.RESOURCE',
        f'http://127.0.0.1:{server.server_port}')
    try:
        yield requests
    finally:
        stop.set(); server.shutdown(); server.server_close(); thread.join(1)


@pytest.mark.parametrize('mode', ['idle', 'no_newline', 'failure_body'])
def test_real_http_deadline_interrupts_blocked_io(monkeypatch, mode, record_property):
    with local_server(monkeypatch, mode) as requests:
        s = session(seconds=0.3)
        tick = time.monotonic()
        with pytest.raises(ModelError) as caught:
            transport().request(s, {'model': 'gpt-6.1-sol'})
        elapsed = time.monotonic() - tick
    d = diagnostic(s)
    record_property('transport_elapsed_seconds', elapsed)
    record_property('configured_deadline_seconds', 0.3)
    assert caught.value.code == 'AGENT_TIME_LIMIT'
    assert 0.28 <= elapsed < 0.65
    assert len(requests) == 1 and not s.usage and not s.trace
    assert d['termination_reason'] == 'absolute_deadline'
    assert d['socket_shutdown'] and d['io_worker_exit_confirmed']
    assert d['unknown_usage'] and d['usage_status'] == 'unknown'
    assert s.runtime_diagnostics.get('automatic_retries', 0) == 0


def test_cancellation_interrupts_idle_stream_and_preserves_unknown_usage(monkeypatch, record_property):
    cancellation = threading.Event()
    timer = threading.Timer(0.15, cancellation.set)
    with local_server(monkeypatch, 'idle') as requests:
        s = session(seconds=5, cancellation=cancellation)
        timer.start(); tick = time.monotonic()
        try:
            with pytest.raises(ExecutionCancelled):
                transport().request(s, {})
        finally:
            timer.cancel()
        elapsed = time.monotonic() - tick
    d = diagnostic(s)
    record_property('transport_elapsed_seconds', elapsed)
    assert elapsed < 0.5 and len(requests) == 1
    assert d['status'] == 'cancelled' and d['termination_reason'] == 'cancelled'
    assert d['socket_shutdown'] and d['io_worker_exit_confirmed']
    assert d['usage_status'] == 'unknown' and not s.usage


def test_slow_headers_return_by_deadline_and_late_result_never_mutates_session(monkeypatch):
    release = threading.Event()
    closed = threading.Event()
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"type":"response.completed"}\n\n'
        def close(self):
            closed.set()
    def handle(request):
        release.wait(2)
        return httpx.Response(200, stream=Stream())
    monkeypatch.setattr(ResponsesTransport, 'client', lambda self, timeout:
        httpx.Client(transport=httpx.MockTransport(handle)))
    s = session(seconds=0.15); tick = time.monotonic()
    try:
        with pytest.raises(ModelError) as caught:
            transport().request(s, {})
        assert caught.value.code == 'AGENT_TIME_LIMIT' and time.monotonic()-tick < 0.4
        assert diagnostic(s)['io_cleanup_pending'] and not diagnostic(s)['socket_shutdown']
        before = json.dumps(s.runtime_diagnostics, sort_keys=True)
    finally:
        release.set()
    assert closed.wait(1)
    assert json.dumps(s.runtime_diagnostics, sort_keys=True) == before and not s.usage


@pytest.mark.parametrize('reason,expected', [
    ('max_output_tokens', 'max_output_tokens'), ('content_filter', 'content_filter'),
    ('private-content', 'other_redacted'), ({'private-content': True}, 'other_redacted'),
    (None, 'unknown')])
def test_incomplete_diagnostics_allowlist_and_no_partial_tool_release(monkeypatch, reason, expected):
    value = {'status': 'incomplete', 'model': 'gpt-6.1-sol', 'incomplete_details': {'reason': reason},
        'error': {'message': 'private-content'},
        'output': [{'type': 'function_call', 'name': 'plan_task', 'arguments': 'private-content'}]}
    body = 'data: ' + json.dumps({'type': 'response.incomplete', 'response': value}) + '\n\n'
    calls = []
    def handle(request):
        calls.append(1); return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, text=body)
    monkeypatch.setattr(ResponsesTransport, 'client', lambda self, timeout:
        httpx.Client(transport=httpx.MockTransport(handle)))
    s = session()
    with pytest.raises(ModelError) as caught:
        transport().request(s, {})
    assert caught.value.code == 'SIWC_RESPONSE_FAILED' and len(calls) == 1
    assert diagnostic(s)['incomplete_reason'] == expected and not s.trace and not s.usage
    assert 'private-content' not in json.dumps(s.runtime_diagnostics)


@pytest.mark.parametrize('question', ['Q01-B', 'Q06-B', 'Q13-B', 'Q16-B', 'Q18-B'])
def test_saved_incomplete_failure_shape_replay(monkeypatch, question):
    # Historical artifacts retain safe metadata, not SSE bodies. This explicitly
    # reconstructs the observed terminal shape; it cannot recover missing cause.
    saved = json.loads((BASELINE / f'attempts/{question}/0001/run.json').read_text())
    last = saved['runtime_diagnostics']['model_requests'][-1]['transport']
    assert last['terminal_event'] == 'response.incomplete'
    assert any(t['tool'] == 'plan_task' and t['status'] == 'failed' for t in saved['trace'])
    official = saved['usage'][-1]
    value = {'model': 'gpt-6.1-sol', 'status': 'incomplete', 'output': [],
        'usage': {'input_tokens': official['prompt_tokens'], 'output_tokens': official['completion_tokens'],
                  'total_tokens': official['total_tokens']}}
    body = 'data: ' + json.dumps({'type': last['terminal_event'], 'response': value}) + '\n\n'
    monkeypatch.setattr(ResponsesTransport, 'client', lambda self, timeout:
        httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, text=body))))
    s = session()
    with pytest.raises(ModelError) as caught:
        transport().request(s, {})
    assert caught.value.code == last['failure_code']
    assert diagnostic(s)['incomplete_reason'] == 'unknown'
    assert diagnostic(s)['usage_status'] == 'reported' and not diagnostic(s)['unknown_usage']
    assert s.usage[0]['total_tokens'] == official['total_tokens'] and not s.trace
    assert s.runtime_diagnostics.get('automatic_retries', 0) == 0
