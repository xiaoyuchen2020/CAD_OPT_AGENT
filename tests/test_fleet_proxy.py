import httpx
import pytest
from fastapi.testclient import TestClient
from cad_agent.fleet.app import create_app, FleetSettings
from cad_agent.fleet import proxy


@pytest.fixture
def setup(tmp_path, monkeypatch):
    observed=[]
    response={'value':httpx.Response(200,json={'ok':True})}
    def handler(request):
        observed.append(request)
        return response['value']
    original=httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    app=create_app(FleetSettings(tmp_path,'worker-secret-'+'x'*32,password='operator'))
    with TestClient(app) as client:
        yield client,observed,response


def login(client):
    assert client.post('/api/fleet/login',json={'password':'operator'}).status_code==200


def test_proxy_requires_session_even_for_worker_credentials(setup):
    client,seen,_=setup
    assert client.get('/nodes/a/api/health').status_code==401
    assert client.get('/nodes/a/api/health',headers={'Authorization':'Bearer worker-secret-'+'x'*32}).status_code==401
    assert not seen
    login(client)
    assert client.get('/nodes/a/api/health').status_code==200


@pytest.mark.parametrize('path',['api/fleet-import','api/fleet-runs','api/fleet-internal/data','api/fleet/anything','api//fleet-runs','api/%66leet-import','api/%2566leet-runs','%2Fapi/fleet-runs','assets/%252e%252e/api/fleet-import'])
def test_internal_and_encoded_paths_never_forward(setup,path):
    client,seen,_=setup
    login(client)
    assert client.post('/nodes/a/'+path,json={},headers={'X-Fleet-Token':'attempted-leak'}).status_code==404
    assert not seen


def test_cross_origin_blocked_even_with_supplied_worker_token(setup):
    client,seen,_=setup
    login(client)
    assert client.post('/nodes/a/api/chat',json={},headers={'Origin':'https://outside.example','Authorization':'Bearer worker-secret-'+'x'*32}).status_code==403
    assert not seen


def test_credentials_host_and_forwarded_headers_not_sent(setup):
    client,seen,_=setup
    login(client)
    result=client.post('/nodes/b/api/chat?part=1',json={'question':'facts'},headers={'Origin':'http://testserver','Authorization':'Bearer attempted-secret','X-Fleet-Token':'attempted-secret','X-Fleet-Execution':'attempt','X-Forwarded-Host':'evil.example'})
    assert result.status_code==200
    request=seen[0]
    assert request.url==httpx.URL('http://127.0.0.1:8766/api/chat?part=1')
    assert request.headers['host']=='127.0.0.1:8766'
    assert all(h not in request.headers for h in ('authorization','cookie','x-fleet-token','x-fleet-execution','x-forwarded-host','origin'))
    assert b'facts' in request.content


@pytest.mark.parametrize('upstream',['https://outside.example:443','http://127.0.0.1:8765@outside.example:80','http://127.0.0.1:8765/prefix','http://127.0.0.1:8765/?target=x','file:///tmp/socket','http://127.0.0.1:bad'])
def test_only_fixed_loopback_upstreams_allowed(setup,monkeypatch,upstream):
    client,seen,_=setup
    login(client)
    monkeypatch.setenv('CAD_FLEET_AGENT_A_URL',upstream)
    assert client.get('/nodes/a/api/health').status_code==503
    assert not seen
    assert client.get('/nodes/evil/api/health').status_code==404


def test_json_links_rewrite_without_changing_source_facts(setup):
    client,_,reply=setup
    login(client)
    payload={'answer':'/api/source is literal drawing text','citations':[{'evidence_url':'/api/evidence/1','quote':'/api/source','preview_url':'/assets/source.svg'}], 'external_url':'https://outside.example/a','ids':['/api/literal-id']}
    reply['value']=httpx.Response(200,json=payload)
    actual=client.get('/nodes/b/api/chat-runs/x').json()
    assert actual['answer']==payload['answer']
    assert actual['citations'][0]['quote']=='/api/source'
    assert actual['citations'][0]['evidence_url']=='/nodes/b/api/evidence/1'
    assert actual['citations'][0]['preview_url']=='/nodes/b/assets/source.svg'
    assert actual['ids']==payload['ids']
    assert actual['external_url']==payload['external_url']


def test_html_js_and_binary_rewrite(setup):
    client,_,reply=setup
    login(client)
    reply['value']=httpx.Response(200,text='<script src="/assets/app.js"></script><pre>/api/fact</pre>',headers={'content-type':'text/html'})
    result=client.get('/nodes/a/')
    assert 'src="/nodes/a/assets/app.js"' in result.text and '<pre>/api/fact</pre>' in result.text
    reply['value']=httpx.Response(200,text="fetch('/api/drawings')",headers={'content-type':'application/javascript'})
    assert '/nodes/a/api/drawings' in client.get('/nodes/a/assets/app.js').text
    raw=b'\x00/api/not-a-url\x01'
    reply['value']=httpx.Response(200,content=raw,headers={'content-type':'application/octet-stream','set-cookie':'unsafe=yes','location':'http://elsewhere'})
    result=client.get('/nodes/a/api/drawings/x/source')
    assert result.content==raw and 'set-cookie' not in result.headers and 'location' not in result.headers
