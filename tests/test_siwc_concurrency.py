"""Local-only concurrency acceptance. Never reads a live subscription account."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import httpx
import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.adapters.siwc import SiwcAccount, SCOPES
from cad_agent.execution import ExecutionCancelled


def seed(path, capacity=1):
    account = SiwcAccount(path, capacity)
    with account.storage():
        account.save({'host_id': 'fixture', 'active': 'fixture', 'accounts': {'fixture': {
            'label': 'fixture', 'access_token': 'fixture', 'refresh_token': 'fixture',
            'client_id': 'fixture', 'subject': 'fixture', 'scopes': SCOPES.split(), 'expires_at': time.time()+3600}}})
    account.configure_concurrency(capacity)
    return account


def process_lease(path, capacity, entered, release):
    try:
        with SiwcAccount(path, capacity).queued_lease(3):
            entered.put('entered')
            deadline = time.monotonic()+10
            while not Path(release).exists() and time.monotonic() < deadline: time.sleep(.02)
    except ModelError as error: entered.put(error.code)


@pytest.mark.parametrize('capacity', [1, 2])
def test_threads_share_slots_and_release_on_exception(tmp_path, capacity):
    account = seed(tmp_path, capacity)
    live = peak = 0
    lock = threading.Lock()
    def work(i):
        nonlocal live, peak
        other = SiwcAccount(tmp_path, capacity)
        try:
            with other.queued_lease(3):
                with lock: live += 1; peak = max(live, peak)
                try:
                    time.sleep(.04)
                    if i == 1: raise ValueError('fixture failure')
                finally:
                    with lock: live -= 1
        except ValueError: pass
    with ThreadPoolExecutor(max_workers=6) as pool: list(pool.map(work, range(6)))
    assert peak == capacity
    assert account.status()['active_runs'] == 0
    with account.lease(): assert account.status()['active_runs'] == 1


@pytest.mark.parametrize('capacity', [1, 2])
def test_process_limit_config_conflict_and_killed_process_release(tmp_path, capacity):
    account = seed(tmp_path, capacity)
    ctx = multiprocessing.get_context('spawn')
    entered, release = ctx.Queue(), str(tmp_path / 'release')
    workers = [ctx.Process(target=process_lease, args=(str(tmp_path), capacity, entered, release)) for _ in range(capacity)]
    try:
        for p in workers: p.start()
        for _ in workers: assert entered.get(timeout=10) == 'entered'
        assert account.status()['active_runs'] == capacity
        with pytest.raises(ModelError) as error:
            with account.lease(): pass
        assert error.value.code == 'SIWC_BUSY'
        conflict = ctx.Process(target=process_lease, args=(str(tmp_path), 3-capacity, entered, release))
        conflict.start(); assert entered.get(timeout=10) == 'SIWC_CONFIG_CONFLICT'; conflict.join(10)
        with pytest.raises(ModelError): account.configure_concurrency(3-capacity)
        workers[0].terminate(); workers[0].join(10)
        with account.lease(): pass
    finally:
        Path(release).touch()
        for p in workers:
            p.join(10)
            if p.is_alive(): p.kill(); p.join()
    assert account.status()['active_runs'] == 0


def test_identity_mutations_fenced_and_legacy_lock_compatible(tmp_path):
    account = seed(tmp_path, 2); other = SiwcAccount(tmp_path, 2)
    with account.lease():
        for operation in (other.login, other.logout, lambda:other.callback({}), lambda:other.configure_concurrency(1)):
            with pytest.raises(ModelError) as error: operation()
            assert error.value.code == 'SIWC_BUSY'
        assert other.status()['connected']
        with (account.home / 'run.lock').open('a') as legacy:
            with pytest.raises(BlockingIOError): fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (account.home / 'run.lock').open('a') as legacy:
        fcntl.flock(legacy, fcntl.LOCK_EX)
        with pytest.raises(ModelError) as error:
            with account.lease(): pass
        assert error.value.code == 'SIWC_BUSY'
    with account.storage(), account.identity_change():
        # gate is held without storage for this direct test of the atomic fence
        with (account.home / 'run.lock').open('a') as other_gate:
            with pytest.raises(BlockingIOError): fcntl.flock(other_gate, fcntl.LOCK_SH | fcntl.LOCK_NB)


def test_queue_timeout_cancel_and_success(tmp_path):
    account = seed(tmp_path)
    with account.lease():
        metadata = {}
        with pytest.raises(ModelError) as error:
            with account.queued_lease(.06, diagnostics=metadata): pass
        assert error.value.code == 'SIWC_QUEUE_TIMEOUT' and metadata['queue_seconds'] >= .06
        assert metadata['acquired'] is False
        def cancel(): raise ExecutionCancelled('cancelled')
        with pytest.raises(ExecutionCancelled):
            with account.queued_lease(3, cancel): pass
        assert account.status()['active_runs'] == 1
    with account.queued_lease(1) as lease:
        assert lease['queue_seconds'] < .05 and lease['max_concurrency'] == 1


def test_refresh_is_serial_across_instances(tmp_path, monkeypatch):
    a = seed(tmp_path, 2); b = SiwcAccount(tmp_path, 2)
    with a.storage():
        data = a.load(); data['accounts']['fixture']['expires_at'] = 0; a.save(data)
    calls = []
    def handle(request):
        calls.append(request); time.sleep(.05)
        return httpx.Response(200, json={'access_token': 'refreshed', 'refresh_token': 'rotated',
             'expires_in': 3600, 'token_type': 'Bearer', 'scope': SCOPES})
    for account in (a, b): monkeypatch.setattr(account, 'client', lambda:httpx.Client(transport=httpx.MockTransport(handle)))
    def token(account):
        with account.queued_lease(2): return account.access_token()
    with ThreadPoolExecutor(max_workers=2) as pool: assert list(pool.map(token, [a, b])) == ['refreshed', 'refreshed']
    assert len(calls) == 1


def test_runtime_slot_wait_excluded_from_inference_deadline(workspace, demo_bytes, monkeypatch):
    from test_siwc import make_session, response
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    session = make_session(workspace, demo_bytes)
    account = seed(workspace.settings.data_dir)
    workspace.siwc_account = account; session.w.siwc_account = account
    session.started = time.monotonic()-100
    original_started = session.started
    def request(self, current, payload):
        assert current._remaining() > current.w.settings.agent_timeout_seconds-101
        raise ModelError('FIXTURE_STOP', 'local fixture stop')
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    lease = account.lease(); lease.__enter__()
    releaser = threading.Thread(target=lambda:(time.sleep(.15), lease.__exit__(None,None,None)))
    releaser.start()
    with pytest.raises(ModelError) as error: session.run()
    releaser.join()
    assert error.value.code == 'FIXTURE_STOP'
    queue = session.runtime_diagnostics['concurrency']['queue_seconds']
    assert queue >= .1 and session.started-original_started == pytest.approx(queue)
    assert session.runtime_diagnostics['concurrency']['acquired'] is True
    assert not session.usage and account.status()['active_runs'] == 0


@pytest.mark.parametrize("timeout", [float('nan'), float('inf'), -1])
def test_invalid_queue_timeout_rejected(tmp_path, timeout):
    with pytest.raises(ValueError):
        with seed(tmp_path).queued_lease(timeout): pass
