from types import SimpleNamespace
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cad_agent.adapters.store import Store
from cad_agent.fleet.store import Conflict
from cad_agent.fleet.supervisor import (Configure, SupervisorConfig, SupervisorStore,
    SupervisorManager, compact_snapshot, register_agent_supervisor, RESERVATION)


def configuration(command='c1', revision=0, **fields):
    return Configure(command_id=command, expected_revision=revision,
                     config=SupervisorConfig(enabled=True, **fields))


def test_supervisor_cas_idempotency_and_restart(tmp_path):
    store = SupervisorStore(tmp_path)
    value = store.configure('job', 'j', configuration())
    assert value['revision'] == 1
    assert store.configure('job', 'j', configuration())['revision'] == 1
    with pytest.raises(Conflict):
        store.configure('job', 'j', configuration(command='other'))
    with pytest.raises(Conflict):
        store.configure('job', 'j', configuration(mode='guidance'))
    assert SupervisorStore(tmp_path).public('job', 'j')['config']['enabled']


def test_unknown_usage_reserved_and_failed_reviews_not_retried(tmp_path):
    store = SupervisorStore(tmp_path)
    store.configure('job', 'j', configuration(max_total_tokens=RESERVATION))
    assert store.claim('job', 'j', 'state', active=False) is None
    review = store.claim('job', 'j', 'state', active=True)
    assert review
    assert store.claim('job', 'j', 'state', active=True) is None
    store.finish('job', 'j', review['review_id'], status='failed', error='transport')
    store.request_review('job', 'j')
    assert store.claim('job', 'j', 'changed', active=True) is None
    public = store.public('job', 'j')
    assert public['status'] == 'budget_exhausted'
    assert public['usage']['reserved_unknown_tokens'] == RESERVATION
    assert public['usage']['reported_tokens'] == 0


def test_new_supervisor_configuration_rechecks_unchanged_snapshot(tmp_path):
    store = SupervisorStore(tmp_path)
    store.configure('job', 'j', configuration())
    first = store.claim('job', 'j', 'unchanged', active=True)
    store.finish('job', 'j', first['review_id'], status='completed', usage={'known': True, 'total_tokens': 10})
    assert store.claim('job', 'j', 'unchanged', active=True) is None
    store.configure('job', 'j', configuration(command='different-focus', revision=1, instructions='检查来源缺口'))
    assert store.claim('job', 'j', 'unchanged', active=True) is not None
    assert store.public('job', 'j')['usage']['review_count'] == 2


class Operations:
    def __init__(self):
        self.revision = 0
        self.commands = []

    def snapshot(self, kind, target):
        return {'kind': kind, 'id': target, 'status': 'running', 'progress': '读取来源',
                'control': {'revision': self.revision, 'state': 'running'},
                'graph': {'nodes': [{'id': 'agent', 'type': 'agent', 'status': 'running'}], 'edges': []}}

    def submit(self, kind, target, **fields):
        if fields['expected_revision'] != self.revision:
            raise Conflict('changed')
        self.commands.append(fields)
        self.revision += 1


def manager(tmp_path, reviewer):
    operations = Operations()
    job = {'id': 'j', 'status': 'running', 'run_id': 'chat_x', 'node_id': 'a', 'attempt_id': 'attempt'}
    fleet = SimpleNamespace(root=tmp_path, job=lambda _: dict(job))
    app = SimpleNamespace(state=SimpleNamespace(fleet_store=fleet, operations=operations))
    return SupervisorManager(app, reviewer), operations, job


def test_supervision_submits_guidance_only_and_skips_unchanged(tmp_path):
    calls = []
    def reviewer(*args):
        calls.append(args)
        return {'advice': '需检查来源', 'guidance': '读取缺少的原文段落', 'usage': {'total_tokens': 100}}
    supervisor, ops, _ = manager(tmp_path, reviewer)
    supervisor.store.configure('job', 'j', configuration(mode='guidance'))
    supervisor.tick('job', 'j')
    assert len(ops.commands) == 1
    assert ops.commands[0]['action'] == 'guidance'
    assert ops.commands[0]['source'] == 'supervisor'
    assert supervisor.store.public('job', 'j')['usage']['reported_tokens'] == 100
    review = supervisor.store.public('job', 'j')['reviews'][0]
    assert review['operational_snapshot']['progress'] == '读取来源'
    assert review['selected_binding']['attempt_id'] == 'attempt'
    supervisor.tick('job', 'j')
    assert len(calls) == 1


def test_human_change_during_review_prevents_stale_guidance(tmp_path):
    def reviewer(*_):
        ops.revision += 1
        return {'advice': '建议', 'guidance': '下一步', 'usage': {}}
    supervisor, ops, _ = manager(tmp_path, reviewer)
    supervisor.store.configure('job', 'j', configuration(mode='guidance'))
    supervisor.tick('job', 'j')
    assert ops.commands == []
    assert supervisor.store.public('job', 'j')['reviews'][0]['guidance_status'] == 'advisory_only_or_target_changed'


def test_disabled_during_review_and_terminal_job_do_not_receive_guidance(tmp_path):
    def reviewer(*_):
        supervisor.store.configure('job', 'j', Configure(command_id='disable', expected_revision=1,
            config=SupervisorConfig(enabled=False, mode='guidance')))
        job['status'] = 'completed'
        return {'advice': '建议', 'guidance': '下一步', 'usage': {'total_tokens': 7}}
    supervisor, ops, job = manager(tmp_path, reviewer)
    supervisor.store.configure('job', 'j', configuration(mode='guidance'))
    supervisor.tick('job', 'j')
    assert not ops.commands
    assert supervisor.store.public('job', 'j')['status'] == 'disabled'
    supervisor.tick('job', 'j')
    assert len(supervisor.store.public('job', 'j')['reviews']) == 1


