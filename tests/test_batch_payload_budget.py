"""Transport sizing must not change evidence, identity, calls or completion."""
import json
from types import SimpleNamespace

import pytest

from cad_agent.analysis.batch_reads import BatchReadSources, invoke_batch
from cad_agent.analysis.efficiency import dumps, evidence_ids
from test_batch_reads import make, read, invoke


def test_valid_read_over_old_7500_limit_is_delivered_once(workspace, demo_bytes, monkeypatch):
    s = make(workspace, demo_bytes)
    execute = s.execute
    def large(name, args, **kw):
        result = execute(name, args, **kw)
        result['extra_source_metadata'] = 'x' * 8000
        return result
    monkeypatch.setattr(s, 'execute', large)
    out = invoke(s, [read(s.index.regions[0].region_id, neighbors=0)])
    assert not out['items'][0]['result'].get('error')
    assert s.calls == len(s.trace) == 1
    assert 7500 < len(dumps(out)) <= 50000
    assert s.policy.response_chars == len(dumps(out))
    assert set(s.evidence) == s.policy.seen_evidence


def test_adaptive_page_keeps_exact_cursor_and_only_commits_visible_evidence(workspace, demo_bytes, monkeypatch):
    s = make(workspace, demo_bytes)
    regions = s.index.regions[:3]
    # Exercise the real evidence registration and bookkeeping with a deterministic
    # pageable reader. Separate tests replay the frozen production DXF reader.
    actual = s.execute
    attempted = []
    def paged(name, args, **kw):
        if name != 'read_assembly':
            return actual(name, args, **kw)
        attempted.append(dict(args))
        page = regions[args['offset']:args['offset']+args['limit']]
        items=[]
        for region in page:
            item=actual('read_source_context', dict(region_id=region.region_id, neighbors=0, start=0), **kw)['items'][0]
            items.append(item | {'source_metadata': 'x'*5000})
        after=args['offset']+len(page)
        return dict(items=items, total=len(regions), offset=args['offset'], has_more=after<len(regions),
                    next_offset=after if after<len(regions) else None,
                    next_actions=[dict(tool='read_assembly',arguments=dict(region_id=args['region_id'],offset=after,limit=args['limit']))] if after<len(regions) else [])
    monkeypatch.setattr(s, 'execute', paged)
    # A normal batch divides its unchanged 28k context among four read requests.
    requests=[read(regions[0].region_id, tool='read_assembly', offset=0, limit=3)] + [read(r.region_id,neighbors=0) for r in regions]
    out=invoke(s,requests)
    first=out['items'][0]['result']
    assert first['batch_pagination']['effective_limit'] == 1
    assert first['has_more'] and first['next_offset'] == 1
    assert first['next_actions'][0]['arguments']['offset'] == 1
    assert len(attempted)>1 and s.calls == len(out['items']) == 4
    assert len(s.observations) == 1
    full=next(iter(s.observations.values()))['data']
    assert len(full['items']) == 1  # rejected local attempts never become observations
    assert s.policy.key('read_assembly',attempted[0]) not in s.policy.cache
    assert s.policy.key('read_assembly',attempted[-1]) in s.policy.cache
    assert s.policy.response_chars == len(dumps(out))
    assert all(e in s.evidence for e in s.policy.seen_evidence)
    assert not any(t.get('error') for t in s.trace)


def test_errors_keep_original_code_when_large_task_metadata_would_mask_it(workspace, demo_bytes, monkeypatch):
    s=make(workspace,demo_bytes,'1. 内墙？\n2. 外墙？')
    s.task.auto_plan()
    context=s.task.context()
    monkeypatch.setattr(s.task,'context',lambda: context | {'long_status':'x'*55000})
    out=invoke(s,[read(s.index.regions[0].region_id)])
    assert out['items'][0]['result']['error']['code']=='INVALID_TOOL_INPUT'
    # Incremental repair actions and a lossless archive replace the former
    # bare error. The synthetic 55k task context must still never be replayed.
    assert len(dumps(out))<12000 and 'long_status' not in dumps(out)
    failed = out['items'][0]['result']
    from cad_agent.analysis.evidence_store import read as read_record, EvidenceRecordRead
    page = read_record(s, EvidenceRecordRead(record_id=failed['error_record']['record_id'], length=12000))
    chunks = [page['content']]
    while page['has_more']:
        page = read_record(s, EvidenceRecordRead(record_id=failed['error_record']['record_id'], start=page['next_start'], length=12000))
        chunks.append(page['content'])
    assert json.loads(''.join(chunks))['feedback']['error']['code']=='INVALID_TOOL_INPUT'
    assert not s.evidence and not s.observations and not s.policy.cache
    assert s.calls==1 and s.policy.response_chars==len(dumps(out))


