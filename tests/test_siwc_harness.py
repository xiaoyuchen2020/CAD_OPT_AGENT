import json
import os

import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.runtimes.bridge import ModelBridge
from cad_agent.analysis.session import tool_definitions
from test_siwc import make_session, connected_lease, response
from test_agent import final


def setup_session(workspace, demo_bytes, monkeypatch, runtime='deepseek_harness'):
    s = make_session(workspace, demo_bytes, allow_fallback=False)
    s.request.runtime = runtime
    connected_lease(workspace, monkeypatch)
    monkeypatch.setattr(workspace.gateway, 'chat_turn', lambda *a, **k: pytest.fail('Paid gateway must not run'))
    return s


@pytest.mark.parametrize('runtime', ['codex', 'deepseek_harness'])
def test_subscription_bridge_preserves_reasoning_pairing_and_usage(workspace, demo_bytes, monkeypatch, runtime):
    s = setup_session(workspace, demo_bytes, monkeypatch, runtime)
    rid = workspace.regions(s.request.snapshot_id)[0].region_id
    reasoning = {'type':'reasoning', 'id':'r1', 'encrypted_content':'private-reasoning', 'summary':[]}
    call = {'type':'function_call', 'namespace':'cad', 'name':'read_source_context',
        'call_id':'c1', 'arguments':json.dumps({'region_id':rid})}
    payloads=[]
    def request(self, session, payload):
        payloads.append(payload)
        assert payload['reasoning']=={'effort':'low'} and payload['store'] is False
        assert payload['tools'][0]['name']=='cad'
        if len(payloads)==1:
            assert payload['tool_choice']=='required'
            r=response([reasoning,call])
        else:
            assert payload['tool_choice']=='auto' and reasoning in payload['input'] and call in payload['input']
            assert any(i.get('type')=='function_call_output' and i['call_id']=='c1' for i in payload['input'])
            r=response([{'type':'message','content':[{'type':'output_text','text':final(next(iter(s.evidence)))}]}])
        self.record_usage(session,r);return r
    monkeypatch.setattr(ResponsesTransport,'request',request)
    with ModelBridge(s) as b:
        msg,_=b.complete({'model':s.w.settings.model,'messages':[{'role':'user','content':'question'}],'tools':tool_definitions()})
        value=s.invoke_tool('read_source_context',{'region_id':rid})
        answer,_=b.complete({'model':s.w.settings.model,'messages':[{'role':'user','content':'question'},msg,
            {'role':'tool','tool_call_id':'c1','content':value}],'tools':tool_definitions()})
        assert s.finish(answer['content'])['citations']
    assert sum(u['total_tokens'] for u in s.usage)==240
    assert 'private-reasoning' not in json.dumps(s.runtime_diagnostics)


@pytest.mark.parametrize('runtime', ['codex', 'deepseek_harness'])
def test_subscription_external_never_falls_back(workspace,demo_bytes,monkeypatch,runtime):
    s=setup_session(workspace,demo_bytes,monkeypatch,runtime);s.request.allow_fallback=True
    with pytest.raises(ModelError) as e:s.run()
    assert e.value.code=='SUBSCRIPTION_FALLBACK_DISABLED'


def test_failed_bridge_cannot_make_another_upstream_request(workspace,demo_bytes,monkeypatch):
    s=setup_session(workspace,demo_bytes,monkeypatch)
    with ModelBridge(s) as b:
        b.error=ModelError('SIWC_HTTP_ERROR','fixture')
        monkeypatch.setattr(ResponsesTransport,'request',lambda *a:pytest.fail('Must not retry'))
        with pytest.raises(ModelError):b.complete({'model':s.w.settings.model,'messages':[],'tools':tool_definitions()})


