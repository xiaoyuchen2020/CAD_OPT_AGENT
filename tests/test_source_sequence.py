from copy import deepcopy
from types import SimpleNamespace as NS
import pytest
from cad_agent.contracts import SourceRegion
from cad_agent.analysis.source_sequence import requests,refresh,pending,summary
from cad_agent.analysis.recall import pending_calls
from cad_agent.analysis.gap_routing import record_result


def region(rid,text,parents=(),frame='f'):
    return SourceRegion(region_id=rid,text=text,context_region_ids=list(parents),layout_id='l',frame_id=frame,
                        frame_name=frame,kind='paragraph',entity_ids=[rid],bbox=[0,0,100,10],column_key='c')


def session(rows,question='请读取防水说明第7至9条。'):
    idx=NS(regions=rows,by_id={r.region_id:r for r in rows},snapshot='s',drawing={'sha256':'hash'})
    idx.scope=lambda section_id:[r for r in rows if r.region_id==section_id or section_id in r.context_region_ids]
    data=dict(question=question,snapshot_id='s',source_sha256='hash',
        subtasks=[dict(id='q1',requirement=question,status='pending',kind='lookup',calls=0,stalled=0,depends_on=[],delivered=False)],
        source_recall=dict(context=dict(snapshot_id='s',source_sha256='hash'),
            queries={'q':dict(arguments={'query':'防水'},candidates={'h':{'region_ids':['h']}},task_ids=['q1'],total=1,first_unread_offset=None)},read_spans={'h':[[0,len(rows[0].text)]]}))
    return NS(index=idx,task=NS(data=data))


def rows():
    return [region('h','四、防水工程'),region('a','7、基层处理。',['h']),region('b','8、材料：',['h']),
            region('b2','a、保留这一条件。',['h']),region('c','9、节点要求。',['h']),region('d','10、不在请求内。',['h'])]


@pytest.mark.parametrize('text,numbers',[('说明第1—3条',[1,2,3]),('说明第７～９条',[7,8,9]),('说明第12到14条',[12,13,14])])
def test_literal_range_grammar(text,numbers):
    r,=requests(text)
    assert list(range(r['first'],r['last']+1))==numbers
    assert not requests('轴线1—3，厚度7～9。')


def test_ranges_queue_unread_clauses_and_include_continuation_without_marking_delivered():
    s=session(rows());refresh(s)
    calls=pending(s.task.data,{'q1'})
    assert [c['arguments']['region_id'] for c in calls]==['a','b','c']
    ledger=s.task.data['source_recall'];ledger['read_spans']['b']=[[0,len(s.index.by_id['b'].text)]]
    refresh(s)
    item=s.task.data['source_sequences']['records'][0]['items'][1]
    assert item['state']=='partially_read'
    assert item['next_reads'][0]['region_id']=='b2'
    for r in rows()[1:-1]:ledger['read_spans'][r.region_id]=[[0,len(r.text)]]
    refresh(s)
    assert not pending(s.task.data,{'q1'})
    assert summary(s.task.data)[0]['state']=='fully_read'
    assert s.task.data['subtasks'][0]['delivered'] is False


def test_missing_duplicate_nested_and_foreign_chapter_numbers_do_not_fill_gap():
    rs=rows();rs=[r for r in rs if r.region_id not in ('b','b2')]
    rs += [region('nested','8）嵌套项',['h','a']),region('foreign','8、另一个章节',['other'])]
    s=session(rs);refresh(s)
    assert s.task.data['source_sequences']['records'][0]['items'][1]['state']=='not_located'
    rs=rows()+[region('dup','8、重复编号',['h'])];s=session(rs);refresh(s)
    assert s.task.data['source_sequences']['records'][0]['items'][1]['state']=='ambiguous'


def test_two_known_chapters_do_not_choose_one_or_merge_same_numbers():
    rs=rows()+[region('h2','五、防水工程',frame='other'),region('foreign','8、其他图框',['h2'],frame='other')]
    s=session(rs);s.task.data['source_recall']['read_spans']['h2']=[[0,len(rs[-2].text)]]
    refresh(s)
    assert summary(s.task.data)[0]['state']=='chapter_unresolved'
    assert not pending(s.task.data,{'q1'})


def test_partial_spans_require_entire_clause_and_wrong_snapshot_cannot_queue():
    s=session(rows());s.task.data['source_recall']['read_spans']['a']=[[0,2]]
    refresh(s)
    assert pending(s.task.data,{'q1'})[0]['arguments']['start']==2
    s.task.data['source_sequences']['snapshot_id']='other'
    assert not pending(s.task.data,{'q1'})