def test_failed_oversize_has_original_tool_fallback_and_no_phantom_evidence(workspace, demo_bytes, monkeypatch):
    s=make(workspace,demo_bytes)
    actual=s.execute
    def oversized(name,args,**kw):
        return actual(name,args,**kw) | {'huge':'x'*60000}
    monkeypatch.setattr(s,'execute',oversized)
    rid=s.index.regions[0].region_id
    out=invoke(s,[read(rid,neighbors=0)])
    result=out['items'][0]['result']
    assert result['error']['code']=='RESULT_TOO_LARGE'
    assert result['retry_via']=='original_tool'
    assert result['next_actions'][0]['arguments']['region_id']==rid
    assert not s.evidence and not s.observations and not s.policy.cache
    assert not s.coverage.read_spans
    assert s.calls==1 and s.policy.response_chars==len(dumps(out))


def test_wire_space_defers_untouched_tail_without_charging_another_call(workspace,demo_bytes,monkeypatch):
    s=make(workspace,demo_bytes)
    # Legacy disables context stopping but retains the independent wire bound.
    s.policy.enabled=False
    requests=[read('r'+str(i)) for i in range(4)]
    def child(name,args):
        s.calls+=1
        s.trace.append(dict(tool=name,status='completed'))
        return dumps({'payload':'x'*48000})
    monkeypatch.setattr(s,'invoke_tool',child)
    result=json.loads(invoke_batch(s,BatchReadSources.model_validate(dict(reads=requests))))
    assert s.calls==1 and result['pending_reads']==requests[1:]
    assert len(dumps(result))<=50000 and s.policy.response_chars==len(dumps(result))


def test_complex_batch_context_returned_once_and_owner_ids_preserved(workspace,demo_bytes):
    s=make(workspace,demo_bytes,'1. 内墙？\n2. 外墙？')
    s.task.auto_plan()
    owners=[p['id'] for p in s.task.parts]
    out=invoke(s,[read(r.region_id,task_ids=[owner],neighbors=0) for r,owner in zip(s.index.regions,owners)])
    assert 'context' in out
    assert all('task' not in i['result'] and 'evidence_coverage' not in i['result'] for i in out['items'])
    assert [i['task_ids'] for i in out['items']]==[[o] for o in owners]
    assert s.policy.response_chars==len(dumps(out))


