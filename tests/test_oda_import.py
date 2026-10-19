from dataclasses import replace
from hashlib import sha256
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cad_agent.adapters.oda import OdaConverter, ConversionError
from cad_agent.app import create_app
from cad_agent.config import Settings

DWG = b'AC1032' + b'fixture-dwg'
JOB = 'a' * 32
SECRET = 'private-oda-test-key'


class OdaServer:
    def __init__(self, dxf, *, states=('queued', 'running', 'succeeded'), error='', mutate=None,
                 http_status=None, request_error=None):
        self.dxf = dxf
        self.states = iter(states)
        self.state = states[-1]
        self.error = error
        self.mutate = mutate
        self.http_status = http_status
        self.request_error = request_error
        self.requests = []
        self.result = {
            'schema_version': '2.0', 'sdk_version': '27.07', 'extractor_revision': '20261014.2',
            'input': {'sha256': sha256(DWG).hexdigest(), 'bytes': len(DWG)},
            'entities': [], 'entity_records_in_all_block_definitions': 0,
            'companion_dxf': {'sha256': sha256(dxf).hexdigest(), 'bytes': len(dxf),
                              'url': 'https://never-follow.example/key-theft'},
        }
        if mutate:
            mutate(self.result)
        self.raw = json.dumps(self.result).encode()

    def handle(self, request):
        self.requests.append(request)
        assert request.headers['x-api-key'] == SECRET
        assert request.url.host == 'oda.test'
        if self.request_error:
            raise self.request_error('private detail ' + SECRET, request=request)
        if self.http_status:
            return httpx.Response(self.http_status, text='private detail ' + SECRET)
        if request.method == 'POST':
            assert request.url.path == '/jobs'
            assert request.content == DWG
            assert request.headers['content-type'] == 'application/octet-stream'
            return httpx.Response(202, json={'id': JOB, 'sha256': sha256(DWG).hexdigest()})
        if request.url.path.endswith('/result'):
            return httpx.Response(200, content=self.raw)
        if request.url.path.endswith('/dxf'):
            return httpx.Response(200, content=self.dxf)
        self.state = next(self.states, self.state)
        return httpx.Response(200, json={'id': JOB, 'sha256': sha256(DWG).hexdigest(),
                                        'status': self.state, 'error': self.error})


def config(tmp_path, **kwargs):
    return Settings(data_dir=tmp_path/'data', oda_base_url='http://oda.test',
                    oda_api_key=SECRET, oda_poll_seconds=.001, **kwargs)


def attach(app, server):
    app.state.workspace.converter = OdaConverter(app.state.workspace.settings,
                                                httpx.MockTransport(server.handle))


def test_dwg_import_downloads_identity_cache_and_dxf_unchanged(tmp_path, demo_bytes):
    server = OdaServer(demo_bytes)
    app = create_app(config(tmp_path))
    attach(app, server)
    with TestClient(app) as client:
        health = client.get('/api/health')
        assert health.json()['supported_formats'] == ['dxf', 'dwg']
        assert health.json()['dwg_conversion']['configured']
        assert SECRET not in health.text
        uploaded = client.post('/api/drawings', files={'file': ('中文图纸.DWG', DWG)})
        assert uploaded.status_code == 201, uploaded.text
        d = uploaded.json()
        sid = d['id']
        assert d['source_format'] == 'DWG' and d['parsed_format'] == 'DXF'
        assert d['original_sha256'] == sha256(DWG).hexdigest()
        assert d['sha256'] == sha256(demo_bytes).hexdigest()
        assert d['conversion']['job_id'] == JOB
        assert d['text_count'] == 29 and d['region_count'] == 21
        assert client.get(f'/api/drawings/{sid}/source').content == DWG
        assert client.get(f'/api/drawings/{sid}/dxf').content == demo_bytes
        assert client.get(f'/api/drawings/{sid}/oda-json').content == server.raw
        # Deduplication does not upload again or depend on remote result retention.
        count = len(server.requests)
        assert client.post('/api/drawings', files={'file': ('renamed.dwg', DWG)}).json()['id'] == sid
        assert len(server.requests) == count
        assert '%E4%B8%AD' in server.requests[0].headers['x-filename']
        native = client.post('/api/drawings', files={'file': ('native.dxf', demo_bytes)}).json()
        assert native['id'] != sid and native['source_format'] == 'DXF'
        assert client.get(f"/api/drawings/{native['id']}/source").content == demo_bytes
        assert client.get(f"/api/drawings/{native['id']}/oda-json").status_code == 404
        assert len(server.requests) == count
        assert client.get('/api/drawings/foreign/oda-json').status_code == 404
        # Corrupted local artifacts must not become a false successful cache hit.
        app.state.workspace.store.artifact(sid, 'source.dxf').write_bytes(b'bad')
        failed = client.post('/api/drawings', files={'file': ('drawing.dwg', DWG)})
        assert failed.status_code == 502
        assert failed.json()['error']['code'] == 'ODA_LOCAL_RESULT_DAMAGED'
        assert len(server.requests) == count


