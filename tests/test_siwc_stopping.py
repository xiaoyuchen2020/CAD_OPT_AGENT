"""Local scripted Responses: evidence recovery must not disable CAD tools."""
import json
from dataclasses import replace
import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from test_siwc import make_session, connected_lease, response
from test_agent import final


def answer(text):
    return response([{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}])


def tool(name, args, call_id):
    return response([{'type':'function_call','namespace':'cad','name':name,
                      'arguments':json.dumps(args),'call_id':call_id}])


def make_stopping_session(workspace,demo_bytes,monkeypatch):
    session=make_session(workspace,demo_bytes)
    connected_lease(workspace,monkeypatch)
    return session,workspace.regions(session.request.snapshot_id)[0].region_id


def test_plan_expansion_does_not_capture_preplan_round_limit(workspace,demo_bytes,monkeypatch):
    s,rid=make_stopping_session(workspace,demo_bytes,monkeypatch)
    # Legacy dynamic limits remain supported; adaptive_v1 has explicit ceilings.
    s.w.settings=replace(s.w.settings,subscription_budget_policy='legacy',
                         stable_subscription_prefix=False,agent_max_rounds=8)
    # Isolate dynamic round expansion from the separately tested input cap:
    # full spatial metadata plus the entire legacy tool catalog nearly fills it.
    monkeypatch.setattr('cad_agent.runtimes.responses_subscription.text_limit', lambda _: 150000)
    monkeypatch.setattr('cad_agent.runtimes.budget_policy.text_limit', lambda _: 150000)
    s.policy.enabled=False
    s.task.auto_plan()
    initial_limit=s.round_limit;calls=[]
    def request(self,session,payload):
        calls.append(payload)
        if len(calls)==1:
            # Mirrors capabilities added by the first model plan.
            session.task.data['subtasks'][0]['operations']=['arithmetic','geometry','vision']
        if len(calls)<=initial_limit+1:
            return tool('read_source_context',{'region_id':rid},f'c{len(calls)}')
        session.policy.stop_reason='partial_delivery'
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    assert s.run()['citations']
    assert len(calls)==initial_limit+2


def test_early_answer_recovers_tools_context_and_citations(workspace,demo_bytes,monkeypatch):
    s,rid=make_stopping_session(workspace,demo_bytes,monkeypatch);payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:
            assert payload['tool_choice']=='required'
            return answer('I will check the drawing.')  # Model ignores the required tool.
        if len(payloads)==2:
            assert payload['tool_choice']=='required' and payload['tools']
            assert any(m.get('role')=='developer' for m in payload['input'])
            assert not session.policy.stop_reason
            return tool('read_source_context',{'region_id':rid},'c1')
        assert payload['tool_choice']=='auto'
        assert any(m.get('type')=='function_call_output' and m['call_id']=='c1' for m in payload['input'])
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run()
    assert len(payloads)==3 and result['citations'] and not result['fallback_used']
    assert result['efficiency']['stop_reason']=='model_answered'
    assert result['model_requests'][0]['answer_validation']=={'category':'no_successful_tool_call','action':'retrieve_evidence'}


def test_search_observation_is_not_a_substitute_for_source_evidence(workspace,demo_bytes,monkeypatch):
    s,rid=make_stopping_session(workspace,demo_bytes,monkeypatch);payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:return tool('search_sources',{'query':'墙'},'search')
        if len(payloads)==2:
            assert session.observations and not session.evidence
            return answer(json.dumps({'claims':[{'text':'墙体采用某材料','state':'observed','evidence_ids':[]}]}))
        if len(payloads)==3:
            assert payload['tool_choice']=='required' and payload['tools']
            assert any(m.get('type')=='function_call_output' and m['call_id']=='search' for m in payload['input'])
            return tool('read_source_context',{'region_id':rid},'read')
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert len(payloads)==4 and result['citations']
    assert result['model_requests'][1]['answer_validation']['category']=='no_source_evidence'


def test_unread_reference_keeps_tools_even_with_existing_evidence(workspace,demo_bytes,monkeypatch):
    s,rid=make_stopping_session(workspace,demo_bytes,monkeypatch)
    s.invoke_tool('read_source_context',{'region_id':rid});payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:return answer(final('ev_not_read'))
        if len(payloads)==2:
            assert payload['tool_choice']=='required' and payload['tools']
            return tool('read_source_context',{'region_id':rid},'read')
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert result['citations'] and len(payloads)==3
    assert result['model_requests'][0]['answer_validation']['category']=='unread_source_reference'


