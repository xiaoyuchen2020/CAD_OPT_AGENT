"""Independent source batching preserves evidence, accounting and runtime gates."""
import json
from types import SimpleNamespace

import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
from test_agent import call, final


def make(workspace, demo_bytes, question='内墙材料是什么？'):
    d = workspace.ingest('batch.dxf', demo_bytes)
    return AgentRunner(workspace, ChatRequest(snapshot_id=d['id'], question=question, mode='agent', source_read_batching=True))


def read(rid, tool='read_source_context', task_ids=None, **arguments):
    return dict(tool=tool, arguments=dict(region_id=rid, **arguments), task_ids=task_ids or [])


def invoke(s, reads):
    return json.loads(s.invoke_tool('batch_read_sources', dict(reads=reads)))


def test_each_read_uses_existing_evidence_and_count(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:2]]
    out = invoke(s, reads)
    assert len(out['items']) == s.calls == len(s.trace) == 2
    assert all('error' not in i['result'] for i in out['items'])
    assert all(t['source_read_batch'] == 1 for t in s.trace)
    assert all(e['quote'] in json.dumps(out, ensure_ascii=False) for e in s.evidence.values())
    assert not out['pending_reads']
    controls = CodexSubscriptionRuntime()
    controls.remember_context('batch_read_sources', out)
    assert len(controls.source_contexts) == 2
    assert s.policy.response_chars == len(json.dumps(out, ensure_ascii=False, separators=(',', ':')))


@pytest.mark.parametrize('reads', [[], [dict(tool='calculate_facts', arguments={})],
    [dict(tool='batch_read_sources', arguments={})], [read('x')] * 5,
    [read('x', padding='x' * 17000)]])
def test_invalid_envelope_is_bounded_and_charged(workspace, demo_bytes, reads):
    s = make(workspace, demo_bytes)
    out = invoke(s, reads)
    assert out['error'] and s.calls == 1 and not s.evidence
    assert len(json.dumps(out)) < 50000


def test_foreign_source_failure_does_not_poison_valid_read(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    rid = s.index.regions[0].region_id
    out = invoke(s, [read('foreign'), read(rid, neighbors=0)])
    assert out['items'][0]['result']['error']
    assert out['items'][1]['result']['items'] and s.calls == 2
    assert all(e['region_id'] != 'foreign' for e in s.evidence.values())
    assert s.task.data['subtasks'][0]['calls'] == 2


def test_tool_limit_returns_exact_unexecuted_requests(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    s.calls = s.w.settings.agent_max_tool_calls - 1
    reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:3]]
    out = invoke(s, reads)
    assert len(out['items']) == 1 and out['pending_reads'] == reads[1:]
    assert s.calls == s.w.settings.agent_max_tool_calls
    assert s.task.data['source_read_batch_progress']['pending_reads'] == reads[1:]


def test_task_owners_do_not_leak_and_missing_owner_isolated(workspace, demo_bytes):
    s = make(workspace, demo_bytes, '1. 内墙材料是什么？\n2. 外墙材料是什么？')
    s.task.auto_plan()
    assert s.task.complex
    ids = [p['id'] for p in s.task.parts]
    regions = s.index.regions[:2]
    out = invoke(s, [read(regions[0].region_id, task_ids=[ids[0]], neighbors=0),
                     read(regions[1].region_id, task_ids=[ids[1]], neighbors=0), read('x')])
    assert out['items'][2]['result']['error'] and s.calls == 3
    for region, owner in zip(regions, ids):
        step = next(t for t in s.task.data['subtasks'] if t['id'] == owner)
        assert step['calls'] == 1
        assert step['evidence_ids']
        assert all(s.evidence[e]['region_id'] == region.region_id for e in step['evidence_ids'])


def test_large_child_rolls_back_unseen_evidence_and_other_reads_continue(workspace, demo_bytes, monkeypatch):
    s = make(workspace, demo_bytes)
    regions = s.index.regions[:2]
    execute = s.execute
    def large(name, args, **kw):
        value = execute(name, args, **kw)
        if args['region_id'] == regions[0].region_id:
            value['large'] = 'x' * 60000
        return value
    monkeypatch.setattr(s, 'execute', large)
    out = invoke(s, [read(r.region_id, neighbors=0) for r in regions])
    assert out['items'][0]['result']['error']['code'] == 'RESULT_TOO_LARGE'
    assert not out['items'][1]['result'].get('error')
    assert all(e['region_id'] != regions[0].region_id for e in s.evidence.values())
    assert all(e in s.evidence for e in s.policy.seen_evidence)
    assert len(json.dumps(out, ensure_ascii=False)) < 50000


def test_pagination_retains_exact_source_spans(workspace, demo_bytes, monkeypatch):
    s = make(workspace, demo_bytes)
    region = s.index.regions[0].model_copy(update={'text': '材料要求' * 1100, 'context_region_ids': []})
    monkeypatch.setattr(s.w, 'regions', lambda _: [region])
    out = invoke(s, [read(region.region_id, neighbors=0)])
    first = out['items'][0]['result']['items'][0]
    assert first['next_start'] == 2000 and first['truncated']
    second = invoke(s, [read(region.region_id, neighbors=0, start=first['next_start'])])
    item = second['items'][0]['result']['items'][0]
    assert item['evidence']['text_span'] == [2000, 4000]
    assert item['evidence']['quote'] == region.text[2000:4000]


