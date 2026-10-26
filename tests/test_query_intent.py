from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pytest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.query_intent import QueryIntent, register, prepare_call
from cad_agent.analysis.source_candidates import title_match, search, SearchSourceRecords, scope_assessment, claim_issues
from cad_agent.contracts import ChatRequest


def intent(question='帮我找下地下室内墙4做法', scope=True):
    return dict(action='lookup', targets=[dict(target_id='wall4', requirement_id='q1',
        subject=dict(value='内墙', question_quote='内墙4'),
        identifier=dict(value='4', question_quote='内墙4'),
        conditions=[dict(kind='scope', operator='include', value='地下室', question_quote='地下室')] if scope else [],
        requested_content=['做法原文'])], unresolved_requirements=[])


@pytest.fixture
def session(workspace, tmp_path):
    from conftest import dxf_bytes
    def build(doc):
        for x, title in [(0,'内墙4'), (120,'内墙40'), (240,'外墙4'), (360,'内墙5')]:
            doc.modelspace().add_mtext(title+'\\P1. 5厚砂浆。\\P2. 基层墙体。',
                dxfattribs={'insert': (x, 100), 'char_height': 3, 'width': 80})
    drawing = workspace.ingest('intent.dxf', dxf_bytes(tmp_path, build))
    workspace.settings = replace(workspace.settings, llm_query_intent=True, agent_max_tool_calls=24)
    return AgentRunner(workspace, ChatRequest(snapshot_id=drawing['id'], question='帮我找下地下室内墙4做法', mode='agent'))


def call(session, tool, args):
    return json.loads(session.invoke_tool(tool, args))


def test_first_call_requires_question_contract_before_any_read(session):
    r=call(session, 'read_source_context', {'region_id':session.index.regions[0].region_id})
    assert r['error']['code']=='QUERY_INTENT_REQUIRED'
    assert not session.evidence and not session.observations


def test_precise_discovery_does_not_read_or_expand_basement_families(session):
    result=call(session, 'search_source_records', dict(target_id='wall4', query_intent=intent()))
    assert 'error' not in result, result
    assert result['total']==1 and result['items'][0]['title']=='内墙4'
    assert result['items'][0]['scope_state']=='unresolved'
    assert not session.evidence
    assert not any(o['tool']=='read_assembly' for o in session.observations.values())
    saved=session.w.store.get('agent_task',session.task.data['task_id'])
    assert saved['query_intent']['intent']['targets'][0]['conditions'][0]['value']=='地下室'
    assert len(saved['source_candidates'])==1


@pytest.mark.parametrize('title,expected', [('内墙4',True),('内墙 ４',True),('地下室内墙4',True),
    ('内墙40',False),('内墙14',False),('外墙4',False),('内墙4A',False),('内墙面4',True),('5厚砂浆',False)])
def test_identifier_boundaries(title,expected):
    assert title_match(title,'内墙','4') is expected


def test_query_validation_rejects_invented_binding_and_preserves_state(session):
    bad=intent();bad['targets'][0]['conditions'][0]['question_quote']='地上'
    result=call(session,'search_source_records',dict(target_id='wall4',query_intent=bad))
    assert result['error']['code']=='QUERY_INTENT_QUOTE'
    assert 'query_intent' not in session.task.data
    assert not session.evidence


def test_contract_cannot_be_relaxed_by_replacing_goal(session):
    register(session,intent())
    with pytest.raises(ValueError,match='QUERY_INTENT_CHANGED'):
        register(session,intent(scope=False))


def test_expansion_requires_reason_and_does_not_change_goal(session):
    record=register(session,intent())
    before=deepcopy(record)
    with pytest.raises(ValueError,match='QUERY_EXPANSION_REASON'):
        search(session,SearchSourceRecords(target_id='wall4',alternatives=['外墙']))
    result=search(session,SearchSourceRecords(target_id='wall4',alternatives=['外墙'],expansion_reason='核对邻近编号候选，保留范围未知'))
    assert {r['match_kind'] for r in result['items']}=={'exact','alternative'}
    assert session.task.data['query_intent']==before


def test_exclusion_and_unknown_scope_are_not_conflated(session):
    t=QueryIntent.model_validate(intent()).targets[0].model_dump()
    region=session.index.regions[0].model_copy(update={'frame_name':'地下室建筑做法表','context_region_ids':[]})
    assert scope_assessment(session.index,region,'内墙4',t)[0]=='supported'
    t['conditions'][0]['operator']='exclude'
    assert scope_assessment(session.index,region,'内墙4',t)[0]=='conflicting'
    region=region.model_copy(update={'frame_name':'建筑做法表'})
    assert scope_assessment(session.index,region,'内墙4',t)[0]=='unresolved'