@pytest.mark.parametrize('runtime', ['deepseek_harness', 'codex'])
@pytest.mark.skipif(os.environ.get('CAD_TEST_EXTERNAL_HARNESSES')!='1',reason='Opt-in real CLI with mocked model, no inference')
def test_real_harness_process_with_mock_subscription(workspace,demo_bytes,monkeypatch,runtime):
    s=setup_session(workspace,demo_bytes,monkeypatch,runtime)
    rid=workspace.regions(s.request.snapshot_id)[0].region_id;payloads=[]
    def request(self,session,payload):
        payloads.append(payload)
        assert len(payloads)<=2
        if len(payloads)==1:
            assert {t['name'] for t in payload['tools'][0]['tools']}=={t['function']['name'] for t in tool_definitions(s.policy.enabled)}
            r=response([{'type':'function_call','namespace':'cad','name':'read_source_context',
                'arguments':json.dumps({'region_id':rid}),'call_id':'c1'}])
        else:
            assert any(i.get('type')=='function_call_output' and i['call_id']=='c1' for i in payload['input'])
            r=response([{'type':'message','content':[{'type':'output_text','text':final(next(iter(s.evidence)))}]}])
        self.record_usage(session,r);return r
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run()
    assert len(payloads)==2 and len(s.trace)==1 and result['citations']
    assert result['transport']=='harness_to_siwc_responses' and result['billing']=='siwc_subscription'
    assert result['runtime_used']==runtime and not result['fallback_used']
    assert sum(u['total_tokens'] for u in s.usage)==240


def test_subscription_bridge_rejects_unknown_history_and_code_calls(workspace,demo_bytes,monkeypatch):
    s=setup_session(workspace,demo_bytes,monkeypatch)
    with ModelBridge(s) as b:
        with pytest.raises(ModelError,match='未知'):
            b.subscription.inputs([{'role':'tool','tool_call_id':'foreign','content':'{}'}])
        def request(self,session,payload):
            r=response([{'type':'function_call','namespace':'shell','name':'exec','call_id':'c','arguments':'{}'}])
            self.record_usage(session,r);return r
        monkeypatch.setattr(ResponsesTransport,'request',request)
        with pytest.raises(ModelError,match='未授权'):
            b.complete({'model':s.w.settings.model,'messages':[],'tools':tool_definitions()})
    assert not s.trace


def test_subscription_bridge_reserves_one_final_request(workspace,demo_bytes,monkeypatch):
    s=setup_session(workspace,demo_bytes,monkeypatch)
    s.invoke_tool('read_source_context',{'region_id':workspace.regions(s.request.snapshot_id)[0].region_id})
    s.usage.append({'total_tokens':40000})
    def request(self,session,payload):
        assert payload['tools']==[] and payload['tool_choice']=='none'
        assert json.loads(payload['input'][0]['content'])['evidence']
        r=response([{'type':'message','content':[{'type':'output_text','text':final(next(iter(s.evidence)))}]}])
        self.record_usage(session,r);return r
    monkeypatch.setattr(ResponsesTransport,'request',request)
    with ModelBridge(s) as b:
        payload={'model':s.w.settings.model,'messages':[],'tools':tool_definitions()}
        message,_=b.complete(payload)
        assert s.finish(message['content'])['citations']
        with pytest.raises(ModelError,match='收尾'):b.complete(payload)
        assert len(b.requests)==1


def test_codex_additional_tools_filtered_using_trusted_registration(workspace,demo_bytes,monkeypatch):
    s=setup_session(workspace,demo_bytes,monkeypatch,'codex')
    def request(self,session,payload):
        names={t['name'] for t in payload['tools'][0]['tools']}
        assert 'exec' not in names and 'read_source_context' in names
        r=response([]);self.record_usage(session,r);return r
    monkeypatch.setattr(ResponsesTransport,'request',request)
    with ModelBridge(s, registered_tools=tool_definitions()) as b:
        b.complete({'model':s.w.settings.model,'input':[{'type':'additional_tools','role':'developer',
            'tools':[{'type':'namespace','name':'functions','tools':[{'type':'custom','name':'exec'}]}]}]}, responses=True)