def test_failed_search_advice_uses_known_clause_without_resetting_limits():
    s=session(rows());refresh(s)
    # Exercise the existing recovery selector, not a separate automatic executor.
    s.trace=[dict(tool='search_sources',arguments={'query':'无匹配词'},task_ids=['q1'])]
    s.calls=3;s.policy=NS(stalled=2,stop_reason=None)
    s.request=NS(runtime='responses_subscription');s.w=NS(settings=NS(provider='responses_subscription',agent_max_tool_calls=40))
    # Existing query candidates carry source-read metadata normally recorded by recall.
    s.task.data['source_recall']['queries']['q']['candidates']={}
    record_result(s,'search_sources',['q1'],{'items':[],'total':0})
    gap,=s.task.data['gap_recovery'].values()
    assert gap['recommendation']['tool']=='read_source_context'
    assert gap['recommendation']['arguments']['region_id']=='a'
    assert s.calls==3
    s.task.data['subtasks'][0]['calls']=12
    assert not pending_calls(s.task.data)


def test_merged_numbered_paragraph_retains_exact_offsets():
    rs=[region('h','四、防水工程'),region('body','7、甲。\n8、乙。\n9、丙。',['h'])]
    s=session(rs);refresh(s)
    items=s.task.data['source_sequences']['records'][0]['items']
    assert [rs[1].text[slice(*i['source_spans'][0]['text_span'])].strip() for i in items]==['7、甲。','8、乙。','9、丙。']


def test_unread_range_cannot_be_declared_fulfilled_but_keeps_local_facts(workspace,demo_bytes):
    import json
    from test_remediation_delivery import read_fact
    s,fact=read_fact(workspace,demo_bytes,question='读取说明第2—4条。')
    assert not s.task.coverage_ready()
    with pytest.raises(ValueError,match='连续条文'):
        s.validate_answer(json.dumps({'claims':[fact],'requirement_outcomes':[
            dict(requirement_id='q1',status='fulfilled',gap_kind='none',reason='已完成')]}))
    result=s.validate_answer(json.dumps({'claims':[fact]}))
    saved=s.task.finalise(result)
    assert saved['status']=='partial' and result['claims'][0]['text']==fact['text']


def test_real_chapter_keeps_unnested_lettered_continuations_and_ignores_subheadings():
    import json
    from pathlib import Path
    fixture=json.loads((Path(__file__).parent/'fixtures/source-clause-sequence.json').read_text())
    rs=[SourceRegion.model_validate(r) for r in fixture['regions']]
    s=session(rs,question='请读取墙体说明第1—3条。')
    ledger=s.task.data['source_recall']
    ledger['queries']['q']['arguments']['query']='墙体'
    ledger['queries']['q']['candidates']={r.region_id:{'region_ids':[r.region_id]} for r in rs}
    ledger['read_spans']={fixture['heading']:[[0,len(rs[0].text)]]}
    refresh(s)
    record,=s.task.data['source_sequences']['records']
    assert record['chapter_id']==fixture['heading']
    second=record['items'][1]
    text='\n'.join(s.index.by_id[span['region_id']].text[slice(*span['text_span'])] for span in second['source_spans'])
    assert 'PC构件' in text and 'A3.5' in text
    assert '管道井隔墙' not in text  # item 3 stays a separate source obligation
    assert [i['number'] for i in record['items']]==[1,2,3]


def test_rejected_tool_payload_rolls_back_sequence_receipt(workspace,demo_bytes,monkeypatch):
    import json
    from test_factual_workflows import make
    from cad_agent.analysis import source_sequence
    s=make(workspace,demo_bytes,'读取说明第2—4条。');s.task.auto_plan()
    original=deepcopy(s.task.data['source_sequences']);seen=[]
    refresh_actual=source_sequence.refresh
    def receipt(session):
        refresh_actual(session)
        # Stand in for a freshly complete read, before wire admission. The
        # rejected payload must not persist any producer's completion receipt.
        session.task.data['source_sequences']['records'][0]['state']='fully_read'
        seen.append(True)
    monkeypatch.setattr(source_sequence,'refresh',receipt)
    s._tool_response_limit=100
    out=json.loads(s.invoke_tool('read_source_context',dict(region_id=s.index.regions[0].region_id,task_ids=['q1'])))
    assert seen and out['error']['code']=='RESULT_TOO_LARGE'
    assert s.task.data['source_sequences']==original
    assert not s.evidence and not s.observations