def test_negative_quote_cannot_be_registered_as_include(session):
    session.request=session.request.model_copy(update={'question':'找内墙4，不要地下室的'})
    bad=intent();bad['targets'][0]['conditions'][0]['question_quote']='不要地下室的'
    with pytest.raises(ValueError,match='QUERY_INTENT_NEGATION'):register(session,bad)


def test_parent_scope_uses_user_text_not_assistant_text(session):
    session.w.store.put('chat_run','parent',dict(request=dict(snapshot_id=session.request.snapshot_id,
        question='帮我找下地下室内墙4做法',related_drawings=[]),result={'answer':'地上内墙'}))
    session.request=session.request.model_copy(update={'question':'那内墙5呢','parent_run_id':'parent'})
    value=intent();target=value['targets'][0]
    target['subject']['question_quote']='内墙5'
    target['identifier'].update(value='5',question_quote='内墙5')
    target['conditions'][0]['source']='parent'
    assert register(session,value)['bindings'][-1]['source']=='parent'
    value['targets'][0]['conditions'][0].update(value='地上',question_quote='地上内墙')
    with pytest.raises(ValueError,match='QUERY_INTENT_QUOTE'):register(session,value)


def test_discovery_pagination_keeps_distinct_same_number_sources(session):
    register(session,intent())
    original=next(r for r in session.index.regions if '内墙4\n' in r.text or r.text=='内墙4')
    duplicate=original.model_copy(update={'region_id':'another_region','frame_id':'other_frame'})
    session.index.regions.append(duplicate)
    first=search(session,SearchSourceRecords(target_id='wall4',limit=1))
    second=search(session,SearchSourceRecords(target_id='wall4',limit=1,offset=1))
    assert first['total']==2 and first['has_more'] and not second['has_more']
    assert first['items'][0]['candidate_id']!=second['items'][0]['candidate_id']


def test_candidate_ids_and_intent_survive_resume(session):
    found=call(session,'search_source_records',dict(target_id='wall4',query_intent=intent()))
    session.task.data['status']='partial';session.task.save()
    resumed=AgentRunner(session.w,session.request.model_copy(update={'resume_task_id':session.task.data['task_id']}))
    assert resumed.task.data['query_intent']==session.task.data['query_intent']
    assert found['items'][0]['candidate_id'] in resumed.task.data['source_candidates']
    assert not resumed.evidence


def test_query_metadata_exposed_only_on_first_call_tools(session):
    from cad_agent.analysis.tool_registry import tool_definitions
    specs={t['function']['name']:t['function']['parameters'] for t in tool_definitions(True,session)}
    assert 'query_intent' in specs['search_source_records']['properties']
    assert 'query_intent' in specs['plan_task']['properties']
    assert 'query_intent' not in specs['read_assembly']['properties']
    assert 'QueryTarget' in specs['search_source_records']['$defs']
    assert 'batch_read_sources' not in specs


def test_build_messages_does_not_auto_execute_room_or_source_workflows(session,monkeypatch):
    session.request=session.request.model_copy(update={'question':'列出一层所有卧室面积'})
    from cad_agent.geometry.room_batch import RoomBatchService
    monkeypatch.setattr(RoomBatchService,'infer_request',lambda *a:pytest.fail('inference must not execute before model'))
    session.build_messages()
    assert not session.trace and not session.evidence


def test_model_is_called_before_source_discovery(session,monkeypatch):
    from cad_agent.analysis import source_record_workflow
    monkeypatch.setattr(source_record_workflow,'try_run',lambda *a:pytest.fail('legacy source route executed'))
    from cad_agent.tables import workflow as table_workflow
    monkeypatch.setattr(table_workflow,'try_run',lambda *a:pytest.fail('table route executed before model'))
    seen=[]
    def turn(messages,tools,timeout):
        seen.append(messages)
        if len(seen)==1:
            assert not session.trace and not session.evidence
            return dict(role='assistant',content=None,tool_calls=[dict(id='c1',type='function',
                function=dict(name='search_source_records',arguments=json.dumps(dict(target_id='wall4',query_intent=intent()))))]),{}
        return dict(role='assistant',content=json.dumps({'claims':[{'text':'已找到内墙4标题，正文及地下室范围尚未核实。',
            'state':'unknown','question_part_ids':['q1']}]})),{}
    monkeypatch.setattr(session.w.gateway,'chat_turn',turn)
    result=session.run()
    assert len(seen)==2 and [t['tool'] for t in session.trace]==['search_source_records']
    assert result['claims'][0]['state']=='unknown'