def test_frozen_dxf_adaptive_pages_recover_the_full_original_sequence(workspace,monkeypatch):
    import shutil
    import sqlite3
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.analysis.assembly import read as assembly_read
    from cad_agent.analysis.fact_tools import ReadAssembly
    from cad_agent.contracts import ChatRequest
    from test_remediation_assembly import FROZEN,FrozenIndex,verify_sources
    if not FROZEN.exists():pytest.skip('Frozen DXF artifacts not distributed')
    run=json.loads((FROZEN/'Q06-A-run.json').read_text())
    sid=run['snapshot_id']; index=FrozenIndex(sid)
    with sqlite3.connect(f'file:{FROZEN / "data/index.sqlite3"}?mode=ro',uri=True) as conn:
        drawing=json.loads(conn.execute('SELECT data FROM records WHERE kind=? AND id=?',('drawing',sid)).fetchone()[0])
    workspace.store.put('drawing',sid,drawing)
    dest=workspace.store.root/'drawings'/sid;dest.mkdir(parents=True,exist_ok=True)
    for filename in ('regions.json','entities.json','scene.json'):
        shutil.copy2(FROZEN/'data/drawings'/sid/filename,dest/filename)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=sid,question='读取外墙构造原文。',mode='agent',source_read_batching=True));s._index=index
    monkeypatch.setattr(workspace,'regions',lambda _:index.regions)
    rid='region_69f7a246408d77f73036bd92'
    expected=assembly_read(index,ReadAssembly(region_id=rid,limit=12))
    from cad_agent.cad.regions import is_heading
    short=[r for r in index.regions if not r.context_region_ids and len(r.text)<30 and not is_heading(r.text)][:3]
    assert len(short)==3
    out=invoke(s,[read(rid,tool='read_assembly',limit=12)]+[read(r.region_id,neighbors=0) for r in short])
    page=out['items'][0]['result']
    assert page.get('batch_pagination') and page['has_more']
    actual=[]
    while True:
        actual.extend((i['region_id'],i['order']) for i in page['items'])
        if not page['has_more']:break
        action=next(a for a in page['next_actions'] if a['tool']=='read_assembly' and a['arguments'].get('offset')==page['next_offset'] and a['arguments'].get('view')=='record')
        # Continue through the original independent reader; batch transport is
        # optional and its offered-tool gate remains unchanged.
        continuation=dict(action['arguments'],limit=min(12,expected['total']-action['arguments']['offset']))
        page=json.loads(s.invoke_tool(action['tool'],continuation))
        assert 'error' not in page,(s.calls,s.policy.response_chars,s.policy.stop_reason,len(actual))
    assert actual==[(i['region_id'],i['order']) for i in expected['items']]
    assert evidence_ids(expected)<=set(s.evidence)
    for evidence in s.evidence.values():verify_sources(index,evidence)
    assert not page['checks']['object_completeness']=='verified'
    assert all(i['ownership_state']=='unresolved' for i in page['items'])
    assert not any(t.get('error') for t in s.trace)
    assert s.policy.context_limit==28000  # never increase the test budget


def test_standalone_sized_source_is_deferred_before_any_call_or_evidence(workspace,demo_bytes,monkeypatch):
    from cad_agent.analysis import evidence_ledger
    s=make(workspace,demo_bytes,'1. 内墙？\n2. 外墙？\n3. 屋面？')
    s.task.auto_plan()
    regions=s.index.regions[:2]
    actual=evidence_ledger.source_context
    def large(*args,**kwargs):
        result=actual(*args,**kwargs)
        result['source_metadata']='x'*(32000 if kwargs.get('region_id')==regions[0].region_id else 22000)
        return result
    monkeypatch.setattr(evidence_ledger,'source_context',large)
    requests=[read(r.region_id,neighbors=0,task_ids=[s.task.parts[0]['id']]) for r in regions]
    out=invoke(s,requests)
    assert len(out['items'])==s.calls==len(s.trace)==1
    assert out['pending_reads']==requests[1:]
    assert not out['items'][0]['result'].get('error')
    assert all(e['region_id']!=regions[1].region_id for e in s.evidence.values())
    assert s.task.data['subtasks'][0]['calls']==1
    assert s.policy.response_chars==len(dumps(out))
    assert len(dumps(out))<=50000


def test_cancel_during_local_resize_does_not_commit_rejected_page(workspace,demo_bytes,monkeypatch):
    s=make(workspace,demo_bytes)
    actual=s.execute
    attempts=[]
    checkpoints=[]
    def execute(name,args,**kw):
        value=actual('read_source_context',dict(region_id=args['region_id'],neighbors=0,start=0),**kw)
        attempts.append(args['limit'])
        return value | {'large':'x'*60000}
    def check():
        if attempts:raise RuntimeError('cancel resize')
    s.execution=SimpleNamespace(check_cancelled=check,
        before_tool=lambda name,args:checkpoints.append(name),after_tool=lambda *args:None)
    monkeypatch.setattr(s,'execute',execute)
    requests=[read(s.index.regions[0].region_id,tool='read_assembly',limit=3)]
    with pytest.raises(RuntimeError,match='cancel resize'):invoke(s,requests)
    assert s.calls==len(attempts)==len(checkpoints)==1
    assert not s.evidence and not s.observations and not s.coverage.read_spans
    assert s.task.data['source_read_batch_progress']['completed_positions']==[]
    assert s.task.data['source_read_batch_progress']['pending_reads']==requests
    assert s._tool_response_limit==50000
