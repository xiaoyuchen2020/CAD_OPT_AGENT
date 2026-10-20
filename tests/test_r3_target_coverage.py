"""Requirement-directed source reads and factual context, without model calls."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
from cad_agent.analysis.recall import pending_calls, record, coverage_feedback
from cad_agent.analysis.project_sources_workflow import pending_calls as catalog_calls
from cad_agent.analysis.retrieval import source_context_path
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest


def data(obj='某层每个目标分区', field='面积'):
    return dict(snapshot_id='s',source_sha256='hash',subtasks=[dict(id='q1',status='pending',calls=0,
        requirement='读取目标分区面积',coverage=[dict(object=obj,field=field,state='unread',evidence_ids=[])])],
        source_recall=dict(context=dict(snapshot_id='s',source_sha256='hash'),read_spans={},queries={}))


def query(d, query_word, preview='', terms=None):
    d['source_recall']['queries']['r']=dict(query_id='r',tool='search_sources',arguments=dict(query=query_word,limit=3),
        task_ids=['q1'],positions=[0],coverage={},first_unread_offset=3,total=4,candidates={'a':dict(read_state='unread',region_ids=['a'],
        source_spans=[dict(region_id='a',text_span=[0,12])],start=0,source_preview=preview,matched_terms=terms or [])})


def test_short_explicit_query_keeps_long_target_candidate_and_scope():
    d=data(); query(d,'目标分区')
    d['source_recall']['queries']['r']['arguments']['frame_id']='frame-a'
    pending=pending_calls(d)
    assert pending[0]['tool']=='read_source_context' and pending[0]['coverage_gaps'][0]['object']=='某层每个目标分区'
    page=next(c for c in pending if c['reason']=='unread_candidate_page')
    assert page['arguments']['frame_id']=='frame-a'


def test_field_search_reads_returned_relevant_object_but_not_unrelated_pages():
    d=data('甲区');query(d,'面积',preview='甲区面积200')
    pending=pending_calls(d)
    assert pending[0]['tool']=='read_source_context'
    assert not any(c['reason']=='unread_candidate_page' for c in pending)
    d['source_recall']['queries']['r']['candidates']['a']['source_preview']='乙区面积200'
    assert not any(c['tool']=='read_source_context' for c in pending_calls(d))


def test_parser_parent_paths_never_cross_frames_or_cycle():
    def region(rid, parents, frame='f'):
        return SimpleNamespace(region_id=rid,context_region_ids=parents,frame_id=frame,layout_id='l',text=rid)
    a=region('a',['p','outside']);p=region('p',['top']);top=region('top',['a'])
    index=SimpleNamespace(by_id={r.region_id:r for r in [a,p,top,region('outside',[],'other')]})
    path=source_context_path(index,a)
    assert [(p['region_id'],p['child_region_id']) for p in path]==[('p','a'),('top','p')]
    assert all(p['attribution_state']=='unverified' for p in path)


def test_unread_parent_is_priority_and_read_preview_never_counts():
    d=data();d['source_recall']['source_records']={'e':dict(evidence_id='e',task_ids=['q1'],
        source_context_path=[dict(region_id='p',text_span=[0,20],text_preview='父标题')])}
    call=pending_calls(d)[0]
    assert call['reason']=='read_source_parent_context' and call['arguments']==dict(region_id='p',start=0,neighbors=0)
    d['source_recall']['read_spans']['p']=[[0,5]]
    assert pending_calls(d)[0]['arguments']['start']==5
    d['source_recall']['read_spans']['p'].append([5,20])
    assert not any(c['reason']=='read_source_parent_context' for c in pending_calls(d))


def test_catalog_is_concrete_optional_task_owned_and_exhausts_once():
    d=data();d['subtasks'][0]['requirement']='核对该文件是否实际已提供'
    calls=catalog_calls(d)
    assert len(calls)==1 and calls[0]['task_ids']==['q1'] and calls[0]['blocking'] is False
    assert calls[0]['arguments']==dict(action='catalog')
    d['observations']={'catalog':dict(tool='read_project_sources',data=dict(action='catalog'))}
    assert catalog_calls(d)==[]
    d['observations']={};d['subtasks'][0]['stalled']=2
    assert catalog_calls(d)==[]



def test_related_read_does_not_charge_other_query_owner():
    d=data('甲区'); other=deepcopy(d['subtasks'][0]);other['id']='q2'
    other['coverage'][0]['object']='乙区';d['subtasks'].append(other)
    query(d,'面积',preview='甲区面积200')
    d['source_recall']['queries']['r']['task_ids']=['q1','q2']
    call=next(c for c in pending_calls(d) if c['tool']=='read_source_context')
    assert call['task_ids']==['q1']


def test_unregistered_feedback_fragment_cannot_count_as_read():
    d=data();query(d,'目标分区')
    d['source_recall']['context']['version']='source-recall-v1'
    session=SimpleNamespace(task=SimpleNamespace(data=d),drawing=dict(sha256='hash'),
        request=SimpleNamespace(snapshot_id='s',related_drawings=[]),trace=[dict(task_ids=['q1'])],evidence={})
    record(session,'read_source_context',{},dict(items=[dict(evidence_id='made-up',snapshot_id='s',region_id='a',
        text_span=[0,12],quote='目标分区面积12')]))
    assert session.task.data['source_recall']['read_spans']=={}
    assert next(c for c in pending_calls(d) if c['tool']=='read_source_context')['arguments']['region_id']=='a'

def frozen_replay(workspace):
    """Replay selected existing queries/read cursors against copied real assets.

    The scheduling probe reactivates a copied requirement before its final stop;
    it is not a resumed model run and does not revise historical answer scores.
    """
    root=Path(__file__).resolve().parents[1]/'artifacts/designer-facts-b-final-20261019'
    if not root.exists():pytest.skip('Frozen designer-facts dataset not installed')
    reports=[]
    for qid in ('Q03-B','Q09-B','Q12-B','Q19-B'):
        base=root/'attempts'/qid/'0001'
        old=json.loads((base/'task.json').read_text()); run=json.loads((base/'run.json').read_text())
        sid=old['snapshot_id']; db=base/'data/index.sqlite3'
        with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as connection:
            drawing=json.loads(connection.execute("SELECT data FROM records WHERE kind='drawing' AND id=?",(sid,)).fetchone()[0])
        workspace.store.put('drawing',sid,drawing)
        hashes={}
        for name in ('source.dxf','regions.json','scene.json','entities.json'):
            src=base/'data/drawings'/sid/name
            hashes[name]=sha256(src.read_bytes()).hexdigest()
            dest=workspace.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True)
            if not dest.exists():shutil.copy2(src,dest)
        session=CadAgentSession(workspace,ChatRequest(snapshot_id=sid,question=old['question']))
        assert session.request.source_read_batching is False
        session.task.data['subtasks']=deepcopy(old['subtasks'])
        for step in session.task.data['subtasks']:
            step.update(status='pending',calls=0,stalled=0)
        session.trace=[dict(task_ids=['q1'])]
        def execute(tool,args,ids=('q1',)):
            session.trace[-1]['task_ids']=list(ids)
            result=session.execute(tool,args)
            update=record(session,tool,args,result)
            session.register_observation(tool,result)
            session.task.data['observations']=deepcopy(session.observations)
            return result|update
        query_trace=next(t for t in run['trace'] if t['tool']=='search_sources')
        first=execute('search_sources',query_trace['arguments'])
        initial_pending=pending_calls(session.task.data)
        reads=[]
        if qid=='Q03-B':
            regions=[r for r in session.index.regions if '其他内墙（包括' in r.text or '除注明外，内墙采用' in r.text]
            assert len(regions)==2
            for region in regions:
                reads.append(execute('read_source_context',dict(region_id=region.region_id,neighbors=0)))
            feedback=coverage_feedback(session.task.data,['q1'],limit=24)
            clauses=[r for r in feedback['source_records'] if r['region_id'] in {r.region_id for r in regions}]
            assert len(clauses)==2
            assert all(r['source_context_path'] and r['source_context_path'][0]['evidence_ids'] for r in clauses)
            parents=[r['source_context_path'][0]['text_preview'] for r in clauses]
            assert any('1.3、住宅建筑' in p for p in parents) and any('1.4、除住宅外' in p for p in parents)
        elif qid in ('Q09-B','Q12-B'):
            # Re-read every successful original cursor before probing what the
            # model left unread; this must not merely replay already-read hits.
            historical_read_regions=set()
            for trace in run['trace']:
                if trace['status']=='completed' and trace['tool'] in ('search_sources','read_source_context'):
                    execute(trace['tool'],trace['arguments'])
                    if trace['tool']=='read_source_context':
                        historical_read_regions.add(trace['arguments']['region_id'])
            initial_pending=pending_calls(session.task.data)
            calls=[c for c in initial_pending if c['reason']=='read_returned_source_before_more_candidates']
            assert calls
            for call in calls[:2 if qid=='Q09-B' else 3]:
                assert call['arguments']['region_id'] not in historical_read_regions
                reads.append(execute(call['tool'],call['arguments']))
            assert coverage_feedback(session.task.data,['q1'])['source_records']
        else:
            calls=catalog_calls(session.task.data)
            assert calls and any('q2' in c['task_ids'] for c in calls)
            catalog=execute('read_project_sources',dict(action='catalog'),ids=calls[0]['task_ids'])
            assert len(catalog['items'])==1 and not catalog['cross_file_comparison_complete']
            assert catalog_calls(session.task.data)==[]
            reads.append(catalog)
        feedback=coverage_feedback(session.task.data,['q1'],limit=24)
        assert all(r['attribution_state']=='unverified' for r in feedback['source_records'])
        for name,digest in hashes.items():assert sha256((base/'data/drawings'/sid/name).read_bytes()).hexdigest()==digest
        reports.append(dict(case=qid,historical_run_id=run['id'],source_snapshot_id=sid,source_sha256=drawing['sha256'],
            input_sha256=hashes,historical_inputs_unchanged=True,initial_search=first,
            pending_before_reads=initial_pending,reads=reads,source_coverage=feedback,
            historical_trace_tools=[t['tool'] for t in run['trace']],source_read_batching=False))
    return dict(evidence_class='frozen_source_deterministic_replay',model_calls=0,embedding_calls=0,
        production_answer_acceptance=False,source_read_batching_default=False,cases=reports,
        limitations=['不证明生产模型会执行全部补读，不更改冻结评分。',
            '父标题是解析器结构证据，仍须逐对象核查；不自动确认语义归属。',
            '停车分区名称候选不等于已配对面积和停车数，不宣称统计或计算已完成。',
            '目录仅核对选定文件，不证明当前文件内缺少所引章节或外部资料。'])


def test_frozen_real_four_case_replay(workspace,monkeypatch):
    from cad_agent.analysis import retrieval
    monkeypatch.setattr(retrieval,'get_embedder',lambda *a:pytest.fail('No model or embedding calls in offline replay'))
    report=frozen_replay(workspace)
    assert len(report['cases'])==4
