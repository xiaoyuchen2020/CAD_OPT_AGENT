"""Behavioral regressions for source ownership, exposure and bounded execution."""
import json
from types import SimpleNamespace

import pytest

from cad_agent.cad.assembly_headings import HEADING, reading_columns
from cad_agent.cad.regions import reading_lines
from cad_agent.analysis.evidence_selection import publish
from cad_agent.analysis.task_progress import record, signature
from cad_agent.analysis.context_projection import replace_state, last_exchange, STATE_PREFIX


@pytest.mark.parametrize('text', ['外墙：外墙1——有保温', '外墙1——有保温', '外墙１（有保温）', '外墙面A1：涂料'])
def test_heading_original_offsets(text):
    m = HEADING.search(text)
    assert m and m.start() == 0
    assert text[m.start():m.end()] == m.group()


@pytest.mark.parametrize('text', ['这是外墙1——保温材料', '3. 参照外墙1（墙）', '外墙1型号与既有相同'])
def test_heading_not_a_body_mention(text):
    assert not HEADING.search(text)


def entity(text, x, y=0):
    return SimpleNamespace(text=text, origin=[x, y], height=10, rotation=0, kind='TEXT')


def test_wide_envelope_splits_three_reading_streams():
    items = [entity('外墙：外墙1——保温', 10, 100), entity('内墙：内墙1——涂料', 310, 100),
             entity('楼地面：楼地面1——砖', 610, 100), entity('1. 真石漆', 12, 80),
             entity('1. 内墙腻子', 312, 80), entity('1. 防滑地砖', 612, 80)]
    streams = reading_columns(items, (0, 900))
    assert len(streams) == 3
    assert {t.text for t in streams[0][1]} == {'外墙：外墙1——保温', '1. 真石漆'}
    assert len({str(key) for key, _ in streams}) == 3


def test_parallel_numbered_entities_never_concatenate():
    items = [entity('5. 3厚粘接砂浆；', 0), entity('6. 10厚防水砂浆；', 85)]
    assert len(reading_lines(items)) == 2
    # Ordinary wrapped fragments still can form a logical source line.
    assert len(reading_lines([entity('外墙', 0), entity('保温', 21)])) == 1


def test_partial_cannot_add_investigated_inner_wall_source():
    result = dict(answer='外墙为真石漆 [1]', claims=[dict(text='外墙为真石漆', state='observed',
        evidence_ids=['outer'], question_part_ids=['q1'])], citations=[
        dict(evidence_id='outer', quote='外墙为真石漆'), dict(evidence_id='inner', quote='内墙为腻子')])
    published = publish(result)
    assert [e['evidence_id'] for e in published['citations']] == ['outer']
    assert published['delivery_manifest']['claim_uses'][0]['evidence_ids'] == ['outer']
    assert 'source_excerpts' not in published


def test_missing_selected_reference_is_not_published():
    with pytest.raises(ValueError, match='DELIVERY_SOURCE_MISSING'):
        publish(dict(claims=[dict(text='外墙', evidence_ids=['invented'])], citations=[]))


def step():
    return dict(id='q1', requirement='帮我找下外墙做法', coverage=[], delivered=False)


def source(text, eid='a'):
    return dict(evidence_id=eid, snapshot_id='s', region_id='r', text_span=[0, len(text)], bbox=[0, 0, 10, 10], quote=text)


def test_unrelated_new_evidence_never_resets_requirement_stall():
    s = step()
    for i in range(3):
        e = source('内墙腻子' + str(i), str(i))
        delta = record(s, 'read_source_context', dict(items=[dict(evidence=e)]), {e['evidence_id']: e}, False)
        assert delta['kind'] == 'no_progress'
    assert s['quality_progress']['stalled'] == 3


def test_reissued_id_is_not_new_content_progress():
    s = step(); e = source('外墙1——保温')
    first = record(s, 'read_source_context', {}, {'a': e}, False)
    before = signature(dict(subtasks=[s]))
    second = record(s, 'read_source_context', {}, {'b': e | {'evidence_id': 'b'}}, False)
    assert first['kind'] == 'candidate_progress'
    assert second['kind'] == 'no_progress'
    assert signature(dict(subtasks=[s])) == before


def test_source_binding_is_distinct_from_candidate():
    s = step(); e = source('1. 真石漆')
    result = dict(source_binding=dict(state='source_bound', title_evidence=source('外墙1——保温')),
                  items=[dict(evidence=e)])
    assert record(s, 'read_assembly', result, {'a': e}, False)['kind'] == 'verified_progress'


