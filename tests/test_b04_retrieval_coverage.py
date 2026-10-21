"""Object/field-directed retrieval must not erase missing or unbound facts."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from cad_agent.analysis.coverage import record_cells
from cad_agent.analysis.recall import pending_calls, _missing
from cad_agent.analysis.tasks import coverage_targets


def cells():
    return [dict(object=o,field=f,state='unread',evidence_ids=[]) for o in ('甲区','乙区') for f in ('面积','数量')]


def data():
    return dict(snapshot_id='s',source_sha256='hash',subtasks=[dict(id='q1',status='pending',calls=0,coverage=cells())])


def query(total=0, candidates=None):
    return dict(query_id='r',tool='search_sources',arguments=dict(query='甲区',keywords=['面积'],match='all',limit=3),
                task_ids=['q1'],total=total,first_unread_offset=None,candidates=candidates or {})


def ledger(d, queries):
    d['source_recall']=dict(context=dict(snapshot_id='s',source_sha256='hash'),queries=queries,read_spans={})


def test_cross_quote_and_cross_clause_cannot_pair_object_to_field():
    c=cells()
    record_cells(c,{}, {'e1':{'quote':'甲区'}, 'e2':{'quote':'乙区面积为100。数量为2。'}})
    assert c[0]['state']=='not_found_in_read_scope'
    assert c[2]['state']=='source_candidate' and c[2]['evidence_ids']==['e2']
    assert c[3]['state']=='attribution_unknown'
    assert all(i['state']!='addressed' for i in c)


def test_neighbor_and_truncated_source_cannot_discharge_target():
    for metadata in ({'relationship':'neighbor'}, {'relationship':'target','next_start':900}):
        c=cells(); ev=dict(evidence_id='e',quote='甲区面积100，数量2。')
        record_cells(c,{'items':[dict(evidence=ev,**metadata)]},{'e':ev})
        assert c[0]['state']!='source_candidate'
        assert c[1]['state']!='source_candidate'


def test_precise_evidence_ids_and_unknown_remain_distinct():
    c=cells(); c[1]['state']='unknown'
    record_cells(c,{}, {'a':{'quote':'甲区面积100，数量2。'},'b':{'quote':'乙区面积200。'}})
    assert c[0]['evidence_ids']==['a'] and c[2]['evidence_ids']==['b']
    assert c[1]['state']=='unknown' and c[3]['state']=='not_found_in_read_scope'


def test_explicit_transform_grammar_retains_special_objects_and_fields():
    result=coverage_targets('请把构造说明中的甲区、乙区和特殊部位分别整理成材料、等级及厚度表。')
    assert {c['object'] for c in result}=={'甲区','乙区','特殊部位'}
    assert {c['field'] for c in result}=={'材料','等级','厚度'}


def test_first_work_is_targeted_and_empty_read_queue_is_not_completion():
    d=data(); work=pending_calls(d)
    assert len(work)==2
    assert all(not c['arguments'].get('keywords') and len(c['coverage_gaps'])==2 for c in work)
    assert all(c['blocking'] is True for c in work)


def test_known_source_precedes_new_query_and_next_page():
    d=data(); c=dict(read_state='unread',region_ids=['region'],source_spans=[dict(region_id='region',text_span=[0,20])],start=0)
    q=query(total=3,candidates={'region':c}); q['first_unread_offset']=1
    ledger(d,{'r':q}); work=pending_calls(d)
    assert work[0]['tool']=='read_source_context'
    assert [w['priority'] for w in work]==sorted(w['priority'] for w in work)
    assert work[0]['coverage_gaps'] and work[0]['arguments']['neighbors']==0


def test_missing_match_does_not_trigger_unbounded_query_or_global_scan():
    d=data(); d['subtasks'][0]['coverage']=d['subtasks'][0]['coverage'][:1]
    q=query(); ledger(d,{'r':q})
    work=pending_calls(d); assert len(work)==1 and work[0]['arguments']['query']=='甲区'
    fallback=deepcopy(q); fallback['arguments']=dict(query='甲区',limit=3)
    d['source_recall']['queries']['fallback']=fallback
    assert pending_calls(d)==[]
    assert d['subtasks'][0]['coverage'][0]['state']=='unread'


def test_partial_scope_is_preserved_in_targeted_query():
    d=data(); d['subtasks'][0]['coverage']=d['subtasks'][0]['coverage'][:1]
    q=query(total=1); q['arguments']=dict(query='甲区',frame_id='frame',layout_id='layout')
    ledger(d,{'r':q}); call=pending_calls(d)[0]
    assert call['arguments']['frame_id']=='frame' and call['arguments']['layout_id']=='layout'


@pytest.mark.parametrize('stop', [dict(calls=12),dict(stalled=2),dict(status='partial'),dict(status='blocked_capability')])
def test_existing_budget_and_repetition_stop_all_continuations(stop):
    d=data();d['subtasks'][0].update(stop)
    assert pending_calls(d)==[]


def test_source_candidate_does_not_queue_unrelated_enumeration_or_complete():
    d=data()
    for c in d['subtasks'][0]['coverage']:c['state']='source_candidate'
    q=query(total=1000000);q['first_unread_offset']=0
    ledger(d,{'r':q})
    assert pending_calls(d)==[] and d['subtasks'][0]['status']=='pending'
    assert _missing([0,1,3],1000000000)==2


def test_field_only_query_does_not_enumerate_whole_drawing_for_every_object():
    d=data();q=query(total=100000)
    q['arguments']=dict(query='面积',limit=3);q['first_unread_offset']=3
    ledger(d,{'r':q})
    work=pending_calls(d)
    assert work and all(w['reason']=='explicit_object_field_gap' for w in work)
    assert all(w['arguments']['query'] in ('甲区','乙区') for w in work)


def test_same_read_is_deduplicated_without_losing_requirement_gaps():
    d=data(); second=deepcopy(d['subtasks'][0]);second['id']='q2';d['subtasks'].append(second)
    work=pending_calls(d)
    assert len(work)==2
    assert all(w['task_ids']==['q1','q2'] for w in work)
    assert all({g['requirement_id'] for g in w['coverage_gaps']}=={'q1','q2'} for w in work)


def test_comma_separated_different_objects_cannot_cross_pair_values():
    c=cells()
    record_cells(c,{}, {'e':{'quote':'甲区面积100，乙区数量20。'}})
    assert c[0]['state']=='source_candidate' and c[3]['state']=='source_candidate'
    assert c[1]['state']=='attribution_unknown' and c[2]['state']=='attribution_unknown'
