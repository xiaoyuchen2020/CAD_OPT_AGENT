"""Root-only sidecar deployment on the LAN host; never restarts calculation units.

Stage: --bundle patch.json --release /opt/cad-atlas/source-overview/releases/NAME
Activate: same arguments plus --activate. Activation requires staged preflight.
Uses a transient unit first and installs an identical persistent unit without a
manager-wide daemon-reload. Nginx reload is graceful and guarded by its file hash.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def cmd(*args):return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=40).strip()
def services():
    result={}
    for unit in ['cad-atlas-center']+['cad-atlas-'+kind+'-'+n for kind in ('agent','worker') for n in 'abcd']:
        result[unit]=dict(line.split('=',1) for line in cmd('systemctl','show',unit,'--property=MainPID,ActiveState,NRestarts').splitlines())
    return result

def atomic(path,data,mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.source-overview-'+str(os.getpid()))
    temp.write_text(data);temp.chmod(mode);os.replace(temp,path)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--release',type=Path,required=True);parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('root required for staging/activation')
    base=Path('/opt/cad-atlas/current').resolve();release=args.release.resolve();root=Path('/var/lib/cad-atlas/cad-center')
    if not release.is_relative_to(Path('/opt/cad-atlas/source-overview/releases')):raise ValueError('release outside sidecar tree')
    config=Path('/etc/nginx/sites-available/cad-atlas');snippet=Path('/etc/nginx/snippets/cad-source-overview.conf');unit=Path('/etc/systemd/system/cad-atlas-source-overview.service')
    if not args.activate:
        if release.exists():raise ValueError('release already exists')
        payload=json.loads(args.bundle.read_text())
        allowed={'cad_agent/fleet/source_overview.py','cad_agent/fleet/source_overview_service.py','cad_agent/fleet/source_overview_support.py','cad_agent/fleet/source_overview_viewer.py','web/source-overlay.mjs',
            *['cad_agent/fleet/static/source-overview'+s for s in ('.html','.js','.css','-model.mjs','-login.html')]}
        if set(payload)!=allowed:raise ValueError('unexpected deployment payload')
        release.mkdir(parents=True)
        for name in ('cad_agent','web'):shutil.copytree(base/name,release/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for name,value in payload.items():atomic(release/name,value)
        # Validate the isolated served adaptation, without changing web/app.js.
        sys.path.insert(0,str(release))
        from cad_agent.fleet.source_overview_viewer import viewer_script
        viewer_script((release/'web/app.js').read_text())
        manifest=dict(base=str(base),nginx_sha256=sha(config),files={p:sha(release/p) for p in sorted(allowed|{'web/app.js'})},services_at_stage=services())
        atomic(release/'manifest.json',json.dumps(manifest,indent=2))
        sys.path.insert(0,str(release))
        from cad_agent.fleet.source_overview import SourceRepository,build_overview
        from cad_agent.fleet.source_overview_service import create_app
        from fastapi.testclient import TestClient
        os.environ['CAD_SOURCE_OVERVIEW_CACHE']=str(release/'preflight-cache')
        repo=SourceRepository(root,release/'preflight-cache');checks=[];archives={}
        for job in repo.jobs():
            aa=[a for a in repo.attempts(job['id']) if a.get('archive_path')]
            if not aa:continue
            a=aa[-1];path=root/a['archive_path'];archives[str(path)]=sha(path)
            view=build_overview(repo,job['id'],attempt_id=a['id'])
            checks.append(dict(job_id=job['id'],counts=view['counts'],warnings=len(view['warnings']),execution_status=view['execution_status']))
        assert checks and any(x['counts']['locatable'] for x in checks)
        app=create_app(root)
        with TestClient(app) as c:
            assert c.get('/source-overview/health').json()['read_only']
            assert c.get('/source-overview/api/jobs').status_code==401
            assert c.post('/source-overview/api/jobs').status_code==405
        assert all(sha(Path(p))==h for p,h in archives.items())
        proof=dict(passed=True,checks=checks,archives_unchanged=True,models_called=0,services_unchanged=services()==manifest['services_at_stage'])
        atomic(release/'preflight.json',json.dumps(proof,indent=2));print(json.dumps(dict(release=str(release),**proof)));return
    manifest=json.loads((release/'manifest.json').read_text());assert json.loads((release/'preflight.json').read_text())['passed']
    assert str(base)==manifest['base'],'base release changed; restage against the new release'
    assert all(sha(release/p)==h for p,h in manifest['files'].items()),'staged files changed'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def health():
        with opener.open('http://127.0.0.1:8794/source-overview/health',timeout=2) as r:return json.load(r)
    with Path('/run/lock/cad-atlas-maintenance.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert not Path('/run/cad-atlas-maintenance').exists(),'other deployment in progress'
        assert sha(config)==manifest['nginx_sha256'],'nginx changed since staging'
        assert not snippet.exists() and not unit.exists(),'sidecar already installed'
        assert subprocess.run(['systemctl','is-active','--quiet','cad-atlas-source-overview']).returncode!=0
        before=services();original=config.read_text()
        assert all(p['ActiveState']=='active' for p in before.values())
        assert original.count(' location / {')==1
        include=' include /etc/nginx/snippets/cad-source-overview.conf;\n'
        patched=original.replace(' location / {',include+' location / {')
        locations='''location = /source-overview { return 308 /source-overview/$is_args$args; }
location ^~ /source-overview/ {
 proxy_pass http://127.0.0.1:8794;
 proxy_set_header Host $http_host;
 proxy_read_timeout 45s;
 proxy_buffering off;
}
'''
        # Add navigation at the gateway; original centre files remain untouched.
        for path in ('/','/index.html','/results.html'):
            locations+='''location = PATH {
 proxy_pass http://127.0.0.1:8780;
 proxy_set_header Host $http_host;
 proxy_set_header X-Forwarded-For $remote_addr;
 proxy_set_header X-Forwarded-Proto $scheme;
 proxy_set_header Accept-Encoding "";
 proxy_read_timeout 700s;
 proxy_send_timeout 700s;
 proxy_buffering off;
 sub_filter_once on;
 sub_filter '<a href="/results.html">' '<a href="/source-overview/">数据来源总览</a><a href="/results.html">';
 sub_filter '<nav>' '<nav><a href="/source-overview/">数据来源总览</a>';
}
'''.replace('PATH',path)
        props={'User':'cad-center','Group':'cad-center','SupplementaryGroups':'cad-fleet','WorkingDirectory':str(release),
            'EnvironmentFile':'/etc/cad-atlas/center.env','Environment':'PYTHONDONTWRITEBYTECODE=1 CAD_SOURCE_OVERVIEW_CACHE=/run/cad-source-overview','NoNewPrivileges':'yes',
            'ProtectSystem':'strict','ProtectHome':'yes','PrivateTmp':'yes','ReadOnlyPaths':'/var/lib/cad-atlas',
            'RuntimeDirectory':'cad-source-overview','RuntimeDirectoryMode':'0700','ReadWritePaths':'/run/cad-source-overview',
            'MemoryMax':'512M','MemorySwapMax':'0','CPUQuota':'40%','TasksMax':'48','Restart':'on-failure','RestartSec':'5','TimeoutStopSec':'10'}
        executable=['/opt/cad-atlas/venv/bin/uvicorn','cad_agent.fleet.source_overview_service:create_app','--factory','--host','127.0.0.1','--port','8794','--no-access-log']
        service='[Unit]\nDescription=CAD Atlas read-only source overview\nAfter=network-online.target\n[Service]\n'+''.join(k+'='+v+'\n' for k,v in props.items())+'ExecStart='+' '.join(executable)+'\n[Install]\nWantedBy=multi-user.target\n'
        enabled=Path('/etc/systemd/system/multi-user.target.wants/cad-atlas-source-overview.service')
        assert not enabled.exists()
        atomic(release/'nginx.before.conf',original)
        atomic(release/'activation-before.json',json.dumps(dict(services=before,current=str(base)),indent=2))
        started=False;changed=False
        try:
            cmd('systemd-run','--unit=cad-atlas-source-overview',*['--property='+k+'='+v for k,v in props.items()],'--',*executable);started=True
            for i in range(30):
                try:
                    assert health()['read_only'];break
                except Exception:
                    if i==29:raise
                    time.sleep(.3)
            atomic(unit,service);enabled.symlink_to(unit)
            atomic(snippet,locations)
            assert sha(config)==manifest['nginx_sha256'] and Path('/opt/cad-atlas/current').resolve()==base
            atomic(config,patched);changed=True
            cmd('nginx','-t');cmd('systemctl','reload','nginx')
            after=services();assert after==before,'existing service process changed during activation'
            assert Path('/opt/cad-atlas/current').resolve()==base
            with opener.open('http://192.168.1.146:8080/source-overview/health',timeout=3) as r:assert json.load(r)['read_only']
            with opener.open('http://192.168.1.146:8080/',timeout=3) as r:assert '数据来源总览' in r.read().decode()
            proof=dict(passed=True,release=str(release),url='http://192.168.1.146:8080/source-overview/',existing_services_unchanged=True,
                current_release_unchanged=True,services=after,nginx_sha256=sha(config),unit_sha256=sha(unit),model_calls=0,manager_reloaded=False)
            atomic(release/'activation.json',json.dumps(proof,indent=2));print(json.dumps(proof))
        except BaseException:
            # Never overwrite a concurrent operator's nginx changes on rollback.
            if changed and config.read_text()==patched:
                atomic(config,original);cmd('nginx','-t');cmd('systemctl','reload','nginx')
            if started:cmd('systemctl','stop','cad-atlas-source-overview')
            if enabled.is_symlink() and enabled.resolve()==unit:enabled.unlink()
            if unit.exists() and unit.read_text()==service:unit.unlink()
            if snippet.exists() and snippet.read_text()==locations:snippet.unlink()
            raise

if __name__=='__main__':main()