def test_dynamic_state_replacement_does_not_grow_history():
    messages = [dict(role='user', content='外墙做法')]
    for i in range(8):
        messages = replace_state(messages, {'round': i})
    assert len(messages) == 2
    assert messages[-1]['content'] == STATE_PREFIX + '{"round":7}'


def test_last_exchange_preserves_all_parallel_call_pairs():
    messages = [dict(role='user', content='question'), dict(type='function_call', call_id='a'),
        dict(type='function_call', call_id='b'), dict(type='function_call_output', call_id='a'),
        dict(type='function_call_output', call_id='b')]
    assert last_exchange(messages) == messages[1:]
    assert last_exchange(messages[:-1]) == []


def test_lookup_profile_has_small_real_schema_and_prompt(workspace, demo_bytes):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    from cad_agent.runtimes.stable_prefix import StableTools
    from cad_agent.analysis.focus import PROMPT
    from cad_agent.runtimes.budget_policy import for_session
    drawing = workspace.ingest('fixture.dxf', demo_bytes)
    s = AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question='帮我找下外墙做法',
        mode='agent', runtime='responses_subscription', model_profile='siwc_sol'))
    schemas = StableTools(s).definitions
    names = {t['function']['name'] for t in schemas}
    assert {'read_assembly', 'read_source_context', 'search_sources', 'request_toolset'} <= names
    assert not names & {'locate_by_axes', 'measure_room_area', 'read_dimensions', 'trace_door_schedule'}
    assert len(s.system_prompt) < len(PROMPT) * .65
    assert len(names) < 15
    assert for_session(s).name == 'source_lookup_v2'
    assert not s.request.source_read_batching
    assembly_schema = next(t['function']['parameters'] for t in schemas if t['function']['name'] == 'read_assembly')
    assert assembly_schema['properties']['limit']['default'] == 12
    s.invoke_tool('read_assembly', {'region_id': s.index.regions[0].region_id})
    assert s.trace[-1]['arguments']['limit'] == 12


def test_projection_keeps_source_scope_units_and_whole_quotes_without_templates():
    from cad_agent.analysis.delivery_context import bounded_packet
    quote = source('4. 20厚岩棉板（厚度具体详节能专篇）；')
    scope = dict(frame_id='residential', frame_name='住宅楼工程做法表')
    obs = dict(observation_id='o', tool='read_assembly', data=dict(
        record_id='r', view='record', source_scope=scope,
        title_evidence=source('外墙1——保温', 'title'),
        items=[dict(evidence=quote, source_binding_state='source_bound')],
        source_binding=dict(state='source_bound', code='外墙1'),
        default_unit_clauses=[dict(unit='mm', applicability='linear_dimensions_only')],
        checks=dict(complete_in_this_response=True), diagnostics=[]))
    packet = bounded_packet(dict(evidence=[quote, obs['data']['title_evidence']], observations=[obs]),
                            10000, include_templates=False)
    projected = packet['observations'][0]['data']
    assert projected['source_scope'] == scope
    assert projected['default_unit_clauses'][0]['applicability'] == 'linear_dimensions_only'
    assert packet['evidence'][0]['quote'] == quote['quote']
    assert 'delivery_fields' not in projected


def test_reindex_is_versioned_idempotent_and_preserves_old_source(workspace, demo_bytes, monkeypatch):
    from cad_agent.cad import reader
    import cad_agent.cad.reindex as reindex_module
    old = workspace.ingest('fixture.dxf', demo_bytes)
    original = workspace.store.artifact(old['id'], 'regions.json').read_bytes()
    monkeypatch.setattr(reindex_module, 'PARSER_VERSION', reader.PARSER_VERSION + '-test-next')
    new = workspace.reindex(old['id'])
    assert new['id'] != old['id'] and new['sha256'] == old['sha256']
    assert new['reindexed_from'] == old['id']
    assert workspace.store.artifact(old['id'], 'regions.json').read_bytes() == original
    assert workspace.reindex(old['id'])['id'] == new['id']


def test_bound_inner_wall_body_cannot_be_reassigned_to_outer_wall():
    from cad_agent.analysis.assembly_delivery import ownership_issues
    from cad_agent.analysis.tool_registry import Claim
    e = source('1. 专用内墙腻子刮平')
    claim = Claim(text='外墙1的层次为专用内墙腻子刮平', state='observed', evidence_ids=['a'])
    # The claim must refer to only the target family for this narrow guard.
    claim.text = '外墙1的层次为水泥砂浆找平'
    e['quote'] = '1. 水泥砂浆找平'
    obs = {'o': dict(tool='read_assembly', data=dict(source_binding=dict(state='source_bound'),
        title_evidence=source('内墙1（砂浆）'), items=[dict(evidence=e)]))}
    assert ownership_issues(claim, {'a': e}, obs)
