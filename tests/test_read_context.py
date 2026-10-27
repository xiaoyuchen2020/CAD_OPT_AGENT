"""Read continuity and request-local reuse must never grant evidence authority."""
from copy import deepcopy
import json

import pytest

from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.model_context import ModelContextCodec
from cad_agent.analysis.read_context import PREFIX, project_messages
from test_model_context_codec import E, R, D, evidence as source_evidence


def evidence(**kwargs):
    return {**source_evidence(**kwargs), 'entity_ids': []}

O = 'obs_' + 'f' * 24


def fixture_page(**updates):
    data = dict(record_id='assembly_test', target_region_id=R, view='record', offset=0,
                has_more=False, next_offset=None, total=1,
                source_binding={'state': 'source_bound', 'engineering_applicability_state': 'unverified'},
                checks={'complete_in_this_response': True},
                items=[{'order': 3, 'evidence': evidence(), 'source_binding_state': 'source_bound'}])
    data.update(updates)
    return {'observation_id': O, 'tool': 'read_assembly', 'data': data}


def build(observation=None):
    c = ModelContextCodec('assembly_lookup')
    c.optimize_reads = True
    c.canonical_evidence = {E: evidence()}
    c.canonical_observations = {O: observation or fixture_page()}
    return c


def test_exploration_restores_whole_source_and_binding_without_mutating_history():
    c = build()
    messages = [{'role': 'user', 'content': dumps({'question': '继续读取来源'})}]
    before = deepcopy(messages)
    output = c.encode_messages(messages)
    page = json.loads(output[0]['content'][len(PREFIX):])
    assert page['pages'][0]['data']['source_binding']['state'] == 'source_bound'
    assert '5厚砂浆' in page['source_groups'][0]['text']
    assert R not in output[0]['content']
    assert '已读来源状态' in output[-1]['content']
    assert messages == before
    assert c.canonical_evidence[E] == evidence()



def test_bounded_checkpoint_keeps_omissions_without_restoring_history():
    c = build()
    messages = [{'role': 'user', 'content': dumps({'context_omissions': {'evidence_count': 1}})}]
    before = deepcopy(messages)
    output = c.encode_messages(messages)
    assert len(output) == 1
    assert json.loads(output[0]['content'])['context_omissions'] == {'evidence_count': 1}
    assert '5厚砂浆' not in dumps(output)
    assert not c.last_stats['restore_read_context']
    assert messages == before and c.canonical_evidence[E] == evidence()


def test_repeated_pages_use_single_definition_preserving_protocol_and_state():
    c = build()
    data = c.canonical_observations[O]['data']
    messages = [{'type': 'function_call', 'call_id': 'call1', 'name': 'read_assembly',
                 'arguments': dumps({'region_id': R})},
                {'type': 'function_call_output', 'call_id': 'call1',
                 'output': dumps({**data, 'task': {'pending': ['q2']}, 'new_annotation': 'keep'})}]
    output = c.encode_messages(messages)
    assert output[1] == messages[0]
    receipt = json.loads(output[2]['output'])
    assert receipt['read_page_observation_id'] == 'o1'
    assert receipt['task'] == {'pending': ['q2']} and receipt['new_annotation'] == 'keep'
    assert 'items' not in receipt
    assert output[2]['call_id'] == 'call1'
    assert c.last_stats['read_working_set']['reused_pages'] == 1
    assert c.decode({'observation_ids': ['o1']}) == {'observation_ids': [O]}


@pytest.mark.parametrize('updates', [dict(has_more=True, next_offset=12),
    dict(source_binding={'state': 'candidate', 'engineering_applicability_state': 'unverified'}),
    dict(offset=12)])
def test_partial_or_candidate_page_not_promoted_to_ready(updates):
    c = build(fixture_page(**updates))
    output = c.encode_messages([{'role': 'user', 'content': '{}'}])
    assert all(not m['content'].startswith('已读来源状态') for m in output)
    page = json.loads(output[0]['content'][len(PREFIX):])['pages'][0]['data']
    for k, v in updates.items():
        assert page[k] == v


def test_missing_quote_or_unread_observation_cannot_create_source():
    c = build()
    c.canonical_evidence = {}
    output = c.encode_messages([{'role': 'user', 'content': '{}'}])
    assert len(output) == 1
    c.canonical_evidence = {E: evidence()}
    c.canonical_observations = {}
    assert len(c.encode_messages([{'role': 'user', 'content': '{}'}])) == 1


def test_conflicting_quotes_fail_closed_before_parent_substitution():
    c = build()
    data = deepcopy(c.canonical_observations[O]['data'])
    data['items'][0]['evidence']['quote'] = '伪造文本'
    with pytest.raises(ValueError, match='SOURCE_CONFLICT'):
        c.encode_messages([{'role': 'tool', 'content': dumps(data)}])


def test_different_page_state_is_not_coalesced_and_failures_remain_visible():
    c = build()
    data = deepcopy(c.canonical_observations[O]['data'])
    data['has_more'] = True
    output = c.encode_messages([{'role': 'tool', 'content': dumps(data)},
        {'role': 'tool', 'content': dumps({'error': {'code': 'SOURCE_PAGE_ALREADY_READ'}})}])
    assert json.loads(output[1]['content'])['has_more'] is True
    assert 'read_page_observation_id' not in json.loads(output[1]['content'])
    assert json.loads(output[2]['content'])['error']['code'] == 'SOURCE_PAGE_ALREADY_READ'


