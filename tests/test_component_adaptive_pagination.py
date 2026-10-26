"""No geometry/model work: stored source-bound component records through real APIs."""
from copy import deepcopy
from dataclasses import replace
import json
import pytest
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.components.service import ComponentService
from conftest import dxf_bytes


def record(count=15, chars=4000):
    return dict(record_id='component_paging',snapshot_id='snapshot',source_sha256='source',kind='relations',layout_id='model',
                unit='mm',status='needs_review',evidence_url='/evidence',rooms=[],walls=[],relations=[],
                openings=[dict(object_id=f'opening_{i}',opening_type='unknown',operation_type='unknown',entity_ids=[f'entity_{i}_{j}' for j in range(80)],
                               source_evidence={'literal':'源'*chars,'unknown_reason':'source_does_not_establish_identity'}) for i in range(count)])


def test_large_page_shrinks_without_losing_unknown_or_source():
    r=record();before=deepcopy(r)
    page=ComponentService.page(r,'openings',0,20)
    assert 1<=page['effective_limit']<20 and page['next_offset']==page['effective_limit']
    assert page['items']==r['openings'][:page['effective_limit']]
    assert len(page['items'][0]['entity_ids'])==80
    assert page['pagination']['items_truncated'] is False
    assert len(json.dumps(page,ensure_ascii=False))<=40000
    assert r==before


def test_small_page_budget_adapts_and_impossible_budget_is_explicit():
    r=record()
    p=ComponentService.page(r,'openings',0,1,page_call_budget=11)
    assert p['requested_limit']==1 and p['effective_limit']==2 and p['next_offset']==2
    assert p['pagination']['adjustment']=='increased_for_call_budget'
    assert p['pagination_budget']['fits_remaining_call_budget'] is True
    p=ComponentService.page(r,'openings',0,1,page_call_budget=1)
    assert p['pagination_budget']['fits_remaining_call_budget'] is False
    assert p['pagination_budget']['unread_after_page']==15-p['effective_limit']
    assert p['total']==15


def test_oversized_item_is_not_skipped_and_empty_pages_terminate():
    r=record(1,60000)
    with pytest.raises(ValueError,match='COMPONENT_ITEM_TOO_LARGE'):ComponentService.page(r,'openings',0,20)
    r=record(3,10);r['openings'][1]['source_evidence']['literal']='x'*60000
    p=ComponentService.page(r,'openings',0,20)
    assert p['effective_limit']==1 and p['next_offset']==1
    assert p['pagination_budget']['blocked_item_offset']==1
    with pytest.raises(ValueError,match='COMPONENT_ITEM_TOO_LARGE'):ComponentService.page(r,'openings',1,20)
    p=ComponentService.page(record(0),'openings',0,20)
    assert not p['items'] and p['next_offset'] is None and not p['has_more']


@pytest.fixture
def stored(workspace,tmp_path):
    drawing=workspace.ingest('pagination.dxf',dxf_bytes(tmp_path,lambda d:d.modelspace().add_text('候选',dxfattribs={'insert':(0,0),'height':1})))
    r=record();r.update(snapshot_id=drawing['id'],source_sha256=drawing['sha256'],layout_id=drawing['layouts'][0]['layout_id'])
    workspace.store.put('component',r['record_id'],r)
    return drawing,r


def new_session(workspace,drawing):
    workspace.settings=replace(workspace.settings,agent_max_tool_calls=12)
    return CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取门窗洞口候选',mode='agent',agent_policy='efficient'))


def test_real_session_limit_one_reads_all_in_budget_without_changing_ids(workspace,stored):
    drawing,r=stored;s=new_session(workspace,drawing);seen=[];offset=0;calls=0
    while offset is not None:
        out=json.loads(s.invoke_tool('get_component_evidence',dict(record_id=r['record_id'],view='openings',offset=offset,limit=1)))
        assert 'error' not in out,(out,s.calls,s.policy.stop_reason,s.policy.response_chars,s.policy.context_limit,getattr(s.policy,'geometry_work',None))
        assert len(json.dumps(out,ensure_ascii=False))<=50000
        seen.extend(i['object_id'] for i in out['items']);offset=out['next_offset'];calls+=1
        assert all(i['opening_type']=='unknown' and len(i['entity_ids'])==80 for i in out['items'])
    assert seen==[i['object_id'] for i in r['openings']]
    assert len(set(seen))==15 and calls<=11 and s.calls==calls
    assert workspace.settings.agent_max_tool_calls==12
    assert workspace.store.get('component',r['record_id'])==r


def test_http_size_adaptation_and_session_duplicate_have_exact_next_offset(workspace,stored,monkeypatch):
    drawing,r=stored
    monkeypatch.setattr(ComponentService,'build',lambda self,kind,args:deepcopy(r))
    with TestClient(create_app(workspace.settings)) as client:
        response=client.post(f'/api/drawings/{drawing["id"]}/components/relations',json={'layout_id':r['layout_id'],'bbox':[0,0,10,10],'view':'openings','offset':0,'limit':20})
        assert response.status_code==200,response.text
        page=response.json();assert page['next_offset']==len(page['items'])<15
        assert page['pagination_budget']['available_page_calls_including_current'] is None
    s=new_session(workspace,drawing);args=dict(record_id=r['record_id'],view='openings',offset=0,limit=20)
    first=json.loads(s.invoke_tool('get_component_evidence',args));observations=deepcopy(s.observations)
    repeat=json.loads(s.invoke_tool('get_component_evidence',args))
    assert repeat.get('already_returned') is True,(repeat,s.policy.stop_reason,s.policy.response_chars)
    assert repeat['next_offset']==first['next_offset'] and repeat['effective_limit']==first['effective_limit']
    assert s.observations==observations