def test_cancel_after_child_keeps_child_checkpoint_and_pending(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:2]]
    checkpoints = []
    def before(name, args):
        checkpoints.append(dict(tool=name, status='started'))
        return len(checkpoints)
    def after(checkpoint, result):
        checkpoints[checkpoint - 1]['status'] = 'completed'
        raise RuntimeError('cancel after completion')
    s.execution = SimpleNamespace(before_tool=before, after_tool=after, check_cancelled=lambda: None)
    with pytest.raises(RuntimeError, match='cancel'):
        invoke(s, reads)
    assert checkpoints == [dict(tool='read_source_context', status='completed')]
    assert s.task.data['source_read_batch_progress'] == dict(batch=1, completed_positions=[0], pending_reads=reads[1:])
    assert s.calls == 1 and s._tool_response_limit == 50000


def test_mock_model_native_runtime_reduces_independent_read_rounds(workspace, demo_bytes, monkeypatch):
    summaries = []
    for batching in (False, True):
        s = make(workspace, demo_bytes)
        reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:2]]
        turns = []
        def turn(messages, tools, timeout):
            turns.append(messages)
            if len(turns) == 1 and batching:
                assert 'batch_read_sources' in {t['function']['name'] for t in tools}
                return call('batch_read_sources', dict(reads=reads)), {}
            if not batching and len(turns) <= len(reads):
                r = reads[len(turns)-1]
                return call(r['tool'], r['arguments'], len(turns)), {}
            return {'content': final(next(iter(s.evidence)))}, {}
        monkeypatch.setattr(workspace.gateway, 'chat_turn', turn)
        result = s.run()
        summaries.append(dict(turns=len(turns), calls=s.calls, evidence=s.evidence, citations=result['citations']))
    assert [r['turns'] for r in summaries] == [3, 2]
    assert [r['calls'] for r in summaries] == [2, 2]
    assert summaries[0]['evidence'] == summaries[1]['evidence']
    assert summaries[0]['citations'] == summaries[1]['citations']


def test_four_maximum_child_packets_and_large_owner_ids_fit_wire_bound(workspace, demo_bytes, monkeypatch):
    from cad_agent.analysis.batch_reads import BatchReadSources, invoke_batch
    s = make(workspace, demo_bytes)
    s.policy.context_limit = 100000
    # The boundary dispatch already validated the bounded raw envelope; exercise
    # aggregation at the largest allowed child size without relying on demo size.
    reads = [read('r', task_ids=['q' * 3500]) for _ in range(4)]
    assert len(json.dumps(dict(reads=reads))) <= 16000
    packet = json.dumps({'payload': 'x' * 7486}, separators=(',', ':'))
    assert len(packet) == 7500
    def child(name, raw):
        s.calls += 1
        s.trace.append(dict(tool=name, status='completed'))
        s.policy.response_chars += len(packet)
        return packet
    monkeypatch.setattr(s, 'invoke_tool', child)
    out = invoke_batch(s, BatchReadSources.model_validate(dict(reads=reads)))
    assert len(out) <= 50000
    assert s.calls == 4


def test_subtask_limit_is_not_bypassed_by_batch(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    s.task.auto_plan()
    step = s.task.data['subtasks'][0]
    step['calls'] = 11
    reads = [read(r.region_id, task_ids=[step['id']], neighbors=0) for r in s.index.regions[:2]]
    out = invoke(s, reads)
    assert out['items'][1]['result']['error'] and s.calls == 2
    assert step['calls'] == 12


def test_context_budget_defers_remaining_reads(workspace, demo_bytes):
    s = make(workspace, demo_bytes)
    s.policy.context_limit = 500
    reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:2]]
    out = invoke(s, reads)
    assert s.calls == 1 and out['pending_reads'] == reads[1:]
    assert s.policy.stop_reason == 'tool_context_budget'


def test_responses_runtime_accepts_batch_and_finishes_without_extra_repair(workspace, demo_bytes, monkeypatch):
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    from test_siwc import make_session, connected_lease
    from test_siwc_stopping import answer, tool
    s = make_session(workspace, demo_bytes)
    s.request.source_read_batching = True
    connected_lease(workspace, monkeypatch)
    reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:2]]
    payloads = []
    def request(transport, session, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            assert 'batch_read_sources' in {t['name'] for ns in payload['tools'] for t in ns['tools']}
            return tool('batch_read_sources', dict(reads=reads), 'batch')
        assert len(payloads) == 2
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    result = s.run()
    assert len(payloads) == 2 and s.calls == 2
    assert result['citations'] and result['efficiency']['answer_repairs'] == 0


def test_disallowed_child_uses_normal_checkpoint_and_failed_call_accounting(workspace, demo_bytes, monkeypatch):
    s = make(workspace, demo_bytes)
    s.task.auto_plan()
    monkeypatch.setattr(s, 'offered_tools', lambda **kw: [])
    checkpoints = []
    def before(name, args):
        checkpoints.append(name)
        return name
    s.execution = SimpleNamespace(before_tool=before, after_tool=lambda *a: None, check_cancelled=lambda: None)
    reads = [read(r.region_id, neighbors=0) for r in s.index.regions[:2]]
    out = invoke(s, reads)
    assert all(i['result']['error'] for i in out['items'])
    assert s.calls == s.task.data['subtasks'][0]['calls'] == 2
    assert checkpoints == ['read_source_context', 'read_source_context']
    assert not s.evidence
