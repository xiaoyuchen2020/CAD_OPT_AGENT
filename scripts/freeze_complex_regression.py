"""Copy this app's code into a run-once audit snapshot; no Git state is touched."""
from pathlib import Path
import shutil,json,hashlib,argparse
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--revision',default='')
args=parser.parse_args()
if args.revision and not args.revision.isalnum():raise SystemExit('Revision must be alphanumeric.')
suffix='-'+args.revision if args.revision else ''
DEST=ROOT/('artifacts/complex-repair-frozen-20261016'+suffix)
if DEST.exists():raise SystemExit('Snapshot already exists; use its frozen scripts, do not overwrite.')
for folder in ('cad_agent','web','tests','harness'):
 shutil.copytree(ROOT/folder,DEST/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
for name in ('pyproject.toml','README.md'):
 shutil.copy2(ROOT/name,DEST/name)
(DEST/'scripts').mkdir()
for name in ('regress_agent_workflow.py','regress_complex_repair.py','regress_complex_extensions.py','audit_complex_repair.py','regress_spatial_repair.py'):
 s=(ROOT/'scripts'/name).read_text()
 if name=='regress_agent_workflow.py':s=s.replace('ROOT=Path(__file__).resolve().parents[1]',f'ROOT=Path({str(ROOT)!r})')
 if name in ('regress_complex_repair.py','regress_complex_extensions.py'):
  s=s.replace("return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'cad_agent').rglob('*.py')}","code_root=Path(__file__).resolve().parents[1]\n return {str(p.relative_to(code_root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (code_root/'cad_agent').rglob('*.py')}")
  s=s.replace("OUT=ROOT/'artifacts/complex-repair-20261016'",f"OUT=ROOT/'artifacts/complex-repair-final-20261016{suffix}'")
  s=s.replace("OUT=ROOT/'artifacts/complex-repair-extensions-20261016'",f"OUT=ROOT/'artifacts/complex-repair-extensions-final-20261016{suffix}'")
 (DEST/'scripts'/name).write_text(s)
manifest={str(p.relative_to(DEST)):hashlib.sha256(p.read_bytes()).hexdigest() for p in DEST.rglob('*') if p.is_file()}
(DEST/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(DEST)
