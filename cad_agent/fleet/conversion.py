"""Validated, immutable ODA bundles shared by local fleet workers."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import fcntl
import json
import os
import tempfile
import time
import httpx
from cad_agent.adapters.oda import OdaConverter


def validate_bundle(settings, filename, original, dxf, oda_json, job_id):
    """Replay a bundle through the same complete validator as a live ODA response.

    No external I/O: response URLs are deliberately ignored by OdaConverter.
    """
    digest = sha256(original).hexdigest()
    def reply(request):
        if request.method == 'POST':
            return httpx.Response(202, json={'id': job_id, 'sha256': digest})
        if request.url.path.endswith('/result'):
            return httpx.Response(200, content=oda_json)
        if request.url.path.endswith('/dxf'):
            return httpx.Response(200, content=dxf)
        return httpx.Response(200, json={'id': job_id, 'sha256': digest, 'status': 'succeeded'})
    conf = replace(settings, oda_base_url='http://bundle.invalid', oda_api_key='in-memory-validation')
    return OdaConverter(conf, httpx.MockTransport(reply)).convert(filename, original)


def cached_conversion(settings, filename, original, cache_dir, revision='1', check=None):
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    key = sha256((sha256(original).hexdigest() + settings.oda_base_url + revision).encode()).hexdigest()
    folder = root/key
    # One converter globally, even with independent agent databases.
    with (root/'conversion.lock').open('a') as lock:
        while True:
            if check:
                check()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.2)
        if check:
            check()
        if folder.exists():
            manifest = json.loads((folder/'manifest.json').read_text())
            return validate_bundle(settings, filename, original, (folder/'source.dxf').read_bytes(),
                                   (folder/'oda.json').read_bytes(), manifest['job_id'])
        converted = OdaConverter(settings).convert(filename, original)
        with tempfile.TemporaryDirectory(dir=root, prefix='.conversion-') as tmp:
            stage = Path(tmp)
            (stage/'source.dwg').write_bytes(original)
            (stage/'source.dxf').write_bytes(converted.dxf)
            (stage/'oda.json').write_bytes(converted.json_bytes)
            (stage/'manifest.json').write_text(json.dumps(converted.metadata))
            # Shared group may read bundles; no agent data or credentials are here.
            os.chmod(stage, 0o2770)
            for item in stage.iterdir():
                os.chmod(item, 0o660)
            os.replace(stage, folder)
        return converted
