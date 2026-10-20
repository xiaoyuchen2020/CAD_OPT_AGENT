import json
import time
import os
from contextlib import contextmanager
from urllib.parse import urlsplit, parse_qs
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cad_agent.adapters.siwc import SiwcAccount, ISSUER, TOKEN, RESOURCE, SCOPES
from cad_agent.adapters.model import ModelError
from cad_agent.config import Settings
from cad_agent.app import create_app
from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.responses_subscription import ResponsesTransport, ResponsesSubscriptionRuntime
from test_agent import final


@pytest.fixture
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "fixture"}
    return key, jwk


def signed(signing_key, **overrides):
    claims = {"iss": ISSUER, "aud": "oaiapp_fixture", "exp": int(time.time())+600,
              "sub": "fixture-subject", "nonce": "fixture-nonce"} | overrides
    return jwt.encode(claims, signing_key[0], algorithm="RS256", headers={"kid": "fixture"})


def mock_account(account, monkeypatch, signing_key, token=None):
    def handle(request):
        if request.url.path.endswith('jwks.json'):
            return httpx.Response(200, json={"keys": [signing_key[1]]})
        assert str(request.url) == TOKEN
        return httpx.Response(200, json=token or {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "id_token": signed(signing_key), "token_type": "Bearer", "expires_in": 3600, "scope": SCOPES})
    monkeypatch.setattr(account, "client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))


def pending(account):
    account.pending = {"state": "fixture-state", "nonce": "fixture-nonce", "verifier": "fixture-verifier",
        "redirect_uri": "http://127.0.0.1:1234/auth/callback", "expires": time.time()+600,
        "client_id": None, "subject": None, "account_id": None}


def test_new_authorization_has_pkce_and_stable_host(tmp_path):
    account = SiwcAccount(tmp_path)
    try:
        one = parse_qs(urlsplit(account.login()["auth_url"]).query)
        two = parse_qs(urlsplit(account.login()["auth_url"]).query)
        assert one['client_id'] == ['dynamic_agent_client']
        assert one['agent_name_hint'] == ['CAD Atlas']
        assert one['ext_agent_host_id'] == two['ext_agent_host_id']
        assert one['state'] != two['state'] and one['nonce'] != two['nonce']
        assert one['code_challenge'] != two['code_challenge']
        assert urlsplit(one['redirect_uri'][0]).hostname == '127.0.0.1'
        assert account.pending['verifier'] not in two['code_challenge']
    finally: account.close()


def test_oauth_state_consumed_once_and_credentials_private(tmp_path, monkeypatch, signing_key):
    a = SiwcAccount(tmp_path); pending(a); mock_account(a, monkeypatch, signing_key)
    args = {"state": "fixture-state", "code": "fixture-code", "client_id": "oaiapp_fixture"}
    with pytest.raises(ModelError): a.callback(args | {"state": "wrong"})
    assert a.pending
    a.callback(args)
    state = a.status(); assert state['connected'] and not state['login_pending']
    assert 'fixture-access' not in json.dumps(state) and 'fixture-subject' not in json.dumps(state)
    assert os.stat(a.home/'accounts.json').st_mode & 0o777 == 0o600
    assert os.stat(a.home).st_mode & 0o777 == 0o700
    with pytest.raises(ModelError): a.callback(args)
    assert SiwcAccount(tmp_path).status()['connected']


@pytest.mark.parametrize('overrides', [{'nonce':'wrong'}, {'aud':'other'}, {'iss':'https://attacker.test'},
                                      {'exp':int(time.time())-100}, {'sub':''}])
def test_rejects_invalid_identity(tmp_path, monkeypatch, signing_key, overrides):
    a = SiwcAccount(tmp_path); pending(a)
    token = {"access_token":"fixture-access", "id_token":signed(signing_key, **overrides),
             "token_type":"Bearer", "expires_in":3600, "scope":SCOPES}
    mock_account(a, monkeypatch, signing_key, token)
    with pytest.raises(ModelError): a.callback({'state':'fixture-state','code':'fixture-code','client_id':'oaiapp_fixture'})
    assert not a.status()['connected'] and a.pending is None


def test_missing_subscription_scope_never_enables_inference(tmp_path, monkeypatch, signing_key):
    a = SiwcAccount(tmp_path); pending(a)
    mock_account(a, monkeypatch, signing_key, {"access_token":"fixture-access", "id_token":signed(signing_key),
        "token_type":"Bearer", "expires_in":3600, "scope":"openid profile email"})
    with pytest.raises(ModelError): a.callback({'state':'fixture-state','code':'fixture-code','client_id':'oaiapp_fixture'})
    with pytest.raises(ModelError, match='授权'): a.access_token()


def test_reauthorization_rejects_different_client_and_subject(tmp_path, monkeypatch, signing_key):
    a = SiwcAccount(tmp_path); pending(a); a.pending['client_id']='oaiapp_fixture'
    mock_account(a, monkeypatch, signing_key)
    with pytest.raises(ModelError): a.callback({'state':'fixture-state','code':'x','client_id':'other'})
    pending(a); a.pending.update(client_id='oaiapp_fixture',subject='different')
    with pytest.raises(ModelError): a.callback({'state':'fixture-state','code':'x'})
    assert not a.status()['connected']


def test_refresh_rotates_atomically_without_scope_or_dynamic_id(tmp_path, monkeypatch, signing_key):
    a = SiwcAccount(tmp_path); pending(a); mock_account(a, monkeypatch, signing_key)
    a.callback({'state':'fixture-state','code':'x','client_id':'oaiapp_fixture'})
    with a.storage():
        data=a.load(); data['accounts'][data['active']]['expires_at']=0; a.save(data)
    count=[]
    def handle(req):
        fields=parse_qs(req.content.decode());count.append(fields)
        assert fields['client_id']==['oaiapp_fixture'] and 'scope' not in fields
        return httpx.Response(200,json={'access_token':'new-access','refresh_token':'new-refresh','token_type':'Bearer','expires_in':3600})
    monkeypatch.setattr(a,'client',lambda:httpx.Client(transport=httpx.MockTransport(handle)))
    assert a.access_token()=='new-access' and a.access_token()=='new-access' and len(count)==1
    with a.storage(): assert a.load()['accounts'][data['active']]['refresh_token']=='new-refresh'


def make_session(workspace, demo_bytes, **kwargs):
    d=workspace.ingest('siwc-test.dxf',demo_bytes)
    return AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='墙体材料？',mode='agent',
        runtime='responses_subscription',model_profile='siwc_sol',**kwargs))


