from dataclasses import replace
import httpx
import pytest
from cad_agent.adapters.model import ModelGateway, ModelError
from cad_agent.config import Settings


def settings(tmp_path,**kwargs):
    return Settings(data_dir=tmp_path,api_key='fixture-secret',api_base='https://secret-host.invalid',
                    model='fixture-model',protocol='openai',**kwargs)


@pytest.mark.parametrize('configured,remaining,expected_read,expected_connect',[
    (60,300,60,15),(120,300,120,15),(180,42.5,42.5,15),(120,4,4,4)])
def test_configured_idle_timeout_stays_within_remaining_budget(tmp_path,monkeypatch,configured,remaining,expected_read,expected_connect):
    captured=[]
    original=httpx.Client
    def client(**kwargs):
        captured.append(kwargs['timeout'])
        return original(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={
            'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],'usage':{}})),**kwargs)
    monkeypatch.setattr(httpx,'Client',client)
    gateway=ModelGateway(settings(tmp_path,model_read_timeout_seconds=configured))
    gateway.chat_turn([{'role':'user','content':'fixture'}],[],timeout=remaining)
    assert captured[0].read==expected_read
    assert captured[0].connect==expected_connect


def test_default_stays_sixty_and_env_load_is_explicit(tmp_path,monkeypatch):
    assert settings(tmp_path).model_read_timeout_seconds==60
    monkeypatch.setattr('cad_agent.config.local_env',lambda:{'CAD_AGENT_DATA_DIR':str(tmp_path),'CAD_MODEL_READ_TIMEOUT_SECONDS':'120'})
    assert Settings.load().model_read_timeout_seconds==120


@pytest.mark.parametrize('value',[0,-1,float('nan'),float('inf'),float('-inf')])
def test_invalid_timeout_fails_configuration(tmp_path,monkeypatch,value):
    with pytest.raises(ValueError,match='CAD_MODEL_READ_TIMEOUT_SECONDS'):
        settings(tmp_path,model_read_timeout_seconds=value)
    monkeypatch.setattr('cad_agent.config.local_env',lambda:{'CAD_AGENT_DATA_DIR':str(tmp_path),'CAD_MODEL_READ_TIMEOUT_SECONDS':str(value)})
    with pytest.raises(ValueError,match='CAD_MODEL_READ_TIMEOUT_SECONDS'):
        Settings.load()


@pytest.mark.parametrize('exception,expected',[
    (httpx.ReadTimeout,'MODEL_READ_TIMEOUT'),(httpx.ConnectTimeout,'MODEL_UNREACHABLE'),
    (httpx.ConnectError,'MODEL_UNREACHABLE')])
def test_read_timeout_classified_without_provider_details(tmp_path,monkeypatch,exception,expected):
    original=httpx.Client
    def handle(request):
        raise exception('fixture-secret https://secret-host.invalid',request=request)
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(ModelError) as caught:
        ModelGateway(settings(tmp_path)).chat_turn([],[],timeout=120)
    assert caught.value.code==expected
    assert 'fixture-secret' not in str(caught.value)
    assert 'secret-host.invalid' not in str(caught.value)
