"""Independent, GET-only source browser; existing centre sessions grant access."""
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
import json
import os
import re
import sqlite3
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, HTMLResponse

from .source_overview import SourceRepository, build_overview, readonly
from .source_overview_support import node_ids
from .source_overview_viewer import viewer_script
from .proxy import upstream_url, allowed_path

STATIC=Path(__file__).with_name('static')
WEB=Path(__file__).resolve().parents[2]/'web'
ASSETS={'app.js','source-overlay.mjs','highlight.mjs','workspace.css'}


class LiveReader:
    """Incremental, bounded readback with task/attempt scoped caches.

    At most eight requests per run per refresh. Large records continue at their
    exact character cursor; no partial JSON is promoted to an extracted source.
    """
    def __init__(self, getter=None):
        self.states=OrderedDict();self.getter=getter

    def get(self,node,path,params=None):
        if self.getter:return self.getter(node,path,params)
        with httpx.Client(timeout=httpx.Timeout(2,read=2),trust_env=False) as client:
            with client.stream('GET',upstream_url(node)+path,params=params) as response:
                response.raise_for_status();parts=[];size=0
                for part in response.iter_bytes():
                    size+=len(part)
                    if size>12*1024*1024:raise ValueError('实时响应超过只读展示上限')
                    parts.append(part)
                return json.loads(b''.join(parts))

    def __call__(self,job,attempt):
        key=(job['id'],attempt['id'],job.get('run_id'),job['node_id'])
        state=self.states.setdefault(key,dict(records={},partial=None,drawings={},updated=0,rejected={}))
        self.states.move_to_end(key)
        while len(self.states)>8:self.states.popitem(last=False)
        if time.monotonic()-state['updated']<10 and state.get('status'):
            return state.get('archive'),state['status']
        state['updated']=time.monotonic();status=dict(job_id=job['id'],attempt_id=attempt['id'],complete=False)
        if not job.get('run_id'):
            status['message']='运行中任务尚未发布模型运行编号，等待下一次刷新';return None,status
        node=job['node_id'];calls=0;deadline=time.monotonic()+5
        def get(path,params=None):
            nonlocal calls
            if calls>=8 or time.monotonic()>deadline:raise TimeoutError('实时读取分批进行，下次刷新继续')
            calls+=1;return self.get(node,path,params)
        try:
            run=get('/api/chat-runs/'+quote(job['run_id'],safe=''))
            if run.get('id')!=job['run_id']:raise ValueError('模型运行身份不一致')
            tid=run.get('task_id')
            if not tid:raise ValueError('运行尚无任务证据账本')
            task=get('/api/tasks/'+quote(tid,safe=''))
            if task.get('task_id')!=tid:raise ValueError('任务证据账本身份不一致')
            refs=list(task.get('evidence_records',{}).values())
            allowed={task.get('snapshot_id'),*(d['snapshot_id'] for d in task.get('related_drawings',[]))}-{None}
            for sid in allowed:
                if sid not in state['drawings']:
                    d=get('/api/drawings/'+quote(sid,safe=''))
                    if d.get('id')!=sid or not d.get('sha256'):raise ValueError('实时图纸身份缺失')
                    state['drawings'][sid]=d
            wanted={r['record_id']:r for r in refs}
            state['rejected']={rid:h for rid,h in state['rejected'].items() if rid in wanted and wanted[rid]['sha256']==h}
            # A receipt removed or changed by the owner is not kept as a current source.
            state['records']={rid:r for rid,r in state['records'].items() if rid in wanted and wanted[rid]['sha256']==r['ref']['sha256']}
            if state['partial'] and state['partial']['ref']!=wanted.get(state['partial']['ref']['record_id']):state['partial']=None
            for ref in refs:
                rid=ref['record_id']
                if rid in state['records'] or rid in state['rejected']:continue
                partial=state['partial']
                if not partial:partial=dict(ref=ref,content='',sources=None);state['partial']=partial
                if partial['ref']['record_id']!=rid:continue
                while True:
                    start=len(partial['content'])
                    page=get('/api/tasks/'+quote(tid,safe='')+'/evidence-records',dict(record_id=rid,start=start,length=12000))
                    if page.get('record_id')!=rid or page.get('sha256')!=ref['sha256'] or page.get('start')!=start:raise ValueError('实时证据分页身份不一致')
                    if partial['sources'] is not None and partial['sources']!=page['sources']:raise ValueError('实时证据来源分页不一致')
                    if not set(page['sources'])<=allowed:raise ValueError('实时证据超出任务范围')
                    partial['sources']=page['sources'];partial['content']+=page['content']
                    if len(partial['content'])>16*1024*1024:raise ValueError('单条记录超出实时展示上限')
                    if page['has_more']:
                        if page['next_start']!=len(partial['content']) or not page['content']:raise ValueError('实时证据分页未前进')
                        continue
                    if sha256(partial['content'].encode()).hexdigest()!=ref['sha256']:raise ValueError('实时证据哈希不一致')
                    state['records'][rid]=partial;state['partial']=None;break
            status.update(complete=not state['rejected'])
            if state['rejected']:status['message']=f"{len(state['rejected'])} 条实时记录未通过校验或超过读取上限，保留缺口"
        except (httpx.HTTPError,ValueError,KeyError,TimeoutError) as exc:
            status['message']='实时来源尚未同步完整：'+str(exc)[:130]
            if not isinstance(exc,(TimeoutError,httpx.HTTPError)):
                if state['partial']:
                    ref=state['partial']['ref'];state['rejected'][ref['record_id']]=ref['sha256']
                state['partial']=None
        status.update(records_loaded=len(state['records']),records_total=len(locals().get('refs',[])),records_rejected=len(state['rejected']),requests=calls)
        if state['drawings']:
            state['archive']=dict(job_id=job['id'],attempt_id=attempt['id'],node_id=node,result={},artifacts=dict(
                drawings=list(state['drawings'].values()),evidence_records=list(state['records'].values())))
        state['status']=status
        return state.get('archive'),status