def test_claim_requires_matching_number_title(session):
    from cad_agent.analysis.tool_registry import Claim
    register(session,intent())
    session.evidence={'e1':{'quote':'内墙40'},'e2':{'quote':'内墙4'}}
    bad=Claim(text='内墙4做法',state='observed',question_part_ids=['q1'],evidence_ids=['e1'])
    assert claim_issues(session,bad)
    session.evidence['e2'].update(snapshot_id='s',frame_id='f')
    session.evidence['scope']={'quote':'地下室','snapshot_id':'s','frame_id':'f'}
    assert not claim_issues(session,bad.model_copy(update={'evidence_ids':['e2','scope'],'evidence_roles':{'scope':['scope']}}))


def test_default_enabled_with_explicit_legacy_switch(tmp_path, monkeypatch):
    from cad_agent.config import Settings
    assert Settings(data_dir=tmp_path).llm_query_intent
    monkeypatch.setenv('CAD_LLM_QUERY_INTENT', '0')
    assert not Settings.load().llm_query_intent


def test_source_change_rejects_previously_discovered_candidate(session):
    found=call(session,'search_source_records',dict(target_id='wall4',query_intent=intent()))
    p=session.w.store.artifact(session.request.snapshot_id,'regions.json')
    p.write_text(p.read_text()+'\n')
    action=found['items'][0]['next_action']
    result=call(session,action['tool'],action['arguments'])
    assert result['error']['code']=='QUERY_CANDIDATE_STALE'
    assert not session.evidence


def test_read_records_page_zero_without_implicit_prefetch(session):
    found=call(session,'search_source_records',dict(target_id='wall4',query_intent=intent()))
    action=found['items'][0]['next_action']
    result=call(session,action['tool'],action['arguments']|{'result_view':'full'})
    assert 'error' not in result, result
    c=session.task.data['source_candidates'][found['items'][0]['candidate_id']]
    assert c['body_read'] and set(c['read_pages'])=={'0'}
    assert len(session.task.data['query_context_observation_ids'])==1


def test_cross_snapshot_discovery_is_denied(session):
    register(session,intent())
    with pytest.raises(ValueError,match='QUERY_SNAPSHOT_DENIED'):
        search(session,SearchSourceRecords(target_id='wall4',snapshot_id='unselected'))


def test_scope_role_from_other_frame_cannot_confirm_numbered_target(session):
    from cad_agent.analysis.tool_registry import Claim
    register(session,intent())
    session.evidence={'title':dict(quote='内墙4',snapshot_id='s',frame_id='a'),
                      'scope':dict(quote='地下室',snapshot_id='s',frame_id='b')}
    claim=Claim(text='地下室内墙4',state='observed',question_part_ids=['q1'],
        evidence_ids=['title','scope'],evidence_roles={'scope':['scope']})
    assert any('QUERY_SCOPE_EVIDENCE' in e for e in claim_issues(session,claim))


def test_negated_scope_label_stays_unresolved(session):
    target=QueryIntent.model_validate(intent()).targets[0].model_dump()
    region=session.index.regions[0].model_copy(update={'frame_name':'非地下室','context_region_ids':[]})
    assert scope_assessment(session.index,region,'内墙4',target)[0]=='unresolved'
    assert not title_match('参见内墙4','内墙','4')


def test_partial_read_cannot_be_reported_fulfilled(session):
    from cad_agent.analysis.source_candidates import delivery_issues, pages_complete
    register(session,intent())
    session.task.data['source_candidates']={'c':dict(target_id='wall4',body_read=True,
        snapshot_id='s',region_id='r',read_pages={'0':dict(returned=1,total=2,has_more=True,next_offset=1)})}
    parsed=NS(requirement_outcomes=[NS(requirement_id='q1',status='fulfilled')],claims=[])
    assert any('QUERY_PAGES_INCOMPLETE' in e for e in delivery_issues(session,parsed))
    assert not pages_complete({'1':dict(returned=1,total=2,has_more=False,next_offset=None)})


def test_multiple_targets_require_individual_delivery(session):
    from cad_agent.analysis.source_candidates import delivery_issues
    value=intent();value['targets'].append(deepcopy(value['targets'][0]));value['targets'][1]['target_id']='other'
    register(session,value)
    parsed=NS(requirement_outcomes=[],claims=[NS(question_part_ids=['q1'],query_target_ids=['wall4'])])
    assert any('QUERY_TARGET_COVERAGE' in e for e in delivery_issues(session,parsed))


