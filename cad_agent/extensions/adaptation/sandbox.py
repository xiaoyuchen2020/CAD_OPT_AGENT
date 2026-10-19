import hashlib
import json
import re
from pathlib import Path
from ..table_recovery.sandbox import docker, execute as isolated_execute, SandboxError

ROOT = Path(__file__).resolve().parent


def runner_hash():
    return hashlib.sha256((ROOT/'runner.py').read_bytes() + (ROOT/'../table_recovery/worker.py').read_bytes()).hexdigest()


def runtime(data_dir):
    try:
        config = json.loads((Path(data_dir)/'adaptation-runtime.json').read_text())
        image = config['image_id']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise ValueError('UNPINNED_IMAGE')
        info = json.loads(docker(['image', 'inspect', image]))[0]
        if (info['Os'] != 'linux' or config['worker_sha256'] != runner_hash()
            or info['Config']['Labels'].get('cad.adaptation.runner-sha256') != runner_hash()
            or info['Config']['Entrypoint'] != ['python', '-I', '-B', '/app/runner.py']):
            raise ValueError('RUNTIME_MISMATCH')
        return config
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise SandboxError('SANDBOX_UNAVAILABLE') from exc


def execute(data_dir, bundle, code, cancel=None, timeout=20):
    if len(code.encode()) > 24000:
        raise SandboxError('CODE_LIMIT_EXCEEDED')
    return isolated_execute(data_dir, dict(bundle=bundle, code=code,
        code_sha256=hashlib.sha256(code.encode()).hexdigest()), timeout=timeout, cancel=cancel, _runtime=runtime)
