#!/usr/bin/python3
"""Root-owned sudo gate. Only fixed sandbox operations; never arbitrary Docker access."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PREFIX=['--pull=never','--network=none','--read-only','--cap-drop=ALL',
 '--security-opt=no-new-privileges:true','--pids-limit=32','--memory=512m',
 '--memory-swap=512m','--cpus=1','--user=65534:65534','--ipc=none','--log-driver=none',
 '--ulimit=cpu=20:20','--ulimit=fsize=2097152:2097152','--ulimit=nofile=64:64',
 '--tmpfs=/work:rw,noexec,nosuid,size=64m,mode=1777','-i']

def validate(args, images):
    if args==['info','--format','{{json .OSType}}']: return ('info',None)
    if len(args)==3 and args[:2]==['image','inspect'] and args[2] in images: return ('image',None)
    if len(args)==len(PREFIX)+4 and args[:2]==['run','--name'] and args[3:-1]==PREFIX and args[-1] in images:
        action,name='run',args[2]
    elif len(args)==2 and args[0]=='inspect': action,name='inspect',args[1]
    elif len(args)==3 and args[:2]==['rm','-f']: action,name='rm',args[2]
    else: raise ValueError('operation denied')
    if not re.fullmatch(r'cad-table-[0-9a-f]{32}',name): raise ValueError('invalid sandbox name')
    return action,name

def main():
    uid=int(os.environ['SUDO_UID'])
    images=json.loads(Path('/etc/cad-atlas/sandbox-images.json').read_text())
    args=sys.argv[1:];action,name=validate(args,images)
    root=Path('/run/cad-atlas-sandboxes');root.mkdir(mode=0o700,exist_ok=True)
    owner=root/name if name else None
    if action=='run':
        with owner.open('x') as f: f.write(str(uid))
    elif owner and (not owner.is_file() or owner.read_text()!=str(uid)):
        raise ValueError('sandbox owner mismatch')
    try:
        r=subprocess.run(['/usr/bin/docker',*args],env={'PATH':'/usr/bin:/bin','HOME':'/root'},timeout=65)
        return r.returncode
    except subprocess.TimeoutExpired:
        if name: subprocess.run(['/usr/bin/docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return 124
    finally:
        if action=='rm':owner.unlink(missing_ok=True)

if __name__=='__main__':
    try: sys.exit(main())
    except Exception: print('Sandbox operation denied',file=sys.stderr);sys.exit(1)