def test_snapshot_has_no_raw_evidence_or_secret_fields():
    result = compact_snapshot({'id': 'j', 'kind': 'job', 'raw_evidence': 'private CAD',
        'api_key': 'secret', 'control': {'revision': 1, 'commands': [{'message': 'raw text'}]},
        'graph': {'nodes': [{'id': 'x', 'secret': 'hidden', 'status': 'running'}], 'edges': []}})
    text = json.dumps(result)
    assert 'private CAD' not in text and 'secret' not in text and 'raw text' not in text


def test_agent_review_internal_auth_no_tools_and_idempotency(tmp_path, monkeypatch):
    from cad_agent.adapters.model import ModelGateway
    monkeypatch.setenv('CAD_FLEET_TOKEN', 'token-value')
    calls = []
    def chat(self, messages, tools, timeout):
        calls.append(messages)
        assert tools == [] and timeout == 55
        return {'content': '{"advice":"建议读取来源","guidance":"检查缺口"}'}, {'total_tokens': 12}
    monkeypatch.setattr(ModelGateway, 'chat_turn', chat)
    app = FastAPI()
    workspace = SimpleNamespace(lock=threading.RLock(), store=Store(tmp_path), model_catalog={'api_sol': object()})
    register_agent_supervisor(app, workspace)
    with TestClient(app) as client:
        body = {'review_id': 'review_'+'a'*32, 'model_profile': 'api_sol', 'snapshot': {'status': 'running'}}
        assert client.post('/api/fleet-supervision-review', json=body).status_code == 403
        headers = {'Authorization': 'Bearer token-value'}
        response = client.post('/api/fleet-supervision-review', json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['usage']['total_tokens'] == 12
        assert client.post('/api/fleet-supervision-review', json=body, headers=headers).json() == response.json()
        assert len(calls) == 1
        body['snapshot'] = {'status': 'different'}
        assert client.post('/api/fleet-supervision-review', json=body, headers=headers).status_code == 409


def test_supervisor_cannot_grant_itself_settings():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SupervisorConfig(enabled=True, permissions={'dynamic_methods': 'task_verified'})
    with pytest.raises(ValidationError):
        SupervisorConfig(enabled=True, max_reviews=True)


def test_missing_historical_trace_does_not_claim_zero_tool_calls():
    from cad_agent.fleet.operations import usage_summary
    assert usage_summary({})['tool_calls'] is None
    assert usage_summary({'trace': []})['tool_calls'] == 0


def test_fleet_supervision_api_auth_config_and_disabled_gate(tmp_path):
    from cad_agent.fleet.app import create_app, FleetSettings
    app = create_app(FleetSettings(tmp_path, 'internal-test-token-'+'x'*32, password='pw'))
    fleet = app.state.fleet_store
    upload = fleet.add_upload('synthetic.dxf', b'synthetic-not-for-inference')
    job = fleet.create_job({'upload_ids': [upload['id']], 'question': 'fixture', 'mode': 'agent'})
    path = '/api/fleet/jobs/'+job['id']+'/supervision'
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        client.post('/api/fleet/login', json={'password': 'pw'})
        assert client.get(path).json()['status'] == 'disabled'
        assert client.post(path+'/review').status_code == 409
        response = client.post(path, json=configuration().model_dump())
        assert response.status_code == 200, response.text
        assert response.json()['revision'] == 1
        app.state.supervisor.tick('job', job['id'])
        value = client.get(path).json()
        assert value['status'] == 'waiting_activity'
        assert value['reviews'] == []
        control = client.post('/api/fleet/jobs/'+job['id']+'/control', json={
            'command_id': 'pause-for-export', 'expected_revision': 0, 'action': 'pause', 'payload': {}})
        assert control.status_code == 200
        exported = client.get('/api/fleet/jobs/'+job['id']+'/operations/export').json()
        assert exported['audit']['control']['commands'][0]['command_id'] == 'pause-for-export'
        assert exported['audit']['supervision']['config']['enabled'] is True
        assert client.post(path, json=configuration(command='stale').model_dump()).status_code == 409
        assert client.get('/api/fleet/jobs/nonexistent/supervision').status_code == 404


def test_agent_rejects_tool_request_without_executing_or_retrying(tmp_path, monkeypatch):
    from cad_agent.adapters.model import ModelGateway
    monkeypatch.setenv('CAD_FLEET_TOKEN', 'token-value')
    calls = []
    def chat(*args, **kwargs):
        calls.append(1)
        return {'content': '{}', 'tool_calls': [{'id': 'tool-request'}]}, {'total_tokens': 3}
    monkeypatch.setattr(ModelGateway, 'chat_turn', chat)
    app = FastAPI()
    workspace = SimpleNamespace(lock=threading.RLock(), store=Store(tmp_path), model_catalog={'api_sol': object()})
    register_agent_supervisor(app, workspace)
    with TestClient(app) as client:
        body = {'review_id': 'review_'+'b'*32, 'model_profile': 'api_sol', 'snapshot': {'status': 'running'}}
        headers = {'Authorization': 'Bearer token-value'}
        assert client.post('/api/fleet-supervision-review', json=body, headers=headers).status_code == 502
        assert client.post('/api/fleet-supervision-review', json=body, headers=headers).status_code == 409
        assert len(calls) == 1