def connected_lease(workspace, monkeypatch):
    @contextmanager
    def lease(): yield
    monkeypatch.setattr(workspace.siwc_account,'lease',lease)
    monkeypatch.setattr(workspace.siwc_account,'access_token',lambda:'fixture-only-token')


def test_profile_and_fallback_never_use_api_or_codex_credentials(workspace,demo_bytes,monkeypatch):
    settings=replace(workspace.settings,api_key='api-secret',api_base='https://api.test')
    p=settings.model_catalog()['siwc_sol'];assert p.api_key==p.api_base==''
    s=make_session(workspace,demo_bytes,allow_fallback=True)
    monkeypatch.setattr(workspace.siwc_account,'lease',lambda:pytest.fail('No auth expected'))
    with pytest.raises(ModelError,match='回退'):s.run()
    s.request.allow_fallback=False;s.request.runtime='codex_subscription'
    with pytest.raises(ModelError,match='订阅模型'):s.run()


def response(output, model='gpt-6.1-sol',status='completed'):
    return {'model':model,'status':status,'output':output,'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120,
        'input_tokens_details':{'cached_tokens':80},'output_tokens_details':{'reasoning_tokens':5}}}


def sse(value, kind='response.completed'):
    return 'data: '+json.dumps({'type':kind,'response':value})+'\n\n'


