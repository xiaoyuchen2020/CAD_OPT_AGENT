from cad_agent.analysis.task_progress import record, register_search_terms
from cad_agent.analysis.evidence_ledger import source_context
from cad_agent.contracts import SourcePart
from cad_agent.evidence import verify_source_evidence


def test_literal_search_expansion_keeps_reading_without_acceptance_credit():
    steps = [dict(id='q1', requirement='建筑设计使用年限如何表述', coverage=[dict(object='建筑设计使用年限')]),
             dict(id='q2', requirement='引用完整原文并定位', depends_on=['q1'], coverage=[dict(object='完整原文', requirement_type='SOURCE_FACT')])]
    register_search_terms(steps, ['q1','q2'], dict(query_variants=[['使用年限'],['设计工作年限']]))
    for s in steps:
        assert s['progress_search_terms'] == ['使用年限']
        search = dict(items=[dict(region_id='r', snippet='主体合理使用年限为50年')])
        assert record(s, 'search_sources', search, {}, False)['kind'] == 'candidate_progress'
        e = dict(evidence_id='e', snapshot_id='s', region_id='r', quote='主体合理使用年限为50年', text_span=[0,13], bbox=[0,0,10,10])
        assert record(s, 'read_source_context', dict(items=[dict(evidence=e)]), {'e':e}, False)['kind'] == 'candidate_progress'
        assert not s['quality_progress']['verified']
        assert record(s, 'read_source_context', dict(items=[dict(evidence=e)]), {'e':e}, False)['kind'] == 'no_progress'
        assert all('state' not in c for c in s['coverage'])


def test_search_drift_cannot_create_progress_terms():
    s=dict(id='q1',requirement='外墙做法',coverage=[dict(object='外墙')])
    register_search_terms([s],['q1'],dict(query_variants=[['内墙'],['墙'],['屋面']]))
    assert not s['progress_search_terms']
    assert record(s,'search_sources',dict(items=[dict(snippet='内墙做法')]),{},False)['kind']=='no_progress'


def test_native_fragments_keep_exact_text_and_independent_location(workspace,demo_bytes,monkeypatch):
    d=workspace.ingest('fragments.dxf',demo_bytes)
    r=workspace.regions(d['id'])[0]
    texts=['6','主体合理使用年限为50年。','防水涂料2.0mm厚。']
    parts=[];offset=0
    for i,text in enumerate(texts):
        parts.append(SourcePart(entity_id='part'+str(i),text_span=[offset,offset+len(text)],entity_text_span=[0,len(text)],bbox=[i*100,0,i*100+20,10]))
        offset+=len(text)
    r=r.model_copy(update=dict(text=''.join(texts),parts=parts,entity_ids=[p.entity_id for p in parts],context_region_ids=[]))
    monkeypatch.setattr(workspace,'regions',lambda _: [r])
    result=source_context(workspace,d['id'],r.region_id,neighbors=0)
    fragments=result['items'][0]['native_fragments']
    assert [f['evidence']['quote'] for f in fragments]==texts
    for i,f in enumerate(fragments):
        e=f['evidence'];assert f['native_part_complete']
        assert e['entity_ids']==['part'+str(i)] and e['bbox']==parts[i].bbox
        assert verify_source_evidence(e,snapshot_id=d['id'],region=r,drawing_unit=d['unit'])==e
    assert '防水' not in fragments[1]['evidence']['quote']
    assert result['items'][0]['evidence']['quote']==''.join(texts)
    paged=source_context(workspace,d['id'],r.region_id,neighbors=0,start=2)
    assert not paged['items'][0]['native_fragments'][0]['native_part_complete']


def test_plain_lookup_plan_has_no_invented_filter_but_still_needs_evidence(workspace,demo_bytes):
    import pytest
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.analysis.tasks import TaskPlan
    from cad_agent.contracts import ChatRequest
    d=workspace.ingest('lookup.dxf',demo_bytes)
    runner=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='图纸中门窗尺寸如何表述？请引用完整原文并定位。',mode='agent'))
    steps=[dict(requirement_id=p['id'],kind='lookup',operations=[],coverage=[],acceptance='读到支持本分项的原文并交付定位') for p in runner.task.parts]
    runner.task.plan(TaskPlan(steps=steps))
    assert all(not s['operations'] and not s['coverage'] for s in runner.task.data['subtasks'])
    assert not runner.task.data.get('evidence')
    with pytest.raises(ValueError):
        runner.finish('{"claims":[{"text":"门窗尺寸为900mm","state":"observed","question_part_ids":["q1","q2"]}],"requirement_outcomes":[{"requirement_id":"q1","status":"fulfilled","reason":"已完成"},{"requirement_id":"q2","status":"fulfilled","reason":"已完成"}]}')
