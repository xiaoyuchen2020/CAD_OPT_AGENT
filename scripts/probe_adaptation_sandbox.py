"""Adversarial probes executed ONLY by the no-mount/no-network Linux sandbox."""
import json
from pathlib import Path
from threading import Event, Timer
from cad_agent.config import ROOT
from cad_agent.extensions.adaptation.sandbox import execute
from cad_agent.extensions.table_recovery.sandbox import SandboxError, docker


def main():
    out=ROOT/'artifacts/dynamic-adaptation-20261016';out.mkdir(exist_ok=True)
    cases=[
        ('isolation', 'def adapt(b):\n from fixed_worker import probe\n return probe()', None),
        ('timeout', 'def adapt(b):\n while True: pass', 'SANDBOX_TIMEOUT'),
        ('stdout_limit', 'def adapt(b):\n print("x"*3000000)\n return {}','SANDBOX_OUTPUT_LIMIT'),
        ('stderr_limit', 'def adapt(b):\n import sys\n sys.stderr.write("x"*100000)\n return {}','SANDBOX_OUTPUT_LIMIT'),
        ('memory_limit', 'def adapt(b):\n x=bytearray(900*1024*1024)\n return len(x)','SANDBOX_EXECUTION_FAILED'),
        ('process_limit', '''def adapt(b):
 import subprocess
 children=[]
 try:
  for i in range(100): children.append(subprocess.Popen(['sleep','10']))
 except OSError:
  return {'denied':True,'spawned':len(children)}
 finally:
  for p in children: p.kill();p.wait()
 return {'denied':False}
''',None),
        ('host_file', 'def adapt(b):\n return open("/Users/vigorchen/Documents/WorkExplore/cad_opt_agent/.env").read()', 'SANDBOX_EXECUTION_FAILED'),
        ('invalid_json', 'def adapt(b):\n import sys\n sys.stdout.write("NaN")\n sys.exit(0)', 'SANDBOX_INVALID_JSON'),
    ]
    results=[]
    for name,code,expected in cases:
        entry=dict(probe=name,expected_error=expected)
        try:
            execution=execute(ROOT/'.data',{},code,timeout=2 if name=='timeout' else 15)
            entry.update(execution=execution,passed=expected is None)
            if name=='process_limit':entry['passed'] &= execution['output']['denied']
            if name=='isolation':entry['passed'] &= all(execution['output'][k] for k in ('root_write_denied','host_file_unavailable','network_denied','sensitive_env_absent'))
        except SandboxError as exc:entry.update(error=exc.code,passed=exc.code==expected)
        results.append(entry);print(name,entry['passed'],flush=True)
    event=Event();timer=Timer(1,event.set);timer.start()
    try:
        execute(ROOT/'.data',{},'def adapt(b):\n while True: pass',cancel=event,timeout=15)
        results.append(dict(probe='cancel',passed=False))
    except SandboxError as exc:results.append(dict(probe='cancel',error=exc.code,passed=exc.code=='SANDBOX_CANCELLED'))
    finally:timer.cancel()
    remaining=docker(['ps','-aq','--filter','name=cad-table-']).decode().strip()
    results.append(dict(probe='container_cleanup',passed=not remaining))
    (out/'sandbox-probes.json').write_text(json.dumps(results,indent=2))
    assert all(r['passed'] for r in results)


if __name__=='__main__':main()