def test_direct_loop_preserves_function_pairing_citations_and_usage(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    rid=workspace.regions(s.request.snapshot_id)[0].region_id; requests=[]
    def handle(req):
        assert str(req.url)==RESOURCE+'/responses'
        assert req.headers['authorization']=='Bearer fixture-only-token'
        body=json.loads(req.content);requests.append(body)
        assert body['store'] is False and body['stream'] is True
        assert body['tools'][0]['type']=='namespace'
        assert not {'previous_response_id','max_tool_calls','temperature'} & body.keys()
        assert body['max_output_tokens'] == 8192
        if len(requests)==1:
            result=response([{'type':'function_call','namespace':'cad','name':'read_source_context','arguments':json.dumps({'region_id':rid}),'call_id':'c1'}])
        else:
            assert any(i.get('type')=='function_call_output' and i['call_id']=='c1' for i in body['input'])
            result=response([{'type':'message','role':'assistant','content':[{'type':'output_text','text':final(next(iter(s.evidence)))}]}])
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse(result))
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    result=s.run()
    assert len(requests)==2 and result['citations'] and result['billing']=='siwc_subscription'
    assert sum(u['total_tokens'] for u in result['usage'])==240
    assert not result['fallback_used'] and result['runtime_used']=='responses_subscription'


@pytest.mark.parametrize('kind,status',[('response.failed','failed'),('response.incomplete','incomplete'),('missing','completed')])
def test_failed_or_truncated_stream_is_not_published_or_retried(workspace,demo_bytes,monkeypatch,kind,status):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch);calls=[]
    def handle(req):
        calls.append(req)
        text=sse(response([],status=status),kind) if kind!='missing' else 'data: [DONE]\n\n'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=text)
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError):s.run()
    assert len(calls)==1 and not s.trace
    assert len(s.usage)==(0 if kind=='missing' else 1)


def test_unauthorized_tool_never_executes(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    monkeypatch.setattr(ResponsesTransport,'request',lambda *args:response([{'type':'function_call','namespace':'shell','name':'exec','call_id':'c','arguments':'{}'}]))
    with pytest.raises(ModelError,match='未授权'):s.run()
    assert not s.trace


def test_final_reserve_has_only_one_tool_free_validation_repair(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    rid=workspace.regions(s.request.snapshot_id)[0].region_id
    s.invoke_tool('read_source_context',{'region_id':rid})
    s.usage.append({'total_tokens':600000});bodies=[]
    def request(self,session,payload):
        bodies.append(payload)
        assert payload['tools'] and payload['tool_choice']=='none'
        assert json.loads(payload['input'][0]['content'])['evidence']
        return response([{'type':'message','content':[{'type':'output_text','text':'invalid'}]}])
    monkeypatch.setattr(ResponsesTransport,'request',request)
    with pytest.raises(ModelError,match='纠错次数或预算'):s.run()
    assert len(bodies)==2
    assert json.loads(bodies[-1]['input'][0]['content'])['validation_issue']


def test_routes_hide_credentials_and_reject_cross_origin(tmp_path,monkeypatch):
    app=create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as c:
        r=c.get('/api/siwc-account');assert r.status_code==200 and not r.json()['connected']
        assert c.post('/api/siwc-account/login',headers={'origin':'https://attacker.test'}).status_code==403
        p=next(x for x in c.get('/api/models').json()['items'] if x['id']=='siwc_sol')
        assert p['required_runtime']=='responses_subscription' and p['billing']=='subscription'


def test_invalid_signature_rejected(tmp_path,monkeypatch,signing_key):
    other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    a=SiwcAccount(tmp_path);pending(a)
    bad=jwt.encode({'iss':ISSUER,'aud':'oaiapp_fixture','exp':int(time.time())+600,'sub':'fixture-subject','nonce':'fixture-nonce'},other,algorithm='RS256',headers={'kid':'fixture'})
    mock_account(a,monkeypatch,signing_key,{'access_token':'fixture-access','id_token':bad,'token_type':'Bearer','scope':SCOPES,'expires_in':3600})
    with pytest.raises(ModelError): a.callback({'state':'fixture-state','code':'x','client_id':'oaiapp_fixture'})
    assert not a.status()['connected']


def test_logout_preserves_registration_and_clears_tokens_on_revocation_failure(tmp_path,monkeypatch,signing_key):
    a=SiwcAccount(tmp_path);pending(a);mock_account(a,monkeypatch,signing_key)
    a.callback({'state':'fixture-state','code':'x','client_id':'oaiapp_fixture'})
    monkeypatch.setattr(a,'client',lambda:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(503))))
    result=a.logout();assert result=={'connected':False,'remote_revocation_confirmed':False}
    with a.storage():
        data=a.load();record=data['accounts'][data['active']]
        assert record['client_id']=='oaiapp_fixture'
        assert not any(k in record for k in ('access_token','id_token','refresh_token'))
    try:
        q=parse_qs(urlsplit(a.login(data['active'])['auth_url']).query)
        assert q['client_id']==['oaiapp_fixture'] and 'agent_name_hint' not in q and 'id_token_hint' not in q
    finally:a.close()


