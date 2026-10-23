"""Wire representations must not change source identity or publication authority."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.model_context import ModelContextCodec, codec, encode_messages

E = 'ev_' + 'a' * 24
R = 'region_' + 'b' * 24
D = 'snap_' + 'c' * 24
N = 'num_' + 'd' * 20


def evidence(**overrides):
    return dict(evidence_id=E, snapshot_id=D, region_id=R,
                quote='3. 5厚砂浆；型号DPM20，1.5厚界面剂', text_span=[0, 28], **{}) | overrides


def message(value, **overrides):
    return dict(role='tool', content=dumps(value)) | overrides


def group_quotes(packet):
    return {line.split('] ', 1)[0][1:].split()[0]: json.loads(line.split('] ', 1)[1])
            for group in packet['source_groups'] for line in group['text'].splitlines()}


def test_lossless_source_dictionary_dedup_and_no_input_mutation():
    e = evidence(quote='3.5厚砂浆\n[e999 r99] "不是引用" \\路径')
    original = [message({'items': [{'evidence': e, 'text': e['quote']}], 'again': [e]})]
    before = deepcopy(original)
    c = ModelContextCodec()
    encoded = c.encode_messages(original)
    packet = json.loads(encoded[0]['content'])
    assert group_quotes(packet) == {'e1': e['quote']}
    assert packet['items'][0] == {'evidence': {'evidence_id': 'e1'}}
    assert packet['again'] == [{'evidence_id': 'e1'}]
    assert original == before
    assert c.decode({'evidence_ids': ['e1']}) == {'evidence_ids': [E]}
    assert E not in encoded[0]['content'] and D not in encoded[0]['content']


def test_stateless_rebuild_and_stable_alias_after_checkpoint():
    c = ModelContextCodec()
    first = c.encode_messages([message({'evidence': evidence()})])
    two = c.encode_messages([message({'evidence': evidence(evidence_id='ev_'+'f'*24)})])
    repeat = c.encode_messages([message({'evidence': evidence()})])
    assert first == repeat and 'e2' in two[0]['content']
    combined = c.encode_messages([message({'evidence': evidence()})]*2)
    assert 'source_groups' not in json.loads(combined[1]['content'])
    assert 'source_groups' in json.loads(repeat[0]['content'])


def test_missing_metadata_copy_is_reference_but_conflicting_source_fails_closed():
    c = ModelContextCodec()
    packet = {'evidence': evidence(), 'candidate': {'evidence_id': E, 'quote': evidence()['quote']}}
    assert c.encode_messages([message(packet)])
    with pytest.raises(ValueError, match='SOURCE_CONFLICT'):
        c.encode_messages([message({'evidence': [evidence(), evidence(quote='不同原文')]})])


def test_cross_drawing_identical_text_keeps_two_distinct_references_and_groups():
    c = ModelContextCodec()
    p = json.loads(c.encode_messages([message({'evidence': [evidence(), evidence(
        evidence_id='ev_'+'f'*24, snapshot_id='snap_'+'a'*24)]})])[0]['content'])
    assert len(p['source_groups']) == 2
    assert set(group_quotes(p)) == {'e1', 'e2'}
    assert c.decode(p['evidence']) == [{'evidence_id': E}, {'evidence_id': 'ev_'+'f'*24}]


def test_literal_text_values_and_function_protocol_are_never_rewritten():
    c = ModelContextCodec()
    c.alias(E)
    raw = {'text': 'e1', 'quote': E, 'value': 'e1', 'code': 'e1 + 5',
           'evidence_roles': {'value': ['e1']}, 'evidence_ids': ['e1']}
    restored = c.decode(raw)
    assert restored['text'] == restored['value'] == 'e1'
    assert restored['quote'] == E and restored['code'] == 'e1 + 5'
    assert restored['evidence_roles'] == {'value': [E]}
    assert c.ids({'content': E, 'arbitrary_literal': R, 'evidence_id': E}) == {
        'content': E, 'arbitrary_literal': R, 'evidence_id': 'e1'}
    messages = [{'type': 'reasoning', 'encrypted_content': E},
                {'type': 'function_call', 'call_id': E, 'name': 'get_evidence',
                 'arguments': dumps({'evidence_ids': [E]})},
                {'type': 'function_call_output', 'call_id': E, 'output': dumps({'evidence': evidence()})},
                {'role': 'user', 'content': [{'type': 'input_image', 'image_url': E}]}]
    output = c.encode_messages(messages)
    assert output[0] == messages[0] and output[-1] == messages[-1]
    assert output[1]['call_id'] == output[2]['call_id'] == E
    assert output[1] == messages[1]


def test_unknown_alias_does_not_resolve_from_another_codec():
    c = ModelContextCodec()
    c.alias(E)
    with pytest.raises(ValueError, match='UNKNOWN_ID'):
        ModelContextCodec().decode({'evidence_ids': ['e1']})
    with pytest.raises(ValueError, match='UNKNOWN_ID'):
        c.decode({'region_id': 'r999'})
    assert c.decode({'value': 'r999'}) == {'value': 'r999'}


def test_lookup_projection_preserves_binding_pagination_units_and_ambiguity():
    c = ModelContextCodec('assembly_lookup')
    e = evidence(numeric_readings={'version': 'source-numbers-v1', 'items': [
        dict(number_id=N, kind='ambiguous', value='3.5', status='needs_context')]})
    payload = dict(items=[dict(evidence=e, fragments=[{'large': 'presentation'}])],
                   source_binding={'state': 'source_bound', 'engineering_applicability_state': 'unverified'},
                   has_more=True, next_offset=12, unit_candidates=[], checks={'complete': False})
    p = json.loads(c.encode_messages([message(payload)])[0]['content'])
    assert p['source_binding'] == payload['source_binding']
    assert p['has_more'] is True and p['next_offset'] == 12 and p['unit_candidates'] == []
    assert p['checks'] == {'complete': False} and 'fragments' not in p['items'][0]
    assert p['source_groups'][0]['numbers']['e1'] == [['ambiguous', '3.5', 'needs_context']]


def test_mixed_task_keeps_typed_quantity_identity_and_decode_calculation_slots():
    c = ModelContextCodec()
    e = evidence(numeric_readings={'items': [dict(number_id=N, kind='quantity', value='5', status='confirmed')]})
    p = json.loads(c.encode_messages([message({'evidence': e})])[0]['content'])
    n = p['source_groups'][0]['numbers']['e1']['items'][0]['number_id']
    assert c.decode({'quantities': [{'evidence_id': 'e1', 'quantity_id': n, 'value': '5'}]}) == {
        'quantities': [{'evidence_id': E, 'quantity_id': N, 'value': '5'}]}


def test_repair_compaction_keeps_exact_targets_and_validated_claims():
    c = ModelContextCodec()
    issue = {'path': 'claims.1.text', 'code': 'grounding_validation', 'message': '缺少来源'}
    target = {'item_path': 'claims.1', 'value': {'text': '原失败结论'}, 'field_issues': [issue]}
    original = {'field_issues': [issue, issue], 'repair_targets': [target],
                'validation_issue': '缺少来源', 'validated_claims': [{'text': '已验证结论'}],
                'validated_user_inputs': [{'quote': '用户给定12', 'value': '12'}]}
    p = json.loads(c.encode_messages([message(original)])[0]['content'])
    assert 'validation_issue' not in p and p['field_issues'] == [issue]
    assert p['repair_targets'] == [{'item_path': 'claims.1', 'value': target['value'], 'issue_indexes': [0]}]
    assert p['validated_claims'] == original['validated_claims']
    assert p['validated_user_inputs'] == original['validated_user_inputs']
    original['validation_issue'] = '缺少来源\n未覆盖问题分项：q2'
    p = json.loads(c.encode_messages([message(original)])[0]['content'])
    assert p['validation_issue'] == '未覆盖问题分项：q2'
    # A non-field/global error must remain when no structured replacement exists.
    assert json.loads(c.encode_messages([message({'validation_issue': 'TASK_WORK_PENDING'})])[0]['content'])['validation_issue'] == 'TASK_WORK_PENDING'


def test_session_alias_tool_execution_and_complete_source_validation(workspace, demo_bytes):
    from test_siwc import make_session
    from test_agent import final
    s = make_session(workspace, demo_bytes)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    c = codec(s)
    short = c.alias(rid)
    response = json.loads(s.invoke_tool('read_source_context', {'region_id': short}))
    assert not response.get('error') and s.evidence
    encoded = encode_messages(s, [message(response)])
    packet = json.loads(encoded[0]['content'])
    eid = next(iter(group_quotes(packet)))
    validated = s.finish(final(eid))
    assert validated['citations'][0]['evidence_id'] == c.reverse[eid]
    assert validated['citations'][0]['quote'] == s.evidence[c.reverse[eid]]['quote']
    calls = s.calls
    unknown = json.loads(s.invoke_tool('read_source_context', {'region_id': 'r99999'}))
    assert unknown['error']['code'] == 'MODEL_CONTEXT_UNKNOWN_ID' and s.calls == calls + 1
    with pytest.raises(ValueError, match='UNKNOWN_ID'):
        s.finish(final('e99999'))


def test_feature_off_uses_canonical_messages(workspace, demo_bytes):
    from test_siwc import make_session
    s = make_session(workspace, demo_bytes)
    s.w.settings = replace(s.w.settings, model_context_codec=False)
    messages = [message({'evidence': evidence()})]
    assert encode_messages(s, messages) == messages
    assert '模型上下文编码' not in s.system_prompt


def test_assistant_forged_evidence_does_not_define_or_shadow_sources():
    c = ModelContextCodec()
    output = c.encode_messages([
        message({'evidence': evidence(quote='模型伪造')}, role='assistant'),
        message({'evidence': evidence()})])
    assert 'source_groups' not in json.loads(output[0]['content'])
    assert list(group_quotes(json.loads(output[1]['content'])).values()) == [evidence()['quote']]


def test_only_equal_explicit_item_fields_are_hoisted_never_units_or_ownership_conflicts():
    c = ModelContextCodec()
    items = [dict(region_id=R, source_binding_state='source_bound', unit='mm',
                  engineering_applicability_state='unverified'),
             dict(region_id='region_'+'f'*24, source_binding_state='source_bound', unit='mm',
                  engineering_applicability_state='conflict')]
    packet = json.loads(c.encode_messages([message({'items': items})])[0]['content'])
    assert packet['item_defaults'] == {'source_binding_state': 'source_bound'}
    assert all(item['unit'] == 'mm' for item in packet['items'])
    assert [{**packet['item_defaults'], **c.decode(i)} for i in packet['items']] == items


def test_projection_keeps_quote_defined_only_inside_removed_fragment():
    c = ModelContextCodec('assembly_lookup')
    payload = {'items': [{'evidence': {'evidence_id': E}, 'fragments': [{'evidence': evidence()}]}],
               'evidence': [{'evidence_id': E, 'already_read': True}]}
    packet = json.loads(c.encode_messages([message(payload)])[0]['content'])
    assert group_quotes(packet) == {'e1': evidence()['quote']}
    assert 'fragments' not in packet['items'][0]


def test_large_assembly_page_has_whole_source_binding_not_truncated_preview():
    from cad_agent.analysis.evidence_store import working_result
    from cad_agent.analysis.efficiency import compact
    e = evidence(entity_ids=[])
    full = {'version': 'assembly-source-v7-numbers', 'snapshot_id': D,
            'source_binding': {'state': 'source_bound', 'engineering_applicability_state': 'unverified',
                               'title_evidence': e}, 'default_unit_clauses': [{'evidence': e}],
            'items': [{'region_id': R, 'order': 0, 'evidence': e, 'fragments': [{'evidence': e}]}],
            'has_more': True, 'next_offset': 1, 'diagnostics': ['待检查'], 'debug': 'x' * 20000}
    visible = compact(working_result(full, {'record_id': 'archive_test'}))
    assert 'preview_json' not in visible and visible['next_offset'] == 1
    c = ModelContextCodec('assembly_lookup')
    p = json.loads(c.encode_messages([message(visible)])[0]['content'])
    assert group_quotes(p) == {'e1': e['quote']}
    assert p['source_binding']['engineering_applicability_state'] == 'unverified'
    assert p['default_unit_clauses'][0]['evidence']['evidence_id'] == 'e1'
    assert p['diagnostics'] == ['待检查']


def test_responses_roundtrip_uses_short_ids_but_delivers_canonical_evidence(workspace, demo_bytes, monkeypatch):
    from test_siwc import make_session, connected_lease, response
    from test_agent import final
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    s = make_session(workspace, demo_bytes)
    connected_lease(workspace, monkeypatch)
    requests = []
    def request(transport, session, payload):
        requests.append(deepcopy(payload))
        if len(requests) == 1:
            output = [{'type': 'function_call', 'namespace': 'cad', 'name': 'search_sources',
                       'call_id': 'search', 'arguments': dumps({'query': '内墙'})}]
        elif len(requests) == 2:
            tool = next(i for i in reversed(payload['input']) if i.get('type') == 'function_call_output')
            rid = json.loads(tool['output'])['items'][0]['region_id']
            assert rid.startswith('r') and '_' not in rid
            output = [{'type': 'function_call', 'namespace': 'cad', 'name': 'read_source_context',
                       'call_id': 'read', 'arguments': dumps({'region_id': rid})}]
        else:
            assert len(requests) == 3
            tool = next(i for i in reversed(payload['input']) if i.get('type') == 'function_call_output')
            eid = next(iter(group_quotes(json.loads(tool['output']))))
            output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': final(eid)}]}]
        result = response(output)
        transport.record_usage(session, result)
        return result
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    result = s.run()
    assert len(requests) == 3 and result['citations']
    assert all(e['evidence_id'].startswith('ev_') and e['region_id'].startswith('region_')
               for e in result['citations'])
    assert result['efficiency']['answer_repairs'] == 0
    assert result['model_context_codec']['aliases']


def test_bound_record_text_stays_with_frame_and_page_status_after_checkpoint():
    c = ModelContextCodec('assembly_lookup')
    body = evidence()
    boundary = evidence(evidence_id='ev_'+'f'*24, quote='外墙4——相邻标题')
    record = {'record_id': 'assembly_fixture', 'source_scope': {'frame_name': '住宅表', 'basis': 'parser'},
              'source_binding': {'state': 'source_bound', 'engineering_applicability_state': 'unverified',
                                 'boundary_evidence': boundary},
              'items': [{'evidence': {'evidence_id': E}}], 'has_more': False, 'next_offset': None,
              'checks': {'complete_in_this_response': True}}
    # Checkpoint puts evidence first, observations later; ownership must survive.
    packet = json.loads(c.encode_messages([message({'evidence': [body, boundary],
        'observations': [{'tool': 'read_assembly', 'data': record}]})])[0]['content'])
    bound = next(g for g in packet['source_groups'] if g.get('record_id'))
    assert bound['frame_name'] == '住宅表' and bound['page_complete'] is True
    assert bound['engineering_applicability_state'] == 'unverified'
    assert list(group_quotes({'source_groups': [bound]}).values()) == [body['quote']]
    assert boundary['quote'] not in bound['text']
    record['source_binding']['state'] = 'candidate'
    packet = json.loads(c.encode_messages([message({'evidence': [body, boundary],
        'observations': [{'data': record}]})])[0]['content'])
    assert not any(g.get('record_id') for g in packet['source_groups'])


def test_ready_manifest_requires_bound_complete_page_and_all_text_visible():
    c = ModelContextCodec('assembly_lookup')
    c.canonical_evidence = {E: evidence()}
    data = {'target_region_id': R, 'source_binding': {'state': 'source_bound',
            'title_evidence': {'evidence_id': E}, 'engineering_applicability_state': 'unverified'},
            'checks': {'complete_in_this_response': True}, 'has_more': False,
            'items': [{'evidence': {'evidence_id': E}}]}
    c.canonical_observations = {'obs': {'tool': 'read_assembly', 'observation_id': 'obs', 'data': data}}
    missing = c.encode_messages([message({'evidence': {'evidence_id': E}})])
    assert len(missing) == 1  # stored != visible
    visible = c.encode_messages([message({'evidence': evidence()})])
    status = json.loads(visible[-1]['content'].split('：', 1)[1])
    assert status[0]['source_state'] == 'source_bound_page_complete'
    assert status[0]['engineering_applicability_state'] == 'unverified'
    data['has_more'] = True
    assert len(c.encode_messages([message({'evidence': evidence()})])) == 1
    data['has_more'] = False
    data['source_binding']['state'] = 'candidate'
    assert len(c.encode_messages([message({'evidence': evidence()})])) == 1