def test_single_extreme_tool_result_has_no_observation_or_read_receipt(workspace,stored):
    drawing,r=stored;r['openings'][0]['source_evidence']['literal']='x'*60000
    workspace.store.put('component',r['record_id'],r)
    s=new_session(workspace,drawing)
    out=json.loads(s.invoke_tool('get_component_evidence',dict(record_id=r['record_id'],view='openings',limit=20)))
    assert out.get('error')
    assert not s.observations
    assert not s.task.data['observations']


def test_heterogeneous_pages_pack_suffix_and_never_promise_impossible_budget():
    r=record(6,10)
    for i,size in enumerate([1000,1000,1000,22000,22000,22000]):
        r['openings'][i]['source_evidence']['literal']='x'*size
    p=ComponentService.page(r,'openings',0,1,page_call_budget=3)
    # Taking only two small items strands the third small item in an extra page.
    assert p['effective_limit']==4 and p['pagination_budget']['fits_remaining_call_budget']
    assert p['pagination_budget']['requested_limit_fits_budget'] is False
    r=record(40,10)
    for item in r['openings'][20:]:item['source_evidence']['literal']='x'*22000
    p=ComponentService.page(r,'openings',0,20,page_call_budget=2)
    assert p['pagination_budget']['minimum_required_page_calls']==21
    assert p['pagination_budget']['requested_limit_fits_budget'] is False
    assert p['pagination_budget']['fits_remaining_call_budget'] is False


def test_selected_subtask_budget_bounds_larger_global_budget(workspace,stored):
    drawing,r=stored;s=new_session(workspace,drawing)
    workspace.settings=replace(workspace.settings,agent_max_tool_calls=24)
    s.task.auto_plan();owner=s.task.data['subtasks'][0];owner['calls']=10
    out=json.loads(s.invoke_tool('get_component_evidence',dict(record_id=r['record_id'],view='openings',limit=1)))
    assert out['pagination_budget']['available_page_calls_including_current']==2
    assert out['effective_limit']>1
    assert out['pagination']['returned_range']==[0,len(out['items'])]
    assert out['pagination_budget']['unread_after_page']==15-len(out['items'])
    assert workspace.settings.agent_max_tool_calls==24


def test_query_cross_page_repeat_reuses_receipt(workspace,stored,monkeypatch):
    drawing,r=stored;s=new_session(workspace,drawing)
    monkeypatch.setattr(ComponentService,'build',lambda self,kind,args:deepcopy(r))
    args=dict(layout_id=r['layout_id'],bbox=[0,0,10,10],view='openings',offset=0,limit=1)
    first=json.loads(s.invoke_tool('query_building_relations',args));assert 'error' not in first,first
    second=json.loads(s.invoke_tool('query_building_relations',args|{'offset':first['next_offset']}));assert 'error' not in second,second
    observations=deepcopy(s.observations)
    third=json.loads(s.invoke_tool('query_building_relations',args))
    assert third.get('already_returned') is True,third
    assert third['observation_id']==first['observation_id']
    assert s.observations==observations


def test_existing_context_cap_preserves_partial_reading(workspace,stored):
    drawing,r=stored
    for item in r['openings']:item['source_evidence']['literal']='x'*20000
    workspace.store.put('component',r['record_id'],r)
    s=new_session(workspace,drawing);seen=[];offset=0
    while offset is not None:
        out=json.loads(s.invoke_tool('get_component_evidence',dict(record_id=r['record_id'],view='openings',offset=offset,limit=20)))
        if 'error' in out:
            assert out['error']['code']=='AGENT_POLICY_STOP'
            assert out['error']['stop_reason']==s.policy.stop_reason
            break
        seen.extend(i['object_id'] for i in out['items']);offset=out['next_offset']
        assert out['has_more'] and out['pagination']['coverage_scope']=='returned_page_only'
    assert s.policy.geometry_work and s.policy.stop_reason=='tool_context_budget'
    assert len(seen)<15 and offset is not None
    observed={i['object_id'] for o in s.observations.values() for i in o['data'].get('items',[]) if 'object_id' in i}
    assert observed==set(seen)


def test_delivered_owner_tag_does_not_reduce_active_owner_budget(workspace,stored):
    drawing,r=stored;s=new_session(workspace,drawing);s.task.auto_plan()
    active=s.task.data['subtasks'][0]
    delivered=deepcopy(active);delivered.update(id='delivered_owner',status='completed',delivered=True,calls=12)
    s.task.data['subtasks'].append(delivered)
    out=json.loads(s.invoke_tool('get_component_evidence',dict(record_id=r['record_id'],view='openings',limit=1,task_ids=[active['id'],delivered['id']])))
    assert 'error' not in out,out
    assert out['pagination_budget']['available_page_calls_including_current']==11
    assert s.trace[-1]['task_ids']==[active['id']]
    assert delivered['calls']==12
