from copy import deepcopy
import json
from types import SimpleNamespace as NS
import pytest
from cad_agent.analysis.final_context_budget import fit_messages
from cad_agent.analysis.efficiency import compact,dumps
from cad_agent.analysis.model_context import encode_messages
from cad_agent.adapters.model import ModelError
from test_siwc import make_session


def large_packet(s):
    for i in range(35):
        eid='ev_'+f'{i:024x}'
        s.evidence[eid]=dict(evidence_id=eid,snapshot_id=s.request.snapshot_id,region_id='region_'+f'{i:024x}',
            quote='厚度5mm，保留材料及适用范围。'*8,text_span=[0,136],numeric_readings={'version':'source-numbers-v1','items':[
                dict(number_id='num_'+f'{i*12+j:020x}',kind='quantity',value='5',unit='mm',status='confirmed',source_span=[0,1]) for j in range(12)]})
    return dict(question=s.request.question,task={},instruction='按原文回答，未包含的事实保留缺口。',
                evidence=compact(list(s.evidence.values())),observations=[])


def test_fit_uses_encoded_size_keeps_whole_quotes_numbers_and_diagnostics(workspace,demo_bytes):
    s=make_session(workspace,demo_bytes);s.runtime_diagnostics={}
    packet=large_packet(s);original=deepcopy(packet);registry=deepcopy(s.evidence)
    limit=12000
    assert sum(len(m.get('content','')) for m in encode_messages(s,[{'role':'user','content':dumps(packet)}]))>limit
    messages=fit_messages(s,packet,limit)
    assert sum(len(m.get('content','')) for m in messages)<=limit
    assert packet==original and s.evidence==registry
    encoded=json.loads(messages[0]['content'])
    assert encoded['context_omissions']['evidence_count']>0
    groups=encoded['source_groups'];assert groups
    for g in groups:
        for line in g['text'].splitlines():
            assert json.loads(line.split('] ',1)[1]) in [e['quote'] for e in registry.values()]
        for catalog in g.get('numbers',{}).values():
            assert all(n['unit']=='mm' and n['number_id'] for n in catalog['items'])
    assert s.runtime_diagnostics['final_context_fit'][-1]['status']=='fits'
    assert len(s.runtime_diagnostics['final_context_fit'][-1]['attempts'])>=2


def test_impossible_immutable_question_preserves_diagnostic_without_silent_truncation(workspace,demo_bytes):
    s=make_session(workspace,demo_bytes);s.runtime_diagnostics={}
    packet=dict(question='必须保留的问题'*3000,evidence=[],observations=[])
    original=deepcopy(packet)
    with pytest.raises(ModelError,match='必要问题'):
        fit_messages(s,packet,1000)
    assert packet==original
    assert s.runtime_diagnostics['final_context_fit'][-1]['status']=='minimal_packet_exceeds_limit'


def test_runtime_sends_fitted_final_once_without_paid_fallback(workspace,demo_bytes,monkeypatch):
    from dataclasses import replace
    from cad_agent.runtimes import responses_subscription as runtime
    from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
    from cad_agent.runtimes.budget_policy import STANDARD
    from test_siwc import connected_lease
    from test_siwc_stopping import answer
    s=make_session(workspace,demo_bytes,allow_fallback=False);connected_lease(workspace,monkeypatch)
    s.invoke_tool('read_source_context',{'region_id':s.index.regions[0].region_id})
    packet=large_packet(s)
    monkeypatch.setattr(s,'final_context',lambda *args:packet)
    monkeypatch.setattr(CodexSubscriptionRuntime,'stop_reason',lambda *args:'tool_call_budget')
    monkeypatch.setattr(runtime,'for_session',lambda *args:replace(STANDARD,final_context_chars=12000))
    sent=[]
    def request(self,session,payload):
        sent.append(payload)
        assert sum(len(m.get('content','')) for m in payload['input'])<=12000
        assert payload['tool_choice']=='none'
        return answer(json.dumps({'claims':[{'text':'本次尚未取得完整资料。','state':'unknown','question_part_ids':['q1']}]}))
    monkeypatch.setattr(runtime.ResponsesTransport,'request',request)
    result=runtime.ResponsesSubscriptionRuntime().run(s)
    assert len(sent)==1 and result['claims']
    assert s.request.allow_fallback is False
