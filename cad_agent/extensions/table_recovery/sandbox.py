"""Docker Linux isolation for fixed methods; no fallback to host execution."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from .bundle import encoded
from .contracts import MAX_INPUT, MAX_OUTPUT

ROOT = Path(__file__).resolve().parent


class SandboxError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def docker(args, timeout=10):
    try:
        result = subprocess.run(['docker', *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError('SANDBOX_UNAVAILABLE') from exc
    if result.returncode:
        raise SandboxError('SANDBOX_UNAVAILABLE')
    return result.stdout


def runtime(data_dir):
    path = Path(data_dir)/'table-recovery-runtime.json'
    try:
        config = json.loads(path.read_text())
        image = config['image_id']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
            raise ValueError('unpinned image')
        local_sha=hashlib.sha256((ROOT/'worker.py').read_bytes()).hexdigest()
        info=json.loads(docker(['image','inspect',image]))[0]
        if info['Os']!='linux' or info['Config']['Labels'].get('cad.table-recovery.worker-sha256')!=local_sha or config['worker_sha256']!=local_sha:
            raise ValueError('worker mismatch')
        if info['Config']['Entrypoint'] != ['python','-I','-B','/app/worker.py']:
            raise ValueError('entrypoint mismatch')
        if json.loads(docker(['info','--format','{{json .OSType}}'])) != 'linux':
            raise ValueError('Linux required')
        return config
    except (OSError,ValueError,KeyError,IndexError,TypeError) as exc:
        raise SandboxError('SANDBOX_UNAVAILABLE') from exc


def execute(data_dir, request, *, timeout=30, cancel=None, _runtime=runtime):
    config=_runtime(data_dir)
    payload=encoded(request)
    if len(payload)>MAX_INPUT+1024:
        raise SandboxError('INPUT_LIMIT_EXCEEDED')
    name='cad-table-'+uuid4().hex
    command=['docker','run','--name',name,'--pull=never','--network=none','--read-only',
             '--cap-drop=ALL','--security-opt=no-new-privileges:true','--pids-limit=32',
             '--memory=512m','--memory-swap=512m','--cpus=1','--user=65534:65534',
             '--ipc=none','--log-driver=none','--ulimit=cpu=20:20',
             '--ulimit=fsize=2097152:2097152','--ulimit=nofile=64:64',
             '--tmpfs=/work:rw,noexec,nosuid,size=64m,mode=1777','-i',config['image_id']]
    started=time.monotonic()
    stop=threading.Event();overflow=threading.Event()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process=None;watcher=None
        try:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=output,stderr=errors)
            def watch():
                import os
                while not stop.wait(.02):
                    if cancel is not None and cancel.is_set():
                        process.kill()
                        return
                    if os.fstat(output.fileno()).st_size>MAX_OUTPUT or os.fstat(errors.fileno()).st_size>65536:
                        overflow.set()
                        process.kill()
                        return
            watcher=threading.Thread(target=watch,daemon=True);watcher.start()
            try:
                process.communicate(payload,timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill();process.wait()
                raise SandboxError('SANDBOX_TIMEOUT') from exc
            output.seek(0);raw=output.read(MAX_OUTPUT+1)
            errors.seek(0);err=errors.read(65537)
            if cancel is not None and cancel.is_set():
                raise SandboxError('SANDBOX_CANCELLED')
            if overflow.is_set() or len(raw)>MAX_OUTPUT or len(err)>65536:
                raise SandboxError('SANDBOX_OUTPUT_LIMIT')
            if process.returncode:
                raise SandboxError('SANDBOX_EXECUTION_FAILED')
            info=json.loads(docker(['inspect',name]))[0]
            host=info['HostConfig']
            isolation=dict(network=host['NetworkMode'],read_only=host['ReadonlyRootfs'],
                           cap_drop=host['CapDrop'],security_options=host['SecurityOpt'],
                           memory_bytes=host['Memory'],pids_limit=host['PidsLimit'],
                           mounts=info['Mounts'],user=info['Config']['User'],image_id=info['Image'])
            if isolation['network']!='none' or not isolation['read_only'] or isolation['mounts']:
                raise SandboxError('SANDBOX_ISOLATION_MISMATCH')
            try:
                result=json.loads(raw,parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
            except (ValueError,UnicodeError) as exc:
                raise SandboxError('SANDBOX_INVALID_JSON') from exc
            return dict(output=result,output_sha256=hashlib.sha256(raw).hexdigest(),
                        worker_sha256=config['worker_sha256'],isolation=isolation,
                        elapsed_seconds=round(time.monotonic()-started,3))
        except OSError as exc:
            raise SandboxError('SANDBOX_UNAVAILABLE') from exc
        finally:
            stop.set()
            if watcher:watcher.join(timeout=1)
            if process and process.poll() is None:
                process.kill();process.wait()
            # Remove the whole container, including descendants, even on timeout.
            docker(['rm','-f',name])
