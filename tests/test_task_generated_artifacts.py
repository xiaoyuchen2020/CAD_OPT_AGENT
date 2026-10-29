"""Materialize only a bound Fleet task; no model or generated-code execution."""
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.analysis import generated_artifacts as export
from cad_agent.extensions.table_recovery.bundle import digest
from test_table_recovery import recovery_case


@pytest.fixture
def exported_run(tmp_path, monkeypatch):
    monkeypatch.setenv('CAD_FLEET_TOKEN', 'test-internal-secret')
    app = create_app(Settings(data_dir=tmp_path / 'data', llm_query_intent=False))
    store = app.state.workspace.store
    def artifact(kind, value):
        ident = 'ad_' + kind + '_' + digest(value)
        store.put('adaptation_' + kind, ident, value)
        return ident
    refs = {
        'input_artifact_id': artifact('input', {'snapshot_id': 'snap-a', 'texts': ['synthetic source']}),
        'code_artifact_id': artifact('code', {'code': 'def adapt(bundle): return {}'}),
        'execution_artifact_id': artifact('execution', {'output': {'input_id': 'fixture'}}),
        'validation_artifact_id': artifact('validation', {'semantic_acceptance': False}),
        'metadata_artifact_id': artifact('model', {'prompt_messages': ['DO_NOT_EXPORT_MODEL_METADATA']}),
    }
    task = dict(task_id='task-a', snapshot_id='snap-a', revision=1, task_methods={
        'method-a': dict(task_id='task-a', snapshot_id='snap-a', status='validated', accepted_attempt=1,
            input_artifact_id=refs['input_artifact_id'], attempts=[{k: v for k, v in refs.items() if k != 'input_artifact_id'}])})
    run = dict(id='run-a', task_id='task-a', snapshot_id='snap-a', fleet_execution_key='attempt-a',
               status='completed', request={'snapshot_id': 'snap-a', 'related_drawings': []})
    store.put('agent_task', task['task_id'], task)
    store.put('chat_run', run['id'], run)
    headers = {'Authorization': 'Bearer test-internal-secret', 'X-Fleet-Execution': 'attempt-a'}
    with TestClient(app) as client:
        yield client, app.state.workspace, task, run, refs, artifact, headers


def get(context, headers=None, suffix=''):
    client, _, _, _, _, _, default = context
    return client.get('/api/fleet-runs/run-a/generated-artifacts' + suffix,
                      headers=default if headers is None else headers)


def test_requires_internal_token_and_matching_execution(exported_run):
    assert get(exported_run, {}).status_code == 403
    assert get(exported_run, {'Authorization': 'Bearer test-internal-secret'}).status_code == 403
    assert get(exported_run, {'Authorization': 'Bearer test-internal-secret', 'X-Fleet-Execution': 'attempt-other'}).status_code == 403
    assert get(exported_run, suffix='?task_id=task-other').status_code == 400
    assert get(exported_run, suffix='?kind=model&artifact_id=other').status_code == 400


def test_export_has_exact_hashes_and_excludes_model_metadata(exported_run):
    response = get(exported_run)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['complete'] is True and len(result['items']) == 4
    assert result['run_id'] == 'run-a' and result['task_id'] == 'task-a'
    assert 'DO_NOT_EXPORT_MODEL_METADATA' not in response.text
    for item in result['items']:
        assert item['sha256'] == digest(item['value'])
        assert item['artifact_id'] == 'ad_' + item['kind'] + '_' + item['sha256']
    assert result['excluded'][0]['reason'] == 'MODEL_METADATA'


@pytest.mark.parametrize('change,code', [('missing', 'ARTIFACT_MISSING'), ('corrupt', 'ARTIFACT_CORRUPT'),
                                         ('invalid_json', 'ARTIFACT_CORRUPT')])
def test_missing_or_corrupt_artifact_is_not_reported_complete(exported_run, monkeypatch, change, code):
    _, workspace, _, _, refs, _, _ = exported_run
    ident = refs['code_artifact_id']
    if change == 'missing':
        with workspace.store.connect() as db:
            db.execute('DELETE FROM records WHERE kind=? AND id=?', ('adaptation_code', ident))
    elif change == 'invalid_json':
        original_get = workspace.store.get
        def unreadable(kind, key):
            if kind == 'adaptation_code' and key == ident:
                raise json.JSONDecodeError('corrupt stored artifact', '{broken', 0)
            return original_get(kind, key)
        monkeypatch.setattr(workspace.store, 'get', unreadable)
    else:
        workspace.store.put('adaptation_code', ident, {'code': 'tampered'})
    result = get(exported_run).json()
    assert result['complete'] is False
    assert any(item['code'] == code and item['artifact_id'] == ident for item in result['diagnostics'])
    assert ident not in {item['artifact_id'] for item in result['items']}


