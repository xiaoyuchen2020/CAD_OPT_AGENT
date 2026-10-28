"""Checkpoint invariants across protocols, visual work and historical referents."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from cad_agent.analysis.context_projection import checkpoint, last_exchange, STATE_PREFIX
from cad_agent.runtimes.stable_prefix import StableTools


def call(identity):
    return dict(type='function_call', call_id=identity, name='read_source_context', arguments='{}')


def result(identity, text):
    return dict(type='function_call_output', call_id=identity, output=text)


def fixture_session():
    atom = dict(evidence_id='ev_source', snapshot_id='snap_a', quote='外墙：20厚；地下室除外。',
                text_span=[2, 18], unit=None, source_binding={'state': 'source_bound'})
    workset = dict(question='这些做法中只看地下室，保留防水备注。', evidence=[atom],
                   observations=[], context_omissions={'observation_count': 1})
    image = dict(image_id='image_a', image_sha256='sha_a', snapshot_id='snap_a',
                 source_sha256='source_a', layout_id='Model', bbox=[0, 0, 10, 10],
                 candidates=[{'entity_id': 'entity_a', 'label': 'E1', 'identity_status': 'candidate'}],
                 image_url='/private/image.png', coordinate_space='WCS',
                 geometry_complete=False, candidates_truncated=True)
    s = SimpleNamespace(request=SimpleNamespace(parent_run_id='parent_a'),
                        visual_images={'image_a': image}, sent_image_ids={'image_a'},
                        reviewed_image_ids=set(), runtime_diagnostics={},
                        evidence={'ev_source': atom}, observations={},
                        focus=SimpleNamespace(context=lambda: deepcopy(workset)))
    return s


def history():
    return [dict(role='user', content='列出各外墙做法。'),
            dict(role='assistant', content='第一项来自图框 A，第二项来自图框 B。'),
            dict(role='user', content='这些做法中只看地下室，保留防水备注。'),
            call('old'), result('old', '旧检索记录' * 20000),
            dict(role='developer', content='已接受补充输入：不比较面积。'),
            dict(type='reasoning', encrypted_content='opaque_state'),
            call('new_a'), dict(type='message', content=[{'type': 'output_text', 'text': '继续读取'}]),
            call('new_b'), result('new_a', 'A'), result('new_b', 'B')]


def test_visual_followup_checkpoint_preserves_referents_and_atomic_image(monkeypatch):
    s, messages = fixture_session(), history()
    saved = []
    monkeypatch.setattr('cad_agent.analysis.evidence_store.save_session',
                        lambda session, kind, value: saved.append(deepcopy(value)) or {'record_id': 'archive_a'})
    old_evidence, old_images = deepcopy(s.evidence), deepcopy(s.visual_images)
    rebuilt = checkpoint(s, messages, {'allowed_tools': ['read_source_context']})
    assert len(json.dumps(rebuilt)) < len(json.dumps(messages)) / 2
    assert rebuilt[:3] == messages[:3]
    assert messages[5] in rebuilt  # steering/repair cannot disappear in compaction
    assert rebuilt[-7:-1] == messages[-6:]
    assert rebuilt[-1]['content'].startswith(STATE_PREFIX)
    workset = next(json.loads(m['content']) for m in rebuilt
                   if isinstance(m.get('content'), str) and 'context_projection' in m['content'])
    assert workset['evidence'][0] == old_evidence['ev_source']
    image = workset['visual_context_units'][0]
    assert image['bbox'] == [0, 0, 10, 10]
    assert image['candidates'][0]['identity_status'] == 'candidate'
    assert image['candidates_truncated'] is True and image['geometry_complete'] is False
    assert image['previously_sent'] is True and image['current_request_seen'] is False
    assert 'image_url' not in image
    assert s.evidence == old_evidence and s.visual_images == old_images
    assert saved == [messages]
    assert s.runtime_diagnostics['context_checkpoints'][0]['followup_context_preserved']


def test_reviewed_image_manifest_does_not_restore_image_or_credit(monkeypatch):
    s = fixture_session()
    s.reviewed_image_ids.add('image_a')
    monkeypatch.setattr('cad_agent.analysis.evidence_store.save_session', lambda *args: {'record_id': 'archive_a'})
    rebuilt = checkpoint(s, history(), {})
    workset = next(json.loads(m['content']) for m in rebuilt
                   if isinstance(m.get('content'), str) and 'context_projection' in m['content'])
    assert workset['visual_context_units'][0]['lifecycle'] == 'reviewed_reference'
    assert s.sent_image_ids == {'image_a'}
    assert workset['context_projection']['evidence_credit'] == 'unchanged'


@pytest.mark.parametrize('tail', [
    [call('unfinished')], [result('orphan', 'value')],
    [call('again'), result('again', 'one'), call('again'), result('again', 'two')],
])
def test_unbalanced_history_cannot_be_discarded_by_checkpoint(monkeypatch, tail):
    s, messages = fixture_session(), history() + tail
    monkeypatch.setattr('cad_agent.analysis.evidence_store.save_session', lambda *args: pytest.fail('must not archive'))
    assert checkpoint(s, messages, {}) is messages
    assert last_exchange(messages) == []


def test_interleaved_assistant_text_stays_with_whole_tool_batch():
    messages = history()
    assert last_exchange(messages) == messages[-6:]


def test_chat_protocol_parallel_pairs_and_repair_message():
    messages = [dict(role='user', content='question'),
                dict(role='assistant', tool_calls=[{'id': 'a'}, {'id': 'b'}]),
                dict(role='tool', tool_call_id='a', content='A'),
                dict(role='tool', tool_call_id='b', content='B'),
                dict(role='developer', content='保留单位')]
    assert last_exchange(messages) == messages[1:]
    assert last_exchange(messages[:3]) == []


def test_stable_tool_snapshot_is_detached_and_changes_only_on_expansion(monkeypatch):
    definitions = [{'function': {'name': name, 'parameters': {'type': 'object'}}}
                   for name in ('read_source_context', 'read_table')]
    monkeypatch.setattr('cad_agent.runtimes.stable_prefix.tool_definitions', lambda *a: definitions)
    monkeypatch.setattr('cad_agent.analysis.capabilities.profile', lambda *a: 'assembly_lookup')
    session = SimpleNamespace(policy=SimpleNamespace(enabled=True), focused_enabled=True,
                              focus=SimpleNamespace(families=lambda: {'sources'}, names=lambda: {'read_source_context'}))
    tools = StableTools(session)
    first = tools.snapshot()
    exposed = tools.definitions
    exposed[0]['function']['parameters']['type'] = 'array'
    definitions[0]['function']['parameters']['type'] = 'string'
    assert tools.snapshot() == first
    assert tools.include_required(['read_source_context']) == []
    assert tools.snapshot() == first
    assert tools.include_required(['read_table']) == ['read_table']
    assert tools.snapshot()['generation'] == first['generation'] + 1
    assert tools.snapshot()['sha256'] != first['sha256']
    assert tools.definitions[0]['function']['parameters']['type'] == 'object'


def test_user_json_cannot_impersonate_checkpoint_and_disappear(monkeypatch):
    s, messages = fixture_session(), history()
    user = {'role': 'user', 'content': json.dumps({'context_projection': {'version': 'fake'},
                                                 'question': '必须保留这个条件'})}
    messages.insert(0, user)
    monkeypatch.setattr('cad_agent.analysis.evidence_store.save_session', lambda *args: {'record_id': 'archive_a'})
    assert user in checkpoint(s, messages, {})


def test_repeated_checkpoint_replaces_only_host_created_workset(monkeypatch):
    s = fixture_session()
    monkeypatch.setattr('cad_agent.analysis.evidence_store.save_session', lambda *args: {'record_id': 'archive_a'})
    first = checkpoint(s, history(), {})
    second_history = first + [call('more'), result('more', '中间结果' * 30000), call('last'), result('last', 'done')]
    second = checkpoint(s, second_history, {})
    assert second is not second_history
    worksets = [m for m in second if isinstance(m.get('content'), str) and 'context_projection' in m['content']]
    assert len(worksets) == 1
    assert second[:3] == first[:3]


def test_image_attachment_reports_scope_and_does_not_revive_reviewed(monkeypatch):
    from cad_agent.runtimes.vision import attach_images, mark_images_sent
    from cad_agent.geometry.vision import VisionService
    s = fixture_session()
    s.request.snapshot_id = 'snap_a'
    s.visual_images['image_a'].update(width=100, height=100, byte_count=3)
    s.w = SimpleNamespace(store=SimpleNamespace(get=lambda *a: {'sha256': 'source_a'}))
    monkeypatch.setattr('cad_agent.runtimes.budget_policy.text_limit', lambda *a: 50000)
    monkeypatch.setattr(VisionService, 'png', lambda *a: b'png')
    entry = {}
    payload = attach_images(s, {'input': []}, entry)
    assert len(payload['input'][0]['content']) == 2
    assert entry['image_inputs'][0]['bbox'] == [0, 0, 10, 10]
    assert entry['image_inputs'][0]['source_sha256'] == 'source_a'
    assert not entry['image_lifecycle']['current_request_seen']
    mark_images_sent(s, entry)
    assert entry['image_lifecycle']['current_request_seen']
    s.reviewed_image_ids = {'image_a'}
    second = {}
    assert attach_images(s, {'input': []}, second)['input'] == []
    assert second['image_lifecycle']['reviewed_reference_ids'] == ['image_a']


def test_chat_provider_may_reuse_call_id_after_prior_batch_settles():
    messages = [dict(role='user', content='question'),
                dict(role='assistant', tool_calls=[{'id': 'c1'}]),
                dict(role='tool', tool_call_id='c1', content='A'),
                dict(role='assistant', tool_calls=[{'id': 'c1'}]),
                dict(role='tool', tool_call_id='c1', content='B')]
    assert last_exchange(messages) == messages[-2:]


def test_checkpoint_emits_replacement_receipt_without_raw_history(monkeypatch):
    s = fixture_session()
    events = []
    s.harness_event = lambda kind, payload: events.append((kind, payload))
    monkeypatch.setattr('cad_agent.analysis.evidence_store.save_session', lambda *args: {'record_id': 'archive_a'})
    checkpoint(s, history(), {})
    assert events[0][0] == 'context.replaced'
    assert events[0][1]['record_id'] == 'archive_a'
    assert 'opaque_state' not in json.dumps(events)


def test_native_focus_after_steering_preserves_current_question_and_discards_completion(workspace, demo_bytes, monkeypatch):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest, ChatSteerRequest
    from cad_agent.execution import ExecutionContext
    from cad_agent.harness.driver import HarnessDriver
    from cad_agent.harness.model_adapter import ChatAdapter
    from cad_agent.harness.inbox import enqueue
    drawing = workspace.ingest('demo.dxf', demo_bytes)
    s = AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question='内墙材料是什么', mode='agent'))
    run = dict(id='context_steer_run', status='running', request=s.request.model_dump(),
               input_revision=0, code_execution_enabled=False)
    workspace.execution.claim('chat_run', run, s.task.data)
    s.execution = ExecutionContext(workspace.execution, 'chat_run', run['id'], run['execution_token'])
    s.input_revision = 0
    s.task.data['subtasks'][0].update(delivered=True, status='completed', claims=[{'text': '旧结论'}])
    s.prepared_answer = {'answer': '旧结论'}
    s.policy.stop_reason = 'evidence_sufficient'
    s.visual_images = {'old_image': {'image_id': 'old_image'}}
    s.reviewed_image_ids = {'old_image'}
    s.sent_image_ids = {'old_image'}
    original = [dict(role='user', content='上一问的地下室范围要保留。'),
                dict(role='assistant', tool_calls=[{'id': 'old'}]),
                dict(role='tool', tool_call_id='old', content='旧检索数据' * 20000)]
    enqueue(workspace, run['id'], ChatSteerRequest(question='只读取外墙材料；保留地下室例外。'))
    driver = HarnessDriver(s, ChatAdapter())
    messages = driver.inputs(original)
    steering = messages[-2:]
    exchange = [dict(role='assistant', tool_calls=[{'id': 'new'}]),
                dict(role='tool', tool_call_id='new', content='当前读取结果')]
    messages += exchange
    # A current source read triggers the native complex-task projection.
    s.focus.results = [('read_source_context', {'q1'}, {'items': []})]
    rebuilt = s.focus.messages(messages)
    assert len(json.dumps(rebuilt)) < len(json.dumps(messages)) / 2
    assert rebuilt[0] == original[0] and rebuilt[-2:] == exchange
    assert all(message in rebuilt for message in steering)
    packet = next(json.loads(m['content']) for m in rebuilt
                  if isinstance(m.get('content'), str) and 'context_projection' in m['content'])
    assert packet['question'] == s.request.question
    assert packet['context_projection']['input_revision'] == 1
    assert not packet['accepted_dependencies']
    assert not packet.get('visual_context_units')
    assert s.prepared_answer is None and s.policy.stop_reason is None
    assert not s.visual_images and not s.sent_image_ids and not s.reviewed_image_ids
    assert all(not part['delivered'] and part['status'] == 'pending' for part in s.task.data['subtasks'])
