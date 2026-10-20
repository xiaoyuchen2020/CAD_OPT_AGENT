"""Optional source batching advertises owners and isolates counted input failures."""
import json
from types import SimpleNamespace

import pytest

from cad_agent.analysis.tool_registry import tool_definitions
from cad_agent.contracts import ChatRequest
from test_batch_reads import make, read, invoke


def complex_session(workspace, demo_bytes):
    session = make(workspace, demo_bytes, '1. 内墙材料是什么？\n2. 外墙材料是什么？')
    session.task.auto_plan()
    return session


def test_enabled_batch_schema_has_per_tool_arguments_and_required_owner_enum(workspace, demo_bytes):
    s = complex_session(workspace, demo_bytes)
    definition = next(t['function'] for t in tool_definitions(session=s) if t['function']['name'] == 'batch_read_sources')
    schema = definition['parameters']
    assert 'task_ids' not in schema['properties']
    variants = schema['properties']['reads']['items']['anyOf']
    assert {v['properties']['tool']['const'] for v in variants} == {'read_assembly', 'read_source_context'}
    for child in variants:
        owner = child['properties']['task_ids']
        assert 'task_ids' in child['required'] and owner['minItems'] == 1
        assert owner['items']['enum'] == [p['id'] for p in s.task.parts]
        args = child['properties']['arguments']
        assert args['additionalProperties'] is False and 'region_id' in args['required']
        assert 'task_ids' not in args['properties']
    s.request.source_read_batching = False
    assert 'batch_read_sources' not in {t['function']['name'] for t in tool_definitions(session=s)}
    result = json.loads(s.invoke_tool('batch_read_sources', {'reads': [read(s.index.regions[0].region_id)]}))
    assert result['error']['code'] == 'SOURCE_READ_BATCHING_DISABLED'
    assert s.calls == 1 and not s.evidence
    assert ChatRequest(snapshot_id=s.request.snapshot_id, question='材料').source_read_batching is False


def test_simple_task_schema_keeps_optional_owner(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    definition = next(t['function'] for t in tool_definitions(session=s) if t['function']['name'] == 'batch_read_sources')
    for child in definition['parameters']['properties']['reads']['items']['anyOf']:
        assert 'task_ids' not in child['required']


@pytest.mark.parametrize('mode,issue,path', [
    ('missing', 'missing_task_ids', 'task_ids'),
    ('empty', 'missing_task_ids', 'task_ids'),
    ('unknown', 'unknown_task_ids', 'task_ids'),
    ('mixed', 'unknown_task_ids', 'task_ids'),
    ('misplaced', 'misplaced_task_ids', 'arguments.task_ids'),
    ('conflicting', 'misplaced_task_ids', 'arguments.task_ids'),
])
def test_owner_failure_is_counted_once_and_valid_sibling_keeps_its_owner(workspace, demo_bytes, monkeypatch, mode, issue, path):
    s = complex_session(workspace, demo_bytes)
    owners = [p['id'] for p in s.task.parts]
    rid = s.index.regions[0].region_id
    bad = read(rid, neighbors=0)
    if mode == 'missing': bad.pop('task_ids')
    if mode == 'unknown': bad['task_ids'] = ['foreign']
    if mode == 'mixed': bad['task_ids'] = [owners[0], 'foreign']
    if mode in ('misplaced', 'conflicting'):
        bad['arguments']['task_ids'] = [owners[0]]
    if mode == 'conflicting': bad['task_ids'] = [owners[1]]
    executed = []
    actual = s.execute
    def execute(name, args, **kw):
        executed.append(name)
        return actual(name, args, **kw)
    monkeypatch.setattr(s, 'execute', execute)
    checkpoints = []
    s.execution = SimpleNamespace(check_cancelled=lambda: None,
        before_tool=lambda name, args: len(checkpoints),
        after_tool=lambda checkpoint, result: checkpoints.append(json.loads(result)))
    out = invoke(s, [bad, read(rid, neighbors=0, task_ids=[owners[1]])])
    error = out['items'][0]['result']['error']
    assert error['issue'] == issue and error['path'] == f'reads.0.{path}'
    assert error['allowed_task_ids'] == owners
    assert error == s.trace[0]['error'] == checkpoints[0]['error']
    assert s.calls == len(s.trace) == len(checkpoints) == 2
    assert executed == ['read_source_context']
    assert not out['items'][1]['result'].get('error')
    assert [step['calls'] for step in s.task.data['subtasks']] == [0, 1]
    assert s.task.data['subtasks'][0]['evidence_ids'] == []
    assert s.task.data['subtasks'][1]['evidence_ids']
    assert s._batch_read_context is None


def test_invalid_child_parameter_has_exact_path_without_exposing_input(workspace, demo_bytes):
    s = complex_session(workspace, demo_bytes)
    owner = s.task.parts[0]['id']
    rid = s.index.regions[0].region_id
    out = invoke(s, [read(rid, neighbors=0, task_ids=[owner]), read(rid, neighbors=8, task_ids=[owner])])
    error = out['items'][1]['result']['error']
    assert error['path'] == 'reads.1.arguments'
    assert error['field_errors'][0]['path'] == 'reads.1.arguments.neighbors'
    assert error['field_errors'][0]['code'] == 'less_than_equal'
    assert s.calls == 2
    assert 'input' not in error['field_errors'][0]
    assert s.trace[1]['error'] == error
    # The same independent tool keeps its existing non-batch error contract.
    direct = json.loads(s.invoke_tool('read_source_context', {'region_id': rid, 'task_ids': ['foreign']}))
    assert 'batch_position' not in direct['error'] and 'path' not in direct['error']