@pytest.mark.parametrize('value', [
    {'bundle': {'sources': []}, 'refs': []},
    {'bundle': {'sources': None}, 'refs': [{'snapshot_id': 'snap-a', 'bundle': {'snapshot_id': 'snap-a'}}]},
    {'bundle': {'sources': []}, 'refs': [{'snapshot_id': ['snap-a'], 'bundle': {'snapshot_id': 'snap-a'}}]},
])
def test_malformed_source_binding_is_diagnostic_not_server_error(exported_run, value):
    _, workspace, task, _, _, artifact, _ = exported_run
    ident = artifact('program_input', value)
    task['task_programs'] = {'program-a': dict(task_id='task-a', kind='program',
        status='failed', input_artifact_id=ident, attempts=[])}
    workspace.store.put('agent_task', task['task_id'], task)
    response = get(exported_run)
    assert response.status_code == 200
    result = response.json()
    assert result['complete'] is False
    assert any(item['code'] == 'ARTIFACT_SOURCE_NOT_AUTHORIZED' and item['artifact_id'] == ident
               for item in result['diagnostics'])
    assert ident not in {item['artifact_id'] for item in result['items']}


def test_foreign_task_and_source_references_are_not_materialized(exported_run):
    _, workspace, task, _, refs, artifact, _ = exported_run
    foreign = deepcopy(task['task_methods']['method-a'])
    foreign['task_id'] = 'task-other'
    foreign['attempts'][0]['code_artifact_id'] = artifact('code', {'code': 'FOREIGN_CODE_DO_NOT_EXPORT'})
    task['task_methods']['method-other'] = foreign
    task['task_methods']['method-a']['input_artifact_id'] = artifact('input', {'snapshot_id': 'snap-other', 'text': 'FOREIGN_INPUT'})
    workspace.store.put('agent_task', task['task_id'], task)
    response = get(exported_run)
    result = response.json()
    assert result['complete'] is False
    assert {item['code'] for item in result['diagnostics']} == {'FOREIGN_TASK_METHOD', 'ARTIFACT_SOURCE_NOT_AUTHORIZED'}
    assert 'FOREIGN_CODE_DO_NOT_EXPORT' not in response.text and 'FOREIGN_INPUT' not in response.text


def test_bound_task_cannot_be_replaced_by_foreign_snapshot(exported_run):
    _, workspace, task, _, _, _, _ = exported_run
    task['snapshot_id'] = 'snap-other'
    workspace.store.put('agent_task', task['task_id'], task)
    assert get(exported_run).status_code == 409


def test_size_limit_is_explicit_without_partial_success(exported_run, monkeypatch):
    monkeypatch.setattr(export, 'MAX_EXPORT_BYTES', 64)
    response = get(exported_run)
    assert response.status_code == 413
    assert response.json()['detail']['code'] == 'GENERATED_ARTIFACT_BYTE_LIMIT'
    assert 'items' not in response.json()


def test_program_and_revision_references_are_materialized_once(exported_run):
    _, workspace, task, _, _, artifact, _ = exported_run
    source = {'snapshot_id': 'snap-a', 'texts': ['synthetic']}
    inputs = artifact('program_input', {'bundle': {'sources': [source]}, 'refs': [{'snapshot_id': 'snap-a', 'bundle': source}]})
    package = artifact('program', {'package': {'files': {'main': 'def run(bundle): return {}'}, 'entry': 'main'}})
    execution = artifact('program_execution', {'output': []})
    validation = artifact('program_validation', {'semantic_acceptance': False})
    task['task_programs'] = {'program-a': dict(task_id='task-a', kind='revision', status='validated', accepted_attempt=1,
        input_artifact_id=inputs, package_artifact_id=package, attempts=[dict(package_artifact_id=package,
            execution_artifact_id=execution, validation_artifact_id=validation,
            diff_artifact_id=artifact('program_diff', {'diff': 'synthetic diff'}),
            regression_artifact_id=artifact('program_regression', {'reports': []}),
            baseline_artifact_id=execution,
            model_artifact_id=artifact('program_model', {'prompt': 'DO_NOT_EXPORT_PROGRAM_MODEL'}))])}
    task['program_results'] = {'result-a': dict(program_id='program-a', status='validated', input_artifact_id=inputs,
        execution_artifact_id=execution, validation_artifact_id=validation)}
    task['tool_version_routes'] = {'route-a': dict(version_id='program-a', snapshot_id='snap-a', refs_artifact_id=inputs,
        executions={'kernel-a': dict(input_artifact_id=artifact('program_kernel_input', {'regions': []}),
            execution_artifact_id=execution, validation_artifact_id=validation)})}
    workspace.store.put('agent_task', task['task_id'], task)
    response = get(exported_run)
    result = response.json()
    assert result['complete'] is True, result['diagnostics']
    ids = [item['artifact_id'] for item in result['items']]
    assert ids.count(package) == ids.count(execution) == ids.count(inputs) == 1
    assert 'DO_NOT_EXPORT_PROGRAM_MODEL' not in response.text
    assert any(item['kind'] == 'program_kernel_input' for item in result['items'])


