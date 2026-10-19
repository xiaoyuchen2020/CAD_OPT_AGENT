"""Explicit, single-DWG LAN integration check. No LLM or production rules."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import argparse
import json
import time

from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dwg', type=Path)
    parser.add_argument('--out', type=Path, default=Path('artifacts/oda-integration-20261015/live'))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.load(), data_dir=(args.out/'data').resolve())
    content = args.dwg.read_bytes()
    start = time.monotonic()
    with TestClient(create_app(settings)) as client:
        response = client.post('/api/drawings', files={'file': (args.dwg.name, content)})
        if response.status_code != 201:
            report = {'status': 'failed', 'http_status': response.status_code, 'response': response.json()}
            (args.out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            raise RuntimeError(response.json())
        drawing = response.json()
        sid = drawing['id']
        assert drawing['source_format'] == 'DWG'
        assert drawing['original_sha256'] == sha256(content).hexdigest()
        checks = {}
        for route, expected in [('source', drawing['original_sha256']), ('dxf', drawing['sha256']),
                                ('oda-json', drawing['conversion']['json_sha256'])]:
            download = client.get(f'/api/drawings/{sid}/{route}')
            assert download.status_code == 200 and sha256(download.content).hexdigest() == expected
            checks[route] = {'bytes': len(download.content), 'sha256': expected}
        preview = client.get(f'/api/drawings/{sid}/preview')
        assert preview.status_code == 200
        sources = client.get(f'/api/drawings/{sid}/sources')
        assert sources.status_code == 200 and sources.json()['total'] == drawing['region_count']
        cached = client.post('/api/drawings', files={'file': (args.dwg.name, content)})
        assert cached.json()['id'] == sid and cached.json()['conversion']['job_id'] == drawing['conversion']['job_id']
        failed = client.post('/api/drawings', files={'file': ('invalid.dwg', b'not a dwg')})
        assert failed.status_code == 400 and len(client.get('/api/drawings').json()['items']) == 1
        assert client.app.state.workspace.store.list('job') == []
        report = {'status': 'passed', 'source': str(args.dwg.resolve()), 'drawing': drawing,
                  'downloads': checks, 'preview_status': preview.status_code,
                  'cached_snapshot_reused': True, 'invalid_import_stopped': True,
                  'seconds': round(time.monotonic()-start, 3)}
        (args.out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'status': report['status'], 'snapshot_id': sid, 'conversion': drawing['conversion'],
                          'text_count': drawing['text_count'], 'region_count': drawing['region_count'],
                          'seconds': report['seconds']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
