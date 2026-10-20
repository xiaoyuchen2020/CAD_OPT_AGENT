"""Run the local suite against a hash-checked working tree, without model calls."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import argparse

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/bottleneck-round3-20261020'


def manifest():
    paths = [p for folder in ('cad_agent', 'web', 'tests')
             for p in (ROOT / folder).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts
             and p.suffix in ('.py', '.js', '.html', '.css', '.json')]
    paths.append(ROOT / 'pyproject.toml')
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', default='integration')
    parser.add_argument('tests', nargs='*', default=['tests'])
    args = parser.parse_args()
    label = args.label
    OUT.mkdir(parents=True, exist_ok=True)
    before = manifest()
    prefix = 'validation' if label == 'integration' else label
    (OUT / f'{prefix}-source-before.json').write_text(json.dumps(before, indent=2) + '\n')
    start = time.monotonic()
    command = [sys.executable, '-m', 'pytest', *args.tests, '-q', '--disable-warnings']
    log_name = 'integration-full.log' if label == 'integration' else f'{label}.log'
    with (OUT / log_name).open('w') as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    after = manifest()
    changed = [p for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]
    (OUT / f'{prefix}-source-after.json').write_text(json.dumps(after, indent=2) + '\n')
    log = (OUT / log_name).read_text()
    summary = dict(
        completed_at=datetime.now(timezone.utc).isoformat(),
        command=command, exit_code=result.returncode,
        elapsed_seconds=round(time.monotonic() - start, 3),
        counts={name: int(n) for n, name in re.findall(r'(\d+) (passed|failed|skipped|errors?)', log[-4000:])},
        source_unchanged=not changed, changed_during_validation=changed,
        manifest_file_count=len(after), model_calls=0,
        log=log_name,
    )
    (OUT / f'{label}-validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    raise SystemExit(result.returncode or bool(changed))