def create_app(root=None, live_reader=None):
    root=Path(root or os.environ.get('CAD_FLEET_DATA_DIR','.fleet'))
    repo=SourceRepository(root,os.environ.get('CAD_SOURCE_OVERVIEW_CACHE'));live=live_reader or LiveReader()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.source_repo=repo;app.state.live_reader=live
    lock=threading.Lock();cache=OrderedDict()

    async def authenticated(request):
        token=request.cookies.get('cad_fleet_session')
        if not token:return False
        if os.environ.get('CAD_SOURCE_OVERVIEW_CACHE'):
            # Reuse the centre's existing session-only endpoint. It does not
            # enumerate jobs, expire leases or expose the internal worker token.
            try:
                async with httpx.AsyncClient(timeout=2,trust_env=False) as client:
                    r=await client.get('http://127.0.0.1:8780/api/fleet/session',cookies={'cad_fleet_session':token})
                    r.raise_for_status();return r.json().get('authenticated') is True
            except (httpx.HTTPError,ValueError):raise HTTPException(503,'任务中心登录状态暂不可读') from None
        try:
            with readonly(root/'fleet.sqlite3') as db:
                return bool(db.execute('SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?',(sha256(token.encode()).hexdigest(),time.time())).fetchone())
        except (OSError,sqlite3.Error):raise HTTPException(503,'来源数据库暂不可读，请稍后重试') from None

    @app.middleware('http')
    async def access(request,call_next):
        if request.method not in ('GET','HEAD'):return JSONResponse({'detail':'来源总览仅提供读取'},405)
        try:authorized=request.url.path=='/source-overview/health' or await authenticated(request)
        except HTTPException as exc:return JSONResponse({'detail':exc.detail},exc.status_code)
        if not authorized:
            if request.url.path=='/source-overview/':
                return FileResponse(STATIC/'source-overview-login.html',headers={'Cache-Control':'no-store'})
            return JSONResponse({'detail':'请先登录任务中心'},401)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'"
        return response

    @app.get('/source-overview/health')
    def health():
        try:
            if not (root/'fleet.sqlite3').exists():raise OSError('missing database')
            repo.rows('fleet.sqlite3','SELECT 1 FROM sessions LIMIT 1')
        except (OSError,sqlite3.Error):raise HTTPException(503,'来源数据库暂不可读') from None
        return dict(status='ok',read_only=True)

    @app.get('/source-overview/')
    def page():
        html=(STATIC/'source-overview.html').read_text()
        if not (STATIC/'results.html').exists():
            html=html.replace('id="results-link"','id="results-link" hidden')
        return HTMLResponse(html)

    @app.get('/source-overview/assets/{name}')
    def asset(name:str):
        if name not in ('source-overview.js','source-overview.css','source-overview-model.mjs'):raise HTTPException(404)
        return FileResponse(STATIC/name,media_type='text/css' if name.endswith('.css') else 'application/javascript')

    @app.get('/source-overview/api/jobs')
    def jobs():return dict(items=repo.jobs())

    @app.get('/source-overview/api/jobs/{job_id}')
    def overview(job_id:str,attempt_id:str|None=None,scope:str='task',history:bool=False):
        if scope not in ('task','project'):raise HTTPException(422,'范围无效')
        key=(job_id,attempt_id,scope,history)
        # Coalesce concurrent tabs, cap background work, retain last successful view.
        if not lock.acquire(timeout=.1):
            if key in cache:return dict(cache[key][1],stale=True)
            raise HTTPException(503,'来源正在同步，请稍后刷新')
        try:
            if key in cache and time.monotonic()-cache[key][0]<10:return cache[key][1]
            result=build_overview(repo,job_id,attempt_id=attempt_id,project_scope=scope=='project',history=history,live_reader=live)
            cache[key]=(time.monotonic(),result)
            while len(cache)>8:cache.popitem(last=False)
            return result
        except KeyError as exc:raise HTTPException(404,str(exc)) from None
        except sqlite3.Error:raise HTTPException(503,'数据库正在更新，请稍后刷新；未修改任何任务') from None
        finally:lock.release()

    # The only proxied APIs are GETs used by the viewer. No task/model/control API.
    @app.api_route('/source-overview/nodes/{node}/{path:path}',methods=['GET','HEAD'])
    async def viewer(node:str,path:str,request:Request):
        if node not in node_ids() or not allowed_path(path):raise HTTPException(404)
        if not (path=='' or path.startswith('assets/') or path in ('api/health','api/models','api/runtimes','api/drawings')
                or re.fullmatch(r'api/drawings/[^/]+(?:/(?:scene|preview(?:/[^/]+/[^/]+)?|sources(?:/[^/]+/preview\.svg)?|layouts|summary))?',path)):
            raise HTTPException(404,'只读查看器不开放此接口')
        prefix='/source-overview/nodes/'+node
        name=path.removeprefix('assets/')
        if path.startswith('assets/') and name in ASSETS:
            try:content=(viewer_script((WEB/name).read_text()).encode() if name=='app.js' else (WEB/name).read_bytes())
            except ValueError:raise HTTPException(503,'查看器接口已变化，请更新来源总览适配器') from None
            ct='text/css' if name.endswith('.css') else 'application/javascript';status=200
        else:
            try:
                async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
                    response=await client.get(upstream_url(node)+'/'+path,params=request.query_params)
                content=response.content;ct=response.headers.get('content-type','application/octet-stream');status=response.status_code
            except httpx.HTTPError:raise HTTPException(502,'原图节点暂时不可用') from None
        if 'text/html' in ct:
            content=re.sub(rb'((?:src|href|action)\s*=\s*["\x27])(/(?:api|assets)/)',lambda m:m[1]+prefix.encode()+m[2],content,flags=re.I)
        elif 'javascript' in ct or 'text/css' in ct:
            content=content.replace(b'/api/',(prefix+'/api/').encode()).replace(b'/assets/',(prefix+'/assets/').encode())
        elif 'application/json' in ct:
            from .proxy import rewrite
            try:content=json.dumps(rewrite(json.loads(content),prefix),ensure_ascii=False).encode()
            except ValueError:pass
        return Response(b'' if request.method=='HEAD' else content,status_code=status,media_type=ct)
    return app
