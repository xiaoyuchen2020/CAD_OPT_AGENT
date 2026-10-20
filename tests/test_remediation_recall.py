"""Deterministic recall receipts, optional continuation and frozen Q18 inputs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.recall import pending_calls
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.tasks import TaskLedger, TaskPlan, PlanStep
from cad_agent.contracts import ChatRequest
from conftest import dxf_bytes


def fixture(workspace, tmp_path):
    def build(doc):
        for i, text in enumerate(['循环泵间','循环泵间','给水设备间','二层平面图']):
            doc.modelspace().add_text(text,dxfattribs={'insert':(i*300,0),'height':2})
        doc.layouts.new('另图').add_text('给水设备间',dxfattribs={'height':2})
    return workspace.ingest('召回.dxf',dxf_bytes(tmp_path,build))


def invoke(s, tool, args):
    result=json.loads(s.invoke_tool(tool,args))
    assert 'error' not in result,result
    return result


def test_explicit_alternatives_keep_distinct_source_entities(workspace,tmp_path):
    drawing=fixture(workspace,tmp_path)
    index=DrawingIndex(workspace,drawing['id'])
    result=index.entities(query='循环泵间',alternatives=['给水设备间','循环泵间'],limit=2)
    second=index.entities(query='循环泵间',alternatives=['给水设备间','循环泵间'],offset=2,limit=2)
    assert result['total']==4 and second['total']==4
    assert result['query_id']==second['query_id']
    rows=result['items']+second['items']
    assert len({r['entity_id'] for r in rows})==4
    assert sum(r['text']=='循环泵间' for r in rows)==2
    assert all(r['matched_query_variants'] for r in rows)
    assert result['engineering_room_count'] is None
    assert result['coverage']['source_read_state']=='unread'
    with pytest.raises(ValueError,match='8'):
        index.entities(alternatives=['x']*9)
    with pytest.raises(ValueError,match='过长'):
        index.entities(alternatives=['x'*151])


def test_alternative_group_all_and_unchecked_scopes(workspace,tmp_path,monkeypatch):
    drawing=fixture(workspace,tmp_path);index=DrawingIndex(workspace,drawing['id'])
    from cad_agent.analysis import retrieval
    monkeypatch.setattr(retrieval,'get_embedder',lambda *a:pytest.fail('Explicit alternatives must not call embedding'))
    layout=next(r.layout_id for r in index.regions if r.text=='循环泵间')
    result=index.search('原称',alternatives=['给水设备间'],keywords=['给水'],match='all',layout_id=layout,mode='hybrid')
    assert result['total']==1
    assert result['retrieval']['effective_mode']=='keyword'
    assert result['items'][0]['matched_query_variants']==['给水设备间','给水']
    assert result['coverage']['outside_scope_state']=='not_checked'
    missing=index.search('不存在的用途',layout_id=layout)
    assert missing['coverage']['match_state']=='not_matched_in_checked_scope'
    assert missing['coverage']['drawing_absence_proven'] is False


def test_lexical_can_page_beyond_former_top60(workspace,tmp_path):
    def build(doc):
        for i in range(76):
            doc.modelspace().add_text('独立机房'+str(i),dxfattribs={'insert':(i*300,0),'height':2})
    drawing=workspace.ingest('大量候选.dxf',dxf_bytes(tmp_path,build))
    index=DrawingIndex(workspace,drawing['id'])
    first=index.search('独立机房',mode='lexical',limit=12)
    tail=index.search('独立机房',mode='lexical',offset=72,limit=12)
    assert first['total']==76 and len(tail['items'])==4 and not tail['has_more']
    assert first['retrieval']['candidate_limit'] is None
    assert first['coverage']['candidate_pool_complete'] is True


def test_last_page_receipt_resume_reads_and_optional_finish(workspace,tmp_path):
    drawing=fixture(workspace,tmp_path)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取给水设备间文字。'))
    args=dict(query='给水设备间',offset=1,limit=1)
    last=invoke(s,'query_entities',args)
    assert last['recall_receipt']['first_unread_offset']==0
    assert last['recall_receipt']['coverage']['enumeration_state']=='unread_pages'
    ledger=s.task.data['source_recall']
    before=deepcopy(ledger)
    from cad_agent.analysis.object_registry import record_tool_result
    record_tool_result(s,'query_entities',args,last)
    assert s.task.data['source_recall']==before
    saved=TaskLedger.read(workspace,s.task.data['task_id'])
    assert saved['source_recall']==before
    resumed=CadAgentSession(workspace,s.request.model_copy(update={'resume_task_id':saved['task_id']}))
    pending=pending_calls(resumed.task.data)
    assert any(c['tool']=='query_entities' and c['arguments']['offset']==0 for c in pending)
    assert all(c['blocking'] is False for c in pending)
    assert {'query_entities','read_source_context'}<=resumed.focus.names()
    first=invoke(resumed,'query_entities',dict(query='给水设备间',offset=0,limit=1))
    q=resumed.task.data['source_recall']['queries'][first['query_id']]
    assert q['coverage']['enumeration_state']=='pages_exhausted'
    assert q['unread_source_candidate_count']==2
    call=next(c for c in pending_calls(resumed.task.data) if c['tool']=='read_source_context')
    read=invoke(resumed,call['tool'],call['arguments'])
    q=resumed.task.data['source_recall']['queries'][first['query_id']]
    assert q['unread_source_candidate_count']==1
    assert all(c['floor_state']==c['axis_state']=='not_checked' for c in q['candidates'].values())
    evidence=next(iter(resumed.evidence))
    answer=resumed.finish(json.dumps({'claims':[{'text':'原文出现给水设备间。','state':'observed','evidence_ids':[evidence]}]}))
    assert answer['claims'] # Optional unmatched candidates do not block a grounded limited statement.


def test_failed_observation_rolls_back_recall(workspace,tmp_path,monkeypatch):
    drawing=fixture(workspace,tmp_path)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取给水设备间。'))
    monkeypatch.setattr(s,'register_observation',lambda *a:(_ for _ in ()).throw(ValueError('synthetic failure')))
    result=json.loads(s.invoke_tool('query_entities',{'query':'给水设备间'}))
    assert result.get('error')
    assert 'source_recall' not in s.task.data


def test_cached_query_receipt_has_each_requirement_owner(workspace,tmp_path):
    drawing=fixture(workspace,tmp_path)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取给水设备间。核查其用途。'))
    s.task.plan(TaskPlan(steps=[PlanStep(requirement_id=p['id'],kind='lookup',acceptance='来源可读。') for p in s.task.data['subtasks']]))
    first=invoke(s,'search_sources',dict(query='给水设备间',limit=1,task_ids=['q1']))
    duplicate=invoke(s,'search_sources',dict(query='给水设备间',limit=1,task_ids=['q2']))
    assert duplicate['already_returned']
    assert duplicate['recall_receipt']['task_ids']==['q1','q2']
    query=s.task.data['source_recall']['queries'][first['query_id']]
    assert query['returned_candidate_count']==1
    assert next(c for c in pending_calls(s.task.data) if c['tool']=='search_sources')['task_ids']==['q1','q2']


def test_optional_recall_does_not_remove_necessary_operation_gate(workspace,tmp_path):
    drawing=fixture(workspace,tmp_path)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='测量给水设备间的长度。'))
    s.task.plan(TaskPlan(steps=[PlanStep(requirement_id='q1',kind='measure',acceptance='真实长度测量。',operations=['geometry'])]))
    invoke(s,'query_entities',dict(query='给水设备间',limit=1,task_ids=['q1']))
    assert any(c.get('blocking') is False for c in s.task.pending_work())
    assert any(c.get('blocking',True) for c in s.task.pending_work())
    from cad_agent.runtimes.responses_subscription import ResponsesSubscriptionRuntime
    assert ResponsesSubscriptionRuntime.recovery_kind(s,'{}')==('task_work_pending',True)
    with pytest.raises(ValueError,match='TASK_WORK_PENDING'):
        s.finish(json.dumps({
            'claims':[{'text':'尚未取得长度。','state':'unknown','question_part_ids':['q1']}],
            'requirement_outcomes':[dict(requirement_id='q1',status='unresolved',
                gap_kind='unread_source',reason='仍需执行长度测量。')],
        }))


def test_scope_next_call_is_optional_without_changing_its_identity():
    from cad_agent.analysis.scope_workflow import pending_scope_calls
    call=dict(call_id='stable-call',scope_id='scope',task_ids=['q1'],tool='read_dimensions',
              arguments=dict(layout_id='layout',frame_id='frame',offset=0,limit=5),operation='scope_measurement_next')
    data=dict(subtasks=[dict(id='q1',status='running')],active_scope_by_task={'q1':'scope'},
              observations={},scope_next_calls=[call])
    pending=pending_scope_calls(data)
    assert pending==[call|{'blocking':False}]
    assert data['scope_next_calls']==[call]


def replay_real_q18(workspace):
    root=Path(__file__).resolve().parents[1]/'artifacts/designer-facts-latest-20261019'
    db=root/'data/index.sqlite3'
    if not db.exists():pytest.skip('Frozen Q18 dataset not installed')
    old=json.loads((root/'Q18-B-task.json').read_text());sid=old['snapshot_id']
    with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as conn:
        drawing=json.loads(conn.execute("SELECT data FROM records WHERE kind='drawing' AND id=?",(sid,)).fetchone()[0])
    workspace.store.put('drawing',sid,drawing)
    hashes={}
    for name in ('source.dxf','regions.json','scene.json','entities.json'):
        src=root/'data/drawings'/sid/name
        hashes[name]=sha256(src.read_bytes()).hexdigest()
        dest=workspace.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=sid,question='读取水泵房和机房名称候选的来源，并核查图内一层标题。'))
    s.task.auto_plan()
    ids=[part['id'] for part in s.task.data['subtasks']]
    if s.task.needs_plan:
        s.task.plan(TaskPlan(steps=[PlanStep(requirement_id=i,kind='lookup',acceptance='读取原文候选，保留未核查状态。') for i in ids]))
    # Broad roots are explicit test queries, not a production hard-coded lexicon.
    query=dict(query='水泵房',alternatives=['泵房'],limit=5)
    pages=[];offset=0
    while True:
        row=invoke(s,'query_entities',query|{'offset':offset,'task_ids':ids})
        pages.append(row)
        if not row['has_more']:break
        offset=row['next_offset']
    all_items=[i for page in pages for i in page['items']]
    assert any(i['text']=='商业水泵房' for i in all_items)
    assert any(i['text']=='消防泵房' for i in all_items)
    assert len({i['entity_id'] for i in all_items})==len(all_items)
    equipment=invoke(s,'query_entities',dict(query='设备房',alternatives=['机房','电房','配电'],limit=1,task_ids=ids))
    assert equipment['has_more'] and equipment['recall_receipt']['first_unread_offset']==1
    titles=invoke(s,'search_sources',dict(query='一层平面图',limit=3,task_ids=ids))
    assert titles['total']>0
    title_receipt=deepcopy(titles['recall_receipt'])
    assert title_receipt['coverage']['source_read_state']=='unread'
    pump=next(i for i in all_items if i['text']=='商业水泵房')
    source=invoke(s,'read_source_context',dict(region_id=pump['source_region_ids'][0],neighbors=0,task_ids=ids))
    ledger=deepcopy(s.task.data['source_recall'])
    assert all(sha256((root/'data/drawings'/sid/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items())
    return dict(evidence_class='frozen_source_deterministic_replay',model_calls=0,embedding_calls=0,
        source_snapshot_id=sid,source_sha256=drawing['sha256'],historical_task_id=old['task_id'],
        historical_run_id=json.loads((root/'Q18-B-run.json').read_text())['id'],historical_inputs_unchanged=True,
        input_sha256=hashes,query=query,query_pages=pages,matched_source_entity_count=len(all_items),
        engineering_room_count=None,equipment_unread_page=equipment,title_query=titles,title_unread_receipt=title_receipt,
        pump_source_read=source,recall_ledger=ledger,budgeted_tool_calls=s.calls,
        limitations=['未确认房间功能、用途排除、楼层及共同轴网对应。','一层标题存在不证明完整首层主平面。',
                     '候选文字数不等于房间数；关联资料未提供不证明图内没有相关内容。'])


def test_real_q18_frozen_replay(workspace):
    replay_real_q18(workspace)