@pytest.mark.parametrize('status',[401,403,429,503])
def test_http_errors_have_no_retry_or_sensitive_body(workspace,demo_bytes,monkeypatch,status):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch);requests=[]
    def handle(req):
        requests.append(req);return httpx.Response(status,text='private-token private-diagnostic')
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError) as caught:s.run()
    assert len(requests)==1 and 'private' not in caught.value.message and not s.usage


def test_final_answer_kept_when_final_usage_crosses_soft_budget(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    s.w.settings=replace(s.w.settings,subscription_budget_policy='legacy',stable_subscription_prefix=False)
    s.invoke_tool('read_source_context',{'region_id':workspace.regions(s.request.snapshot_id)[0].region_id})
    s.usage.append({'total_tokens':40000});calls=[]
    def request(self,session,payload):
        calls.append(payload);assert payload['tools']==[]
        r=response([{'type':'message','content':[{'type':'output_text','text':final(next(iter(s.evidence)))}]}])
        r['usage'].update(input_tokens=20000,output_tokens=122,total_tokens=20122)
        self.record_usage(session,r)
        return r
    monkeypatch.setattr(ResponsesTransport,'request',request)
    result=s.run();assert result['citations'] and result['subscription_budget']['reported_tokens']==60122
    assert len(calls)==1 and not result['fallback_used']


@pytest.mark.parametrize('content_type', ['text/event-stream; charset=utf-8', 'Text/Event-Stream; Charset=UTF-8', ''])
def test_sse_media_type_is_case_insensitive(workspace,demo_bytes,monkeypatch,content_type):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    s.runtime_diagnostics={'model_requests':[{}]}
    calls=[]
    def handle(req):
        calls.append(req)
        return httpx.Response(200,headers={'content-type':content_type,'x-request-id':'req_fixture'},text=sse(response([])))
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    result=ResponsesTransport(workspace.siwc_account).request(s,{'model':'gpt-6.1-sol'})
    assert result['status']=='completed' and len(calls)==1 and len(s.usage)==1
    diagnostic=s.runtime_diagnostics['model_requests'][0]['transport']
    assert diagnostic['content_type']==('text/event-stream' if content_type else '')
    assert diagnostic['content_type_missing']==(not bool(content_type))
    assert diagnostic['body_shape']=='event_stream' and diagnostic['terminal_event']=='response.completed'
    assert diagnostic['request_id']=='req_fixture'


@pytest.mark.parametrize('content_type,body,shape',[
    ('text/html','<!DOCTYPE html><html>private-token</html>','html'),
    ('application/json',json.dumps({'error':{'code':'subscription_sharing_unsupported_capability','param':'tools','message':'private-token'},'detail':'private-token'}),'json_object'),
    ('application/json',sse(response([])),'possible_sse_with_wrong_content_type'),
    ('text/event-stream-bogus',sse(response([])),'possible_sse_with_wrong_content_type'),
    ('text/plain', '', 'empty'),
    ('text/plain', 'private-token'*1000, 'unparsed_or_truncated'),
])
def test_bad_media_type_preserves_safe_diagnostics_without_retry(workspace,demo_bytes,monkeypatch,content_type,body,shape):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch);calls=[]
    def handle(req):
        calls.append(req)
        return httpx.Response(200,headers={'content-type':content_type,'x-request-id':'req_fixture','set-cookie':'private-token'},text=body)
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError) as caught:s.run()
    assert caught.value.code=='SIWC_STREAM_FORMAT' and len(calls)==1 and not s.trace and not s.usage
    diagnostic=s.runtime_diagnostics['model_requests'][0]['transport']
    assert diagnostic['http_status']==200 and diagnostic['body_shape']==shape
    assert diagnostic['body_bytes_inspected']<=8192 and diagnostic['request_id']=='req_fixture'
    assert 'private-token' not in json.dumps(s.runtime_diagnostics) + caught.value.message
    if shape=='json_object':
        assert diagnostic['error_code']=='subscription_sharing_unsupported_capability'
        assert diagnostic['error_param']=='tools'
    if len(body)>8192:assert diagnostic['body_truncated']