@pytest.mark.parametrize('options,code,status', [
    ({'http_status': 401}, 'ODA_AUTH_FAILED', 503),
    ({'http_status': 429}, 'ODA_BUSY', 503),
    ({'http_status': 503}, 'ODA_HTTP_ERROR', 502),
    ({'http_status': 302}, 'ODA_HTTP_ERROR', 502),
    ({'request_error': httpx.ConnectError}, 'ODA_UNAVAILABLE', 503),
    ({'request_error': httpx.ReadTimeout}, 'ODA_TIMEOUT', 504),
    ({'states': ('failed',), 'error': 'native secret '+SECRET}, 'ODA_CONVERSION_FAILED', 502),
    ({'states': ('failed',), 'error': 'authorization unavailable'}, 'ODA_TRIAL_UNAVAILABLE', 503),
    ({'states': ('failed',), 'error': 'authorization 429'}, 'ODA_TRIAL_RATE_LIMITED', 503),
    ({'states': ('unexpected',)}, 'ODA_RESULT_MISMATCH', 502),
    ({'mutate': lambda x: x['input'].update(sha256='0'*64)}, 'ODA_RESULT_MISMATCH', 502),
    ({'mutate': lambda x: x['companion_dxf'].update(sha256='0'*64)}, 'ODA_RESULT_MISMATCH', 502),
    ({'mutate': lambda x: x['companion_dxf'].update(bytes=1)}, 'ODA_RESULT_MISMATCH', 502),
    ({'mutate': lambda x: x.update(entities=None)}, 'ODA_RESULT_MISMATCH', 502),
])
def test_conversion_failure_stops_import(tmp_path, demo_bytes, options, code, status):
    server = OdaServer(demo_bytes, **options)
    app = create_app(config(tmp_path))
    attach(app, server)
    with TestClient(app) as client:
        existing = client.post('/api/drawings', files={'file': ('existing.dxf', demo_bytes)}).json()
        failed = client.post('/api/drawings', files={'file': ('bad.dwg', DWG)})
        assert failed.status_code == status, failed.text
        assert failed.json()['error']['code'] == code
        assert '已终止' in failed.text and SECRET not in failed.text
        assert [x['id'] for x in client.get('/api/drawings').json()['items']] == [existing['id']]
        assert app.state.workspace.store.list('job') == []
        assert list((tmp_path/'data'/'drawings').iterdir()) == [tmp_path/'data'/'drawings'/existing['id']]
        assert not list((tmp_path/'data').glob('.import-*'))
        assert sum(r.method == 'POST' for r in server.requests) == 1


def test_valid_response_but_unreadable_dxf_aborts(tmp_path):
    app = create_app(config(tmp_path))
    attach(app, OdaServer(b'not dxf'))
    with TestClient(app) as client:
        failed = client.post('/api/drawings', files={'file': ('bad.dwg', DWG)})
        assert failed.status_code == 502
        assert failed.json()['error']['code'] == 'ODA_DXF_UNREADABLE'
        assert client.get('/api/drawings').json()['items'] == []
        assert not (tmp_path/'data'/'drawings').exists()
        assert not list((tmp_path/'data').glob('.import-*'))


def test_total_timeout_and_result_bound(tmp_path, demo_bytes):
    settings = config(tmp_path, oda_timeout_seconds=.004)
    server = OdaServer(demo_bytes, states=('running',))
    with pytest.raises(ConversionError) as e:
        OdaConverter(settings, httpx.MockTransport(server.handle)).convert('a.dwg', DWG)
    assert e.value.code == 'ODA_TIMEOUT'
    server = OdaServer(demo_bytes)
    with pytest.raises(ConversionError) as e:
        OdaConverter(replace(settings, oda_timeout_seconds=10, oda_max_result_bytes=10),
                     httpx.MockTransport(server.handle)).convert('a.dwg', DWG)
    assert e.value.code == 'ODA_RESULT_TOO_LARGE'


def test_unconfigured_and_invalid_input_fail_without_network(tmp_path, demo_bytes):
    app = create_app(Settings(data_dir=tmp_path/'data'))
    with TestClient(app) as client:
        assert not client.get('/api/health').json()['dwg_conversion']['configured']
        for name, content, status in [('a.dwg', DWG, 503), ('a.dwg', b'not dwg', 400),
                                      ('a.pdf', demo_bytes, 400)]:
            response = client.post('/api/drawings', files={'file': (name, content)})
            assert response.status_code == status
        assert client.get('/api/drawings').json()['items'] == []
        assert client.post('/api/drawings', files={'file': ('ok.dxf', demo_bytes)}).status_code == 201
