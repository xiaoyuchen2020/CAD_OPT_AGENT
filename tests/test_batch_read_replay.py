"""Read-only replay of a frozen real DXF through the live tool/runtime paths."""
import hashlib
import json
import sqlite3
import shutil

import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from test_agent import call, final
from test_batch_reads import read, invoke
from test_remediation_assembly import FROZEN, FrozenIndex, verify_sources


def replay(workspace, monkeypatch, batching, assembly=False):
    run = json.loads((FROZEN / 'Q06-A-run.json').read_text())
    snapshot = run['snapshot_id']
    index = FrozenIndex(snapshot)
    with sqlite3.connect(f'file:{FROZEN / "data/index.sqlite3"}?mode=ro', uri=True) as conn:
        drawing = json.loads(conn.execute('SELECT data FROM records WHERE kind=? AND id=?', ('drawing', snapshot)).fetchone()[0])
    workspace.store.put('drawing', snapshot, drawing)
    target = workspace.store.root / 'drawings' / snapshot
    target.mkdir(parents=True, exist_ok=True)
    for name in ('regions.json', 'entities.json', 'scene.json'):
        # copy2 retains the frozen source's read-only mode. Replace only the
        # temporary replay copy when the second run uses the same workspace.
        (target / name).unlink(missing_ok=True)
        shutil.copy2(FROZEN / 'data/drawings' / snapshot / name, target / name)
    s = AgentRunner(workspace, ChatRequest(snapshot_id=snapshot, question='读取外墙构造原文。', mode='agent', source_read_batching=batching))
    s._index = index
    monkeypatch.setattr(s.w, 'regions', lambda sid: index.regions if sid == snapshot else [])
    rids = ['region_69f7a246408d77f73036bd92', 'region_66fe4f4f916d6216811c8851']
    reads = [read(rid, tool='read_assembly', limit=1) if assembly else read(rid, neighbors=0) for rid in rids]
    turns = []
    if assembly:
        if batching:
            out = invoke(s, reads)
            results = [i['result'] for i in out['items']]
        else:
            results = [json.loads(s.invoke_tool(r['tool'], r['arguments'])) for r in reads]
        assert all('error' not in value for value in results)
        for value in results:
            assert value['has_more'] and value['next_actions']
            assert not value['checks']['source_sequence_exhausted']
        result = None
    else:
        def turn(messages, tools, timeout):
            turns.append(messages)
            if batching and len(turns) == 1:
                assert 'batch_read_sources' in {t['function']['name'] for t in tools}
                return call('batch_read_sources', dict(reads=reads)), {}
            if not batching and len(turns) <= len(reads):
                r = reads[len(turns) - 1]
                return call(r['tool'], r['arguments'], len(turns)), {}
            return {'content': final(next(iter(s.evidence)))}, {}
        monkeypatch.setattr(workspace.gateway, 'chat_turn', turn)
        result = s.run()
    for value in s.evidence.values():
        verify_sources(index, value)
    return dict(model_rounds=len(turns), tool_calls=s.calls, evidence=s.evidence,
                citations=result['citations'] if result else None,
                batches=getattr(s, 'source_read_batches', []), snapshot_id=snapshot,
                input_sha256=run['input_sha256'])


@pytest.mark.parametrize('assembly', [False, True])
def test_frozen_dxf_equivalent_evidence_and_fewer_model_rounds(workspace, monkeypatch, assembly):
    if not FROZEN.exists():
        pytest.skip('Frozen DXF artifacts not distributed')
    sequential = replay(workspace, monkeypatch, False, assembly)
    batched = replay(workspace, monkeypatch, True, assembly)
    assert sequential['evidence'] == batched['evidence']
    assert sequential['citations'] == batched['citations']
    assert sequential['tool_calls'] == batched['tool_calls'] == 2
    if not assembly:
        assert [sequential['model_rounds'], batched['model_rounds']] == [3, 2]
    assert batched['batches'][0]['response_chars'] <= 50000