def test_unknown_error_fields_and_request_id_are_not_retained(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    def handle(req):
        return httpx.Response(403,headers={'content-type':'application/json','x-request-id':'private-token'},
            json={'error':{'code':'private-token','param':'private-token','message':'private-token'}})
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError):s.run()
    diagnostic=s.runtime_diagnostics['model_requests'][0]['transport']
    assert diagnostic['error_code']==diagnostic['error_param']=='other_redacted'
    assert 'private-token' not in json.dumps(s.runtime_diagnostics)


def test_body_diagnostic_read_failure_keeps_original_error(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch)
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError('private-token')
            yield b''
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(
        lambda req:httpx.Response(503,headers={'content-type':'application/json'},stream=BrokenBody()))))
    with pytest.raises(ModelError) as caught:s.run()
    assert caught.value.code=='SIWC_HTTP_ERROR'
    assert s.runtime_diagnostics['model_requests'][0]['transport']['body_shape']=='read_interrupted'
    assert 'private-token' not in json.dumps(s.runtime_diagnostics)


@pytest.mark.parametrize('body', [
    '<html>private-token</html>', '{"output":"private-token"}',
    'data: [DONE]\n\n', 'data: []\n\n', 'data: {"type":"untrusted"}\n\n',
    sse(response([],status='failed'),'response.failed'),
    sse(response([],status='incomplete'),'response.incomplete'),
    sse(response([],model='different-model')),
])
def test_missing_content_type_does_not_accept_invalid_or_failed_responses(workspace,demo_bytes,monkeypatch,body):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch);calls=[]
    def handle(req):
        calls.append(req);return httpx.Response(200,content=body.encode())
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError) as caught:s.run()
    assert len(calls)==1 and not s.trace
    assert 'private-token' not in json.dumps(s.runtime_diagnostics)+caught.value.message


def test_missing_content_type_preserves_full_cad_loop(workspace,demo_bytes,monkeypatch):
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch);calls=[]
    rid=workspace.regions(s.request.snapshot_id)[0].region_id
    def handle(req):
        body=json.loads(req.content);calls.append(body)
        if len(calls)==1:
            r=response([{'type':'function_call','namespace':'cad','name':'read_source_context',
                'arguments':json.dumps({'region_id':rid}),'call_id':'missing-header-call'}])
        else:
            assert any(i.get('type')=='function_call_output' and i['call_id']=='missing-header-call' for i in body['input'])
            r=response([{'type':'message','content':[{'type':'output_text','text':final(next(iter(s.evidence)))}]}])
        return httpx.Response(200,content=sse(r).encode())
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    result=s.run()
    assert len(calls)==2 and result['citations'] and not result['fallback_used']
    assert sum(u['total_tokens'] for u in result['usage'])==240
    assert all(r['transport']['content_type_missing'] for r in result['model_requests'])
