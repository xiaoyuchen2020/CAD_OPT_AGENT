"""Build the fixed worker image from a locally pulled official Python image.

No drawing data enters the build context. The app never pulls/builds images.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from cad_agent.extensions.table_recovery.sandbox import ROOT, docker, execute


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args()
    base=json.loads(docker(['image','inspect','python:3.11-slim']))[0]
    # docker save/load may omit repository digests; pin the actual local image ID.
    reference='python@'+base['Id']
    local_base='cad-sandbox-base:'+base['Id'].split(':')[1][:16]
    docker(['tag',base['Id'],local_base])
    sha=hashlib.sha256((ROOT/'worker.py').read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp)
        for name in ('Dockerfile','worker.py'):shutil.copy2(ROOT/name,folder/name)
        tag='cad-table-recovery:'+sha[:16]
        subprocess.run(['docker','build','--pull=false','--network=none',
                        '--build-arg','BASE_IMAGE='+local_base,'--build-arg','WORKER_SHA256='+sha,
                        '-t',tag,str(folder)],check=True,timeout=120)
    image=json.loads(docker(['image','inspect',tag]))[0]
    config=dict(image_id=image['Id'],base_image=reference,worker_sha256=sha,
                method_ref='anchored-lines@1',runtime='docker-linux-fixed-worker')
    args.data_dir.mkdir(parents=True,exist_ok=True)
    path=args.data_dir/'table-recovery-runtime.json'
    path.write_text(json.dumps(config,indent=2))
    probe=execute(args.data_dir,dict(operation='probe'))
    checks=probe['output']
    assert checks['uid']==65534 and all(checks[k] for k in ('root_write_denied','host_file_unavailable','network_denied','sensitive_env_absent'))
    (args.data_dir/'table-recovery-sandbox-probe.json').write_text(json.dumps(probe,indent=2))
    print(json.dumps(dict(runtime_file=str(path.resolve()),**config,probe=checks)))


if __name__=='__main__':main()