def test_feature_off_and_mixed_task_retain_old_encoding():
    c = build()
    c.optimize_reads = False
    assert len(c.encode_messages([{'role': 'user', 'content': '{}'}])) == 1
    c.optimize_reads = True
    c.profile = 'mixed_task'
    assert len(c.encode_messages([{'role': 'user', 'content': '{}'}])) == 1


def test_conflicting_observation_versions_never_replace_original_page():
    first = fixture_page()
    second = fixture_page(has_more=True)
    second['observation_id'] = 'obs_' + 'e' * 24
    third = deepcopy(first)
    third['observation_id'] = 'obs_' + 'd' * 24
    out, stats = project_messages([{'role': 'tool', 'content': dumps(first['data'])}],
        {o['observation_id']: o for o in (first, second, third)}, {E: evidence()})
    assert stats['reused_pages'] == 0
    assert stats['pages'] == 3
    assert 'items' in json.loads(out[-1]['content'])


def test_item_tables_preserve_order_missing_fields_and_explicit_null():
    c = build()
    page = c.canonical_observations[O]['data']
    page['items'].append({'order': 4, 'evidence': {'evidence_id': E}, 'truncated': None})
    original = deepcopy(page['items'])
    result = c.encode_messages([{'role': 'user', 'content': '{}'}])
    packet = json.loads(result[0]['content'][len(PREFIX):])
    data = packet['pages'][0]['data']
    rows = [dict(zip(table['columns'], row)) for table in data['item_tables'] for row in table['rows']]
    assert [row['order'] for row in rows] == [3, 4]
    assert 'truncated' not in rows[0] and rows[1]['truncated'] is None
    assert page['items'] == original


def test_duplicate_feedback_reuses_observation_without_retry_or_new_credit():
    from types import SimpleNamespace
    from cad_agent.analysis.read_context import repair_read_routes
    c = build()
    session = SimpleNamespace(observations=c.canonical_observations,
        w=SimpleNamespace(settings=SimpleNamespace(read_context_optimization=True)))
    before = deepcopy(session.observations)
    packet = dict(error={'code': 'SOURCE_PAGE_ALREADY_READ'}, repair_context={}, next_actions=[
        {'tool': 'read_assembly', 'action': 'correct_reported_fields'},
        {'tools': ['read_assembly'], 'arguments': {'region_id': R, 'limit': 100}},
        {'tool': 'read_assembly', 'arguments': {'region_id': R, 'offset': 12}},
        {'tool': 'finish_analysis', 'action': 'correct_reported_fields'}])
    result = repair_read_routes(session, 'read_assembly', {'region_id': R}, packet, ValueError('read'))
    assert result['next_actions'][0]['action'] == 'reuse_read_page'
    assert result['next_actions'][0]['observation_id'] == O
    assert result['next_actions'][1]['arguments']['offset'] == 12
    assert result['next_actions'][2]['tool'] == 'finish_analysis'
    assert len(result['next_actions']) == 3 and session.observations == before
    assert result['error']['code'] == 'SOURCE_PAGE_ALREADY_READ'


def test_incomplete_short_page_does_not_block_larger_read():
    from cad_agent.analysis.read_context import matching_read_page
    observation = fixture_page(has_more=True, next_offset=1)
    assert matching_read_page({O: observation}, {'region_id': R, 'limit': 12}) is None
    assert matching_read_page({O: observation}, {'region_id': R, 'limit': 1}) == observation


def test_page_groups_keep_offsets_and_incomplete_state_separate():
    c = build(fixture_page(has_more=True, next_offset=1, checks={'complete_in_this_response': False}))
    eid = 'ev_' + '1' * 24
    c.canonical_evidence[eid] = evidence(evidence_id=eid, quote='4. 20厚岩棉板')
    page2 = fixture_page(offset=1)
    page2['observation_id'] = 'obs_' + '2' * 24
    page2['data']['items'] = [{'order': 4, 'evidence': c.canonical_evidence[eid]}]
    c.canonical_observations[page2['observation_id']] = page2
    result = c.encode_messages([{'role': 'user', 'content': '{}'}])
    groups = json.loads(result[0]['content'][len(PREFIX):])['source_groups']
    assert len(groups) == 2
    states = {g['offset']: (g['has_more'], g['page_complete']) for g in groups}
    assert states == {0: (True, False), 1: (False, True)}
    assert not any(m.get('content', '').startswith('已读来源状态') for m in result)


def test_frontier_keeps_unread_same_name_frames_and_excludes_other_family():
    from types import SimpleNamespace
    from cad_agent.analysis.read_context import lookup_frontier
    candidates = {
        'a': dict(assembly_heading='外墙：外墙1——有保温', region_ids=['a']),
        'b': dict(assembly_heading='外墙：外墙1——有保温', region_ids=['b']),
        'c': dict(assembly_heading='顶棚：顶棚3——外墙涂料顶棚', region_ids=['c'])}
    session = SimpleNamespace(request=SimpleNamespace(question='帮我找下外墙做法'),
        task=SimpleNamespace(data={'source_recall': {'queries': {'q': {'candidates': candidates}}}}),
        observations={}, index=SimpleNamespace(by_id={k: SimpleNamespace(frame_name=k) for k in candidates}))
    result = lookup_frontier(session)
    assert [r['region_id'] for r in result['unread_headings']] == ['a', 'b']
    session.observations[O] = fixture_page(target_region_id='a')
    assert [r['region_id'] for r in lookup_frontier(session)['unread_headings']] == ['b']
    assert session.task.data['source_recall']['queries']['q']['candidates'] == candidates
