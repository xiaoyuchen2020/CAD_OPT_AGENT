"""Explicit operator setup. The API never builds images or installs packages."""
import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from cad_agent.extensions.adaptation.sandbox import ROOT, runner_hash, execute
from cad_agent.extensions.table_recovery.sandbox import docker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    # Reuse the already audited immutable base, never a mutable latest tag.
    reference = json.loads((args.data_dir/'table-recovery-runtime.json').read_text())['base_image']
    base_id = reference.split('@', 1)[1]
    docker(['image','inspect',base_id])
    local_base = 'cad-adaptation-base:'+base_id.split(':')[1][:16]
    docker(['tag',base_id,local_base])
    sha = runner_hash(); tag = 'cad-adaptation:'+sha[:16]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name in ('Dockerfile','runner.py'):
            shutil.copy2(ROOT/name,folder/name)
        shutil.copy2(ROOT/'../table_recovery/worker.py',folder/'fixed_worker.py')
        subprocess.run(['docker','build','--pull=false','--network=none','--build-arg','BASE_IMAGE='+local_base,
            '--build-arg','WORKER_SHA256='+sha,'-t',tag,str(folder)],check=True,timeout=120)
    image = json.loads(docker(['image','inspect',tag]))[0]
    args.data_dir.mkdir(exist_ok=True,parents=True)
    config = dict(image_id=image['Id'],base_image=reference,worker_sha256=sha, runtime='docker-linux-dynamic@1')
    (args.data_dir/'adaptation-runtime.json').write_text(json.dumps(config,indent=2))
    probe = execute(args.data_dir, {}, 'def adapt(bundle):\n    from fixed_worker import probe\n    return probe()\n')
    assert probe['output']['uid'] == 65534
    assert all(probe['output'][k] for k in ('root_write_denied','host_file_unavailable','network_denied','sensitive_env_absent'))
    (args.data_dir/'adaptation-sandbox-probe.json').write_text(json.dumps(probe,indent=2))
    print(json.dumps(config))


if __name__ == '__main__':
    main()