def test_existing_evidence_format_error_gets_one_tool_free_repair(workspace,demo_bytes,monkeypatch):
    s,rid=make_stopping_session(workspace,demo_bytes,monkeypatch)
    s.invoke_tool('read_source_context',{'region_id':rid});payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:return answer('not JSON private-marker')
        assert payload['tool_choice']=='none' and payload['tools']==payloads[0]['tools']
        assert json.loads(payload['input'][0]['content'])['evidence']
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert len(payloads)==2 and result['citations']
    assert result['efficiency']['stop_reason']=='answer_repair'
    assert 'private-marker' not in json.dumps(result['model_requests'])


def test_repeated_no_tool_answers_stop_without_inventing_evidence(workspace,demo_bytes,monkeypatch):
    s,_=make_stopping_session(workspace,demo_bytes,monkeypatch);payloads=[]
    def request(self,session,payload):
        payloads.append(payload);assert payload['tool_choice']=='required'
        return answer('no tools private-marker')
    monkeypatch.setattr(ResponsesTransport,'request',request)
    with pytest.raises(ModelError) as caught:s.run()
    assert caught.value.code=='SIWC_EVIDENCE_NOT_OBTAINED' and len(payloads)==3
    assert not s.trace and not s.evidence
    assert s.runtime_diagnostics['stop_reason']=='evidence_recovery_limit'
    assert 'private-marker' not in json.dumps(s.runtime_diagnostics)


@pytest.mark.parametrize('budget',['tokens','calls','rounds','stalled'])
def test_budgets_do_not_reopen_tools_without_any_successful_read(workspace,demo_bytes,monkeypatch,budget):
    s,_=make_stopping_session(workspace,demo_bytes,monkeypatch)
    if budget=='tokens':s.usage.append({'total_tokens':850000})
    if budget=='calls':s.calls=s.w.settings.agent_max_tool_calls
    if budget=='rounds':monkeypatch.setattr(s.w,'settings',replace(s.w.settings,agent_max_rounds=1))
    if budget=='stalled':s.policy.stop_reason='no_new_information'
    monkeypatch.setattr(ResponsesTransport,'request',lambda *args:pytest.fail('No doomed final request'))
    with pytest.raises(ModelError) as caught:s.run()
    assert caught.value.code=='SIWC_EVIDENCE_NOT_OBTAINED'
    assert not s.runtime_diagnostics['model_requests']


def test_unknown_after_actual_empty_search_can_finish(workspace,demo_bytes,monkeypatch):
    s,_=make_stopping_session(workspace,demo_bytes,monkeypatch);payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:return tool('search_sources',{'query':'impossible-fixture-marker'},'search')
        assert payload['tool_choice']=='auto'
        return answer(json.dumps({'claims':[{'text':'本次检索未找到相关依据，无法确定。','state':'unknown','evidence_ids':[]}]}))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert len(payloads)==2 and not result['citations']
    assert result['claims'][0]['state']=='unknown'


def test_budget_after_search_allows_unknown_but_no_more_tools(workspace,demo_bytes,monkeypatch):
    s,_=make_stopping_session(workspace,demo_bytes,monkeypatch);payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:
            session.usage.append({'total_tokens':850000})
            return tool('search_sources',{'query':'impossible-fixture-marker'},'search')
        assert payload['tool_choice']=='none' and payload['tools']==payloads[0]['tools']
        return answer(json.dumps({'claims':[{'text':'已读取信息不足，无法确定。','state':'unknown','evidence_ids':[]}]}))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert len(payloads)==2 and not result['citations']
    assert result['efficiency']['stop_reason']=='finalization_token_reserve'


def test_tool_observation_answer_needs_no_unrelated_source_search(workspace,demo_bytes,monkeypatch):
    s,_=make_stopping_session(workspace,demo_bytes,monkeypatch);payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        if len(payloads)==1:return tool('search_sources',{'query':'impossible-fixture-marker'},'search')
        return answer(json.dumps({'claims':[{'text':'此次检索未返回候选。','state':'observed',
            'evidence_basis':'tool_observation','observation_ids':[next(iter(session.observations))],'evidence_ids':[]}]}))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert len(payloads)==2 and result['tool_observations'] and not result['citations']


def test_time_reserve_stays_tool_free_with_evidence(workspace,demo_bytes,monkeypatch):
    s,rid=make_stopping_session(workspace,demo_bytes,monkeypatch)
    s.invoke_tool('read_source_context',{'region_id':rid})
    monkeypatch.setattr(s,'_remaining',lambda:40);payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        assert payload['tool_choice']=='none' and payload['tools']
        return answer(final(next(iter(s.evidence))))
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert len(payloads)==1 and result['citations']
    assert result['efficiency']['stop_reason']=='finalization_time_reserve'