def test_imported_program_excludes_old_input_but_keeps_current_result(exported_run):
    _, workspace, task, _, _, artifact, _ = exported_run
    old_input = artifact('program_input', {'bundle': {'sources': [{'snapshot_id': 'snap-old'}]}, 'refs': []})
    package = artifact('program', {'package': {'files': {'main': 'def run(bundle): return {}'}, 'entry': 'main'}})
    registration = artifact('program_registration', {'entry': {'package_artifact_id': package, 'contract': {'fixture': True}}})
    task['task_programs'] = {'imported-a': dict(task_id='task-a', kind='program', status='validated', contract={'fixture': True},
        imported_from=registration, input_artifact_id=old_input, package_artifact_id=package, attempts=[])}
    source = {'snapshot_id': 'snap-a'}
    current = artifact('program_input', {'bundle': {'sources': [source]}, 'refs': [{'snapshot_id': 'snap-a', 'bundle': source}]})
    task['program_results'] = {'result-a': dict(program_id='imported-a', status='validated', input_artifact_id=current,
        execution_artifact_id=artifact('program_execution', {'output': []}),
        validation_artifact_id=artifact('program_validation', {'semantic_acceptance': False}))}
    workspace.store.put('agent_task', task['task_id'], task)
    result = get(exported_run).json()
    assert result['complete'] is True, result['diagnostics']
    assert {item['artifact_id'] for item in result['items']} >= {package, current}
    assert old_input not in {item['artifact_id'] for item in result['items']}
    assert any(item['artifact_id'] == old_input and item['reason'] == 'IMPORTED_GENERATION_INPUT' for item in result['excluded'])


def test_foreign_program_results_and_routes_do_not_export_refs(exported_run):
    _, workspace, task, _, _, artifact, _ = exported_run
    foreign = artifact('program_execution', {'output': 'FOREIGN_PROGRAM_RESULT'})
    task['program_results'] = {'other': {'program_id': 'program-other', 'execution_artifact_id': foreign}}
    task['tool_version_routes'] = {'other': {'version_id': 'program-other', 'snapshot_id': 'snap-a',
                                         'executions': {'x': {'execution_artifact_id': foreign}}}}
    workspace.store.put('agent_task', task['task_id'], task)
    response = get(exported_run)
    result = response.json()
    assert result['complete'] is False
    assert {item['code'] for item in result['diagnostics']} == {'FOREIGN_PROGRAM_RESULT', 'FOREIGN_TOOL_ROUTE'}
    assert foreign not in {item['artifact_id'] for item in result['items']}


def test_actual_task_ledgers_export_validated_method_and_program(workspace, recovery_case, monkeypatch):
    from test_task_methods import session_for, baseline, mocks as method_mocks, develop as develop_method
    from test_task_programs import mocks as program_mocks, develop as develop_program
    method_mocks(monkeypatch)
    program_mocks(monkeypatch)
    session = session_for(workspace, recovery_case)
    oid = baseline(session, recovery_case)
    assert develop_method(session, oid)['admitted']
    assert develop_program(session, oid)['admitted']
    run = dict(id='synthetic-run', task_id=session.task.data['task_id'], snapshot_id=session.request.snapshot_id,
               request=session.request.model_dump())
    result = export.materialize(session.w, run)
    assert result['complete'], result['diagnostics']
    assert {'input', 'code', 'execution', 'validation', 'program', 'program_input',
            'program_execution', 'program_validation'} <= {item['kind'] for item in result['items']}
