"""Integration races must preserve the winning dispatch and every attempt."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event

import pytest

from cad_agent.exploration import build_job_payload
from test_fleet_rule_api import client, create_run, start


def start_url(run):
    return f"/api/fleet/exploration-runs/{run['run_id']}/cases/{run['cases'][0]['case_id']}/start"


def artifacts(active, tokens):
    content = '归档原文：墙体材料。'
    return {'run': {'id': 'agent_run', 'task_id': 'task', 'usage': [{'total_tokens': tokens}]},
            'drawings': [{'id': 'snapshot', 'sha256': active['uploads'][0]['sha256']}],
            'task': {'task_id': 'task', 'status': 'completed'},
            'evidence_records': [{'ref': {'record_id': 'e', 'sha256': sha256(content.encode()).hexdigest()},
                                  'content': content, 'sources': {'snapshot': active['uploads'][0]['sha256']}}]}


def test_different_node_concurrent_start_does_not_fail_winning_dispatch(client, monkeypatch):
    run = create_run(client)
    fleet = client.app.state.fleet_store
    original = fleet.create_job
    entered, release = Event(), Event()
    def enqueue(payload, idempotency_key=None):
        entered.set()
        assert release.wait(5)
        return original(payload, idempotency_key)
    monkeypatch.setattr(fleet, 'create_job', enqueue)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, start_url(run), json={'preferred_node': 'a'})
        assert entered.wait(5)
        try:
            conflicting = client.post(start_url(run), json={'preferred_node': 'b'})
            assert conflicting.status_code == 409
            assert conflicting.json()['code'] == 'DISPATCH_INPUT_CHANGED'
            case = client.app.state.exploration.get_case(run['run_id'], run['cases'][0]['case_id'])
            assert case['status'] == 'queued' and case['dispatch_failures'] == []
        finally:
            release.set()
        assert first.result().status_code == 200
    jobs = fleet.jobs()
    assert len(jobs) == 1 and jobs[0]['status'] == 'queued' and jobs[0]['preferred_node'] == 'a'


def test_lost_enqueue_acknowledgement_recovers_without_cancelling_committed_job(client, monkeypatch):
    run = create_run(client)
    fleet = client.app.state.fleet_store
    original = fleet.create_job
    def enqueue(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('lost acknowledgement')
    monkeypatch.setattr(fleet, 'create_job', enqueue)
    response = start(client, run)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'queued'
    assert len(fleet.jobs()) == 1 and fleet.jobs()[0]['status'] == 'queued'


def test_uncommitted_dispatch_error_stays_retryable_and_keeps_its_reservation(client, monkeypatch):
    run = create_run(client)
    fleet, exploration = client.app.state.fleet_store, client.app.state.exploration
    original = fleet.create_job
    def broken(*args, **kwargs):
        raise OSError('temporary storage error')
    monkeypatch.setattr(fleet, 'create_job', broken)
    with pytest.raises(OSError):
        start(client, run)
    case = exploration.get_case(run['run_id'], run['cases'][0]['case_id'])
    assert case['status'] == 'queued' and case['prepared'] is True
    assert case['dispatch_failures'][0]['retryable'] is True
    monkeypatch.setattr(fleet, 'create_job', original)
    assert start(client, run).status_code == 200
    assert exploration.get_run(run['run_id'])['budget_usage']['case_starts'] == 1


def test_cancel_between_enqueue_and_bind_still_recovers_archived_evidence(client):
    run = create_run(client)
    fleet, exploration = client.app.state.fleet_store, client.app.state.exploration
    case = exploration.prepare_case(run['run_id'], run['cases'][0]['case_id'])
    created = fleet.create_job(build_job_payload(run, case), idempotency_key=case['dispatch_key'])
    active = fleet.claim('a')
    fleet.finish(created['id'], 'a', active['attempt_id'], result={'answer': 'facts'},
                 artifacts=artifacts(active, 10), delivery_status='completed')
    response = client.post(f"/api/fleet/exploration-runs/{run['run_id']}/cancel", json={})
    assert response.status_code == 200, response.text
    recovered = response.json()['cases'][0]
    assert recovered['job_id'] == created['id'] and recovered['status'] == 'cancelled'
    assert recovered['case_record']['late_archive'] is True
    assert recovered['case_record']['fact_package']['records']


def test_sync_reads_pinned_attempt_when_fleet_claims_retry_during_archive_read(client, monkeypatch):
    run = create_run(client)
    assert start(client, run).status_code == 200
    fleet = client.app.state.fleet_store
    first = fleet.claim('a')
    fleet.finish(first['id'], 'a', first['attempt_id'], error={'code': 'TEMPORARY'},
                 retryable=True, artifacts=artifacts(first, 10))
    original = fleet.archive
    observed = []
    def read(job_id, attempt_id=None):
        observed.append(attempt_id)
        if len(observed) == 1:
            next_attempt = fleet.claim('b')
            assert next_attempt['attempt_id'] != first['attempt_id']
        return original(job_id, attempt_id)
    monkeypatch.setattr(fleet, 'archive', read)
    response = client.get(f"/api/fleet/exploration-runs/{run['run_id']}")
    assert response.status_code == 200, response.text
    assert observed == [first['attempt_id']]
    assert response.json()['cases'][0]['status'] == 'queued'
    assert client.get(f"/api/fleet/exploration-runs/{run['run_id']}").json()['cases'][0]['status'] == 'running'


def test_sync_recovers_unobserved_failed_attempt_usage_without_changing_current_result(client):
    run = create_run(client)
    assert start(client, run).status_code == 200
    fleet = client.app.state.fleet_store
    first = fleet.claim('a')
    fleet.finish(first['id'], 'a', first['attempt_id'], error={'code': 'TEMPORARY'},
                 retryable=True, artifacts=artifacts(first, 17))
    second = fleet.claim('b')
    fleet.finish(second['id'], 'b', second['attempt_id'], result={'answer': 'facts'},
                 artifacts=artifacts(second, 20), delivery_status='completed')
    response = client.get(f"/api/fleet/exploration-runs/{run['run_id']}")
    assert response.status_code == 200, response.text
    case = response.json()['cases'][0]
    assert case['status'] == 'completed' and case['case_record']['attempt_id'] == second['attempt_id']
    assert len(case['attempt_records']) == 2 and case['usage']['total_tokens'] == 37
    assert client.get(f"/api/fleet/exploration-runs/{run['run_id']}").json()['budget_usage']['reported_tokens'] == 37


def test_preflight_capability_failure_is_exported_without_charging_a_start(client):
    run = create_run(client, text='很长的原始规则。' * 900)
    response = start(client, run)
    assert response.status_code == 400
    exported = client.get(f"/api/fleet/exploration-runs/{run['run_id']}/export").json()
    case = exported['cases'][0]
    assert case['status'] == 'failed' and case['started_at'] is None
    assert case['case_record']['failures'][0]['error']['code'] == 'EXPLORATION_QUESTION_TOO_LONG'
    assert exported['budget_usage']['case_starts'] == 0


def test_no_drawings_preflight_does_not_become_a_failure(client):
    run = create_run(client, drawings=False)
    assert start(client, run).status_code in (400, 409)
    case = client.app.state.exploration.get_run(run['run_id'])['cases'][0]
    assert case['status'] == 'waiting_input' and case['dispatch_failures'] == []
