"""Final synthesis must not resurrect sources omitted by its bounded packet."""
from copy import deepcopy
from types import SimpleNamespace as NS
import json

import pytest

from cad_agent.analysis.delivery_context import bounded_packet, _reference_ids
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.final_context_budget import fit_messages
from cad_agent.analysis.model_context import codec, encode_messages


def populate(session, count=39):
    session.evidence.clear()
    session.observations.clear()
    for i in range(count):
        eid, rid, oid = (prefix + f'{i:024x}' for prefix in ('ev_', 'region_', 'obs_'))
        quote = f'来源{i}：' + '3. 5厚砂浆；型号DPM20，1.5厚界面剂。' * 90
        evidence = dict(evidence_id=eid, region_id=rid, snapshot_id=session.request.snapshot_id,
                        quote=quote, text_span=[0, len(quote)], entity_ids=[])
        session.evidence[eid] = evidence
        session.observations[oid] = dict(observation_id=oid, tool='read_assembly', data=dict(
            record_id=f'assembly_{i}', target_region_id=rid, snapshot_id=session.request.snapshot_id,
            view='record', offset=0, total=1, has_more=False, next_offset=None,
            source_scope={'frame_name': f'图框{i}', 'basis': 'source_title'},
            source_binding={'state': 'source_bound', 'engineering_applicability_state': 'unverified'},
            checks={'complete_in_this_response': True},
            items=[{'order': 3, 'evidence': evidence, 'source_binding_state': 'source_bound'}]))
    return dict(question=session.request.question, task={}, instruction='逐项引用原文，保留遗漏说明。',
                evidence=list(session.evidence.values()), observations=list(session.observations.values()))


def session_fixture():
    return NS(request=NS(question='帮我找下地下室内墙4做法', snapshot_id='snap_' + 'a' * 24,
                         parent_run_id=None, related_drawings=[]),
              w=NS(settings=NS(model_context_codec=True, read_context_optimization=True)),
              task=NS(data={'subtasks': []}, complex=False),
              evidence={}, observations={}, runtime_diagnostics={}, visual_images={})


def test_final_packet_fits_without_reintroducing_archived_pages():
    session = session_fixture()
    original = populate(session)
    packet = bounded_packet(original, 12000, include_templates=False)
    before = deepcopy((packet, session.evidence, session.observations))
    # Both checkpoints and final encoding respect the bounded selection.
    restored = encode_messages(session, [{'role': 'user', 'content': dumps(packet)}])
    assert sum(len(m['content']) for m in restored) <= 12000
    messages = fit_messages(session, packet, 12000)
    assert sum(len(m['content']) for m in messages) <= 12000
    assert (packet, session.evidence, session.observations) == before
    decoded = codec(session).decode(json.loads(messages[0]['content']))
    retained = {e['evidence_id'] for e in decoded['evidence']}
    assert retained and retained < session.evidence.keys()
    assert decoded['context_omissions']['evidence_count'] == len(session.evidence) - len(retained)
    for observation in decoded['observations']:
        assert _reference_ids(observation) <= retained
        assert observation['data']['source_binding']['engineering_applicability_state'] == 'unverified'
    quotes = [json.loads(line.split('] ', 1)[1]) for group in decoded['source_groups']
              for line in group['text'].splitlines()]
    assert set(quotes) == {session.evidence[eid]['quote'] for eid in retained}
    omitted = session.evidence.keys() - retained
    assert all(session.evidence[eid]['quote'] not in dumps(messages) for eid in omitted)
    assert session.runtime_diagnostics['final_context_fit'][-1]['status'] == 'fits'
    # This decision is local to final encoding; a subsequent exploration can restore history.
    encode_messages(session, [{'role': 'user', 'content': '{}'}])
    assert codec(session).last_stats['read_working_set']['pages'] == 39


def test_empty_final_packet_keeps_omissions_without_restoring_ready_or_navigation():
    session = session_fixture()
    populate(session, count=2)
    packet = dict(question=session.request.question, evidence=[], observations=[],
                  context_omissions={'evidence_count': 2, 'observation_count': 2})
    messages = fit_messages(session, packet, 3000)
    assert len(messages) == 1
    assert json.loads(messages[0]['content']) == packet


def test_bounded_encoding_keeps_selected_binding_without_adding_other_ready_pages():
    session = session_fixture()
    populate(session, count=2)
    selected, omitted = session.observations.values()
    # A different page can reuse the same source text; seeing that quote alone
    # must not restore the omitted page's ownership or completion metadata.
    omitted['data']['items'] = deepcopy(selected['data']['items'])
    c = codec(session)
    c.read_frontier = {'unread_headings': [{'title': '未选中的导航标题'}]}
    packet = dict(evidence=[next(iter(session.evidence.values()))], observations=[selected])
    messages = c.encode_messages([{'role': 'user', 'content': dumps(packet)}], restore_read_context=False)
    assert len(messages) == 1
    assert omitted['data']['record_id'] not in dumps(messages)
    assert '未选中的导航标题' not in dumps(messages)
    decoded = c.decode(json.loads(messages[0]['content']))
    assert decoded['observations'][0]['data']['source_binding'] == selected['data']['source_binding']
    assert decoded['source_groups'][0]['record_id'] == selected['data']['record_id']
    assert c.optimize_reads is True


@pytest.mark.parametrize('runtime_name', ['direct', 'harness'])
def test_subscription_final_request_uses_only_bounded_sources(
        workspace, demo_bytes, monkeypatch, runtime_name):
    from dataclasses import replace
    from test_siwc import make_session, connected_lease
    from test_siwc_stopping import answer
    from cad_agent.runtimes import responses_subscription as runtime
    from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
    from cad_agent.runtimes.budget_policy import STANDARD
    from cad_agent.runtimes.siwc_bridge import SiwcBridge

    session = make_session(workspace, demo_bytes, allow_fallback=False)
    session.request = session.request.model_copy(update={'question': '帮我找下地下室内墙4做法'})
    connected_lease(workspace, monkeypatch)
    session.invoke_tool('read_source_context', {'region_id': session.index.regions[0].region_id})
    packet = bounded_packet(populate(session), 12000, include_templates=False)
    before = deepcopy((session.evidence, session.observations))
    monkeypatch.setattr(session, 'final_context', lambda *args: packet)
    monkeypatch.setattr(CodexSubscriptionRuntime, 'stop_reason', lambda *args: 'tool_call_budget')
    monkeypatch.setattr(runtime, 'for_session', lambda *args: replace(STANDARD, final_context_chars=12000))
    sent = []

    def request(self, active_session, payload):
        sent.append(payload)
        assert sum(len(m.get('content', '')) for m in payload['input']) <= 12000
        assert payload['tool_choice'] == 'none'
        return answer(json.dumps({'claims': [{'text': '本次尚未取得完整资料。', 'state': 'unknown',
                                              'question_part_ids': ['q1']}]}))

    monkeypatch.setattr(runtime.ResponsesTransport, 'request', request)
    if runtime_name == 'direct':
        runtime.ResponsesSubscriptionRuntime().run(session)
    else:
        session.runtime_diagnostics = {'model_requests': []}
        bridge = SiwcBridge(session)
        bridge.controls.FINAL_CONTEXT_CHARS = 12000
        bridge.complete([{'role': 'user', 'content': session.request.question}], [], True, {})
    assert len(sent) == 1
    assert (session.evidence, session.observations) == before