def test_final_and_checkpoint_keep_original_contract(session):
    register(session,intent())
    from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
    assert session.final_context(CodexSubscriptionRuntime(), "test_budget")['query_intent']==session.task.data['query_intent']
    assert session.focus.context()['query_intent']==session.task.data['query_intent']


def test_scope_recovery_does_not_execute_implicit_batch_in_new_mode(session, monkeypatch):
    from cad_agent.geometry.room_batch import RoomBatchService
    session.room_batches={'old':dict(scope=None)}
    monkeypatch.setattr(RoomBatchService,'run',lambda *a:pytest.fail('implicit execution'))
    assert not session.recover_scope_before_delivery()


def test_excluded_scope_cannot_be_delivered_as_satisfying_request(session):
    from cad_agent.analysis.tool_registry import Claim
    session.request=session.request.model_copy(update={'question':'找内墙4，不要地下室的'})
    value=intent();value['targets'][0]['conditions'][0].update(operator='exclude',question_quote='不要地下室的')
    register(session,value)
    session.evidence={'title':dict(quote='内墙4',snapshot_id='s',frame_id='a'),
                      'scope':dict(quote='地下室',snapshot_id='s',frame_id='a')}
    claim=Claim(text='内墙4做法',state='observed',question_part_ids=['q1'],
        evidence_ids=['title','scope'],evidence_roles={'scope':['scope']})
    assert any('QUERY_SCOPE_EXCLUDED' in e for e in claim_issues(session,claim))
    assert not claim_issues(session,claim.model_copy(update={'state':'conflict'}))


def test_unnumbered_target_still_requires_scope_provenance(session):
    from cad_agent.analysis.tool_registry import Claim
    value=intent();value['targets'][0].pop('identifier');register(session,value)
    session.evidence={'e':dict(quote='内墙',snapshot_id='s',frame_id='f')}
    c=Claim(text='内墙原文',state='observed',question_part_ids=['q1'],evidence_ids=['e'])
    assert any('QUERY_SCOPE_EVIDENCE' in e for e in claim_issues(session,c))


def test_content_exclusion_requires_full_record_and_rejects_conflict(session):
    from cad_agent.analysis.source_candidates import delivery_issues
    from cad_agent.analysis.tool_registry import Claim
    session.request=session.request.model_copy(update={'question':'找内墙4做法，不含保温'})
    value=intent(scope=False);value['targets'][0]['conditions']=[dict(kind='content',operator='exclude',value='保温',question_quote='不含保温')]
    register(session,value)
    session.evidence={'title':dict(quote='内墙4',snapshot_id='s',region_id='r')}
    claim=Claim(text='内墙4',state='observed',question_part_ids=['q1'],evidence_ids=['title'])
    parsed=NS(claims=[claim],requirement_outcomes=[NS(requirement_id='q1',status='fulfilled')])
    assert any('QUERY_EXCLUSION_UNVERIFIED' in e for e in delivery_issues(session,parsed))
    session.task.data['source_candidates']={'c':dict(target_id='wall4',snapshot_id='s',region_id='r',body_read=True,
        read_pages={'0':dict(observation_id='o',returned=1,total=1,has_more=False,next_offset=None)})}
    session.observations={'o':dict(data=dict(items=[dict(evidence=dict(quote='25厚保温砂浆'))]))}
    assert any('QUERY_EXCLUSION_CONFLICT' in e for e in delivery_issues(session,parsed))


@pytest.mark.parametrize('subject,identifier', [('内墙4','4'),('内墙4','内墙4'),('内墙','内墙4'),('内墙','4')])
def test_model_can_bind_complete_subject_and_separate_number(subject,identifier):
    assert title_match('内墙4',subject,identifier)
    assert not title_match('内墙40',subject,identifier)
    assert not title_match('内墙14',subject,identifier)
    assert not title_match('内墙4A',subject,identifier)


def test_composite_subject_discovery_retains_explicit_exclusion(session):
    session.request=session.request.model_copy(update={'question':'帮我找下内墙4做法，不要地下室的。'})
    value=intent();target=value['targets'][0]
    target['subject']['value']='内墙4'
    target['conditions'][0].update(operator='exclude',question_quote='不要地下室的')
    result=call(session,'search_source_records',dict(target_id='wall4',query_intent=value))
    assert result['total']==1
    assert result['items'][0]['scope_state']=='unresolved'
    assert session.task.data['query_intent']['intent']['targets'][0]['conditions'][0]['operator']=='exclude'
