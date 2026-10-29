"""One durable fleet consumer and one local CAD Atlas instance per process."""
import contextlib
import fcntl
from hashlib import sha256
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
import httpx
from cad_agent.config import Settings
from .conversion import cached_conversion

LOG = logging.getLogger('cad-fleet-worker')


class Cancelled(Exception):
    pass


class LostLease(Exception):
    pass


class Worker:
    def __init__(self):
        self.node_id = os.environ['CAD_FLEET_NODE_ID']
        self.root = Path(os.environ['CAD_FLEET_WORKER_DIR'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = Path(os.environ['CAD_FLEET_CACHE_DIR'])
        self.cache.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root/'active.json'
        self.settings = Settings.load()
        headers = {'Authorization': 'Bearer '+os.environ['CAD_FLEET_TOKEN']}
        self.center = httpx.Client(base_url=os.environ['CAD_FLEET_URL'], headers=headers,
                                   timeout=30, trust_env=False, follow_redirects=False)
        self.agent = httpx.Client(base_url=os.environ['CAD_FLEET_AGENT_URL'], headers=headers,
                                  timeout=900, trust_env=False, follow_redirects=False)
        self.stop = threading.Event()
        self.cancelled = threading.Event()
        self.lost = threading.Event()
        self.state = {}
        self.control_lock = threading.Lock()

    def save(self):
        target = self.state_path.with_suffix('.tmp')
        target.write_text(json.dumps(self.state, ensure_ascii=False))
        os.replace(target, self.state_path)

    def fields(self):
        return {k: self.state[k] for k in ('node_id','job_id','attempt_id')}

    def request(self, endpoint, **fields):
        response = self.center.post('/api/fleet/worker/'+endpoint, json={**self.fields(), **fields})
        if response.status_code == 409:
            self.lost.set()
            raise LostLease()
        response.raise_for_status()
        return response.json()

    def check(self):
        if self.lost.is_set():
            raise LostLease()
        if self.cancelled.is_set():
            raise Cancelled()

    def heartbeat(self):
        try:
            value = self.request('heartbeat', progress=self.state.get('progress','执行中'),
                                 snapshot_id=self.state.get('snapshot_id'), run_id=self.state.get('run_id'),
                                 model_profile=os.environ.get('CAD_FLEET_MODEL_PROFILE','api_sol'))
            self.pause_before_agent = value.get('control',{}).get('desired_state')=='paused'
            if value.get('cancel_requested'):
                self.cancelled.set()
        except LostLease:
            self.lost.set()
        except httpx.HTTPError:
            # Never continue inference indefinitely after central connectivity is lost.
            if time.monotonic()-self.last_heartbeat > self.lease_seconds:
                self.lost.set()
        else:
            self.last_heartbeat = time.monotonic()
            # Keep lease renewal independent of slower Agent/control telemetry.
            # The execution polling loop performs control synchronization.

    def heartbeat_loop(self):
        while not self.stop.wait(min(10, self.lease_seconds/3)):
            self.heartbeat()
            if self.cancelled.is_set() or self.lost.is_set():
                self.cancel_agent()

    def sync_operations(self, run=None):
        """Forward durable commands only under the current Fleet lease."""
        if not self.state.get('run_id') or not self.control_lock.acquire(blocking=False):return
        try:
            pending=self.request('operations/poll')
            self.check()
            run_id=self.state['run_id']
            response=self.agent.get('/api/chat-runs/'+run_id+'/control',timeout=10)
            if response.status_code==404:return
            response.raise_for_status();control=response.json()
            deadline=time.monotonic()+5
            for command in pending.get('commands',[]):
                if time.monotonic()>deadline:break
                self.check()
                if any(c.get('command_id')==command['command_id'] for c in control.get('commands',[])):continue
                data={k:command[k] for k in ('command_id','action','payload','source')}
                data['expected_revision']=control.get('revision',0)
                response=self.agent.post('/api/chat-runs/'+run_id+'/control',json=data,
                    headers={'X-Fleet-Execution':self.state['attempt_id']},timeout=5)
                if response.status_code==409:
                    # Another controller changed the Agent revision. Do not lose this command;
                    # the next fenced poll retries its idempotency key with a fresh revision.
                    break
                response.raise_for_status();control=response.json()
            if run is None:
                response=self.agent.get('/api/chat-runs/'+run_id,timeout=10)
                response.raise_for_status();run=response.json()
            response=self.agent.get('/api/chat-runs/'+run_id+'/harness-events',
                                    params={'after':self.state.get('operations_cursor',0),'limit':100},timeout=10)
            events=[]
            if response.status_code==200:
                page=response.json();events=page.get('items',[])
                self.state['operations_cursor']=page.get('next_after',self.state.get('operations_cursor',0))
            from .operations import bounded_telemetry
            telemetry=bounded_telemetry(run,control,events)
            telemetry['resources'].update(worker_slot_held=True,model_slot_held=bool(self.state.get('model_slot_held')))
            # Keep a bounded event tail across incremental fetches, including idle polls.
            recent=self.state.get('operations_events',[])+telemetry['events']
            telemetry['events']=recent[-200:]
            self.state['operations_events']=telemetry['events']
            receipts=[{k:c[k] for k in ('command_id','status','applied_at','boundary','error','reason','rejected_at') if k in c}
                      for c in control.get('commands',[]) if any(p['command_id']==c.get('command_id') for p in pending.get('commands',[]))]
            self.request('operations/report',telemetry=telemetry,receipts=receipts)
        except LostLease:
            self.lost.set()
            self.cancel_agent()
        except httpx.HTTPError as exc:
            # Read/control transport problems do not manufacture an applied acknowledgement.
            LOG.warning('Control synchronization pending (%s)',type(exc).__name__)
        finally:
            self.control_lock.release()

    def before_agent_boundary(self):
        """Pause conversion/import scheduling before an Agent exists, without losing its lease."""
        if self.state.get('run_id'):return
        waited=False
        while getattr(self,'pause_before_agent',False):
            self.check()
            if not waited:
                self.report_pre_agent_pause(True)
                waited=True
            time.sleep(.2)
        if waited:self.report_pre_agent_pause(False)

    def report_pre_agent_pause(self,paused):
        pending=self.request('operations/poll')
        # Only acknowledge the latest desired state actually reached at this boundary.
        commands=[c for c in pending.get('commands',[]) if c['action'] in {'pause','resume'}]
        actual='pause' if paused else 'resume'
        receipts=[]
        if commands and commands[-1]['action']==actual:
            receipts=[{'command_id':c['command_id'],'status':'rejected','reason':'superseded_before_agent_boundary','boundary':'before_agent_start'} for c in commands[:-1]]
            receipts+=[{'command_id':commands[-1]['command_id'],'status':'applied','applied_at':datetime.now(timezone.utc).isoformat(),'boundary':'before_agent_start'}]
        from .operations import bounded_telemetry
        telemetry=bounded_telemetry({'id':None,'status':'running','stage':'已暂停，等待继续' if paused else '准备执行'},
                                    {'state':'paused' if paused else 'running','effective':{}})
        self.request('operations/report',telemetry=telemetry,receipts=receipts)

    def cancel_agent(self):
        if self.state.get('run_id'):
            with contextlib.suppress(httpx.HTTPError):
                self.agent.post('/api/chat-runs/'+self.state['run_id']+'/cancel', timeout=10)

    def progress(self, value):
        self.check()
        self.state['progress'] = value
        self.save()

    def import_upload(self, upload):
        self.check()
        response = self.center.get('/api/fleet/uploads/'+upload['id']+'/file', timeout=120)
        response.raise_for_status()
        raw = response.content
        if len(raw) > self.settings.max_upload_bytes or sha256(raw).hexdigest() != upload['sha256']:
            raise ValueError('上传图纸完整性校验失败。')
        filename = upload['filename']
        if filename.lower().endswith('.dwg'):
            self.progress('ODA 转换与校验')
            bundle = cached_conversion(self.settings, filename, raw, self.cache,
                                       os.environ.get('CAD_FLEET_CONVERTER_REV','1'), check=self.check)
            self.check()
            self.progress('导入已转换图纸')
            response = self.agent.post('/api/fleet-import',
                data={'job_id': bundle.metadata['job_id']}, files={
                    'original': (filename, raw), 'dxf': ('source.dxf', bundle.dxf),
                    'oda_json': ('oda.json', bundle.json_bytes)})
        else:
            self.progress('导入 DXF')
            response = self.agent.post('/api/drawings', files={'file': (filename,raw)})
        response.raise_for_status()
        return response.json()

    @contextlib.contextmanager
    def model_slot(self):
        capacity = max(1,min(8,int(os.environ.get('CAD_FLEET_MODEL_CONCURRENCY','2'))))
        held = None
        self.progress('等待模型执行额度')
        try:
            while held is None:
                self.check()
                self.before_agent_boundary()
                for i in range(capacity):
                    file = (self.cache/f'model-{i}.lock').open('a')
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX|fcntl.LOCK_NB)
                        held = file
                        break
                    except BlockingIOError:
                        file.close()
                if held is None:
                    time.sleep(.5)
            self.state['model_slot_held']=True
            yield
        finally:
            self.state['model_slot_held']=False
            if held is not None:
                held.close()

    def archive(self, run):
        result = {'run': run, 'drawings':self.state.get('drawings',[])}
        task_id = run.get('task_id')
        if task_id:
            response = self.agent.get('/api/tasks/'+task_id)
            response.raise_for_status()
            result['task'] = response.json()
            if self.state.get('job', {}).get('exploration_run_id') or any(
                result['task'].get(key) for key in
                ('task_methods', 'task_programs', 'program_results', 'tool_version_routes')
            ):
                # Preserve the rest of the evidence even if code export fails.
                # E0 records this explicit gap and cannot accept the case as complete.
                try:
                    response = self.agent.get('/api/fleet-runs/'+run['id']+'/generated-artifacts',
                        headers={'X-Fleet-Execution': self.state['attempt_id']})
                    response.raise_for_status()
                    bundle = response.json()
                    if not isinstance(bundle, dict) or bundle.get('run_id') != run['id'] or bundle.get('task_id') != task_id:
                        raise ValueError('GENERATED_ARTIFACT_BINDING_MISMATCH')
                    result['generated_artifacts'] = bundle
                except (httpx.HTTPError, ValueError) as exc:
                    result['generated_artifacts'] = {
                        'schema_version': 'generated-artifacts@1', 'run_id': run['id'],
                        'task_id': task_id, 'complete': False, 'items': [],
                        'diagnostics': [{'code': 'GENERATED_ARTIFACT_EXPORT_FAILED',
                            'error_type': type(exc).__name__,
                            'http_status': exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None}],
                    }
            records = []
            offset = 0
            while True:
                self.check()
                response = self.agent.get('/api/tasks/'+task_id+'/evidence-records',params={'start':offset})
                response.raise_for_status()
                catalog = response.json()
                for ref in catalog['items']:
                    pages = []
                    start = 0
                    while True:
                        response = self.agent.get('/api/tasks/'+task_id+'/evidence-records',
                            params={'record_id':ref['record_id'],'start':start,'length':12000})
                        response.raise_for_status()
                        page = response.json(); pages.append(page)
                        if not page['has_more']:
                            break
                        start = page['next_start']
                    text = ''.join(p['content'] for p in pages)
                    if sha256(text.encode()).hexdigest() != ref['sha256']:
                        raise ValueError('证据归档完整性校验失败。')
                    records.append({'ref':ref,'content':text,'sources':pages[0]['sources']})
                if not catalog['has_more']:
                    break
                offset = catalog['next_start']
            result['evidence_records'] = records
        response = self.agent.get('/api/chat-runs/'+run['id']+'/journal-export')
        response.raise_for_status()
        result['journal'] = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        return result

    def execute(self, job):
        from types import SimpleNamespace
        from cad_agent.analysis.method_authorization import authorization
        from .rule_api import check_tool_snapshot
        if job.get('tool_snapshot'):
            check_tool_snapshot(job['tool_snapshot'])
        if job.get('exploration_run_id'):
            from .snapshots import check_model_snapshot
            check_model_snapshot(job['model_snapshot'], self.settings)
        # The rule appendix and model-written task text are never permission sources.
        permission = authorization(SimpleNamespace(
            question=job.get('original_user_input', job.get('question', '')),
            dynamic_methods=job.get('dynamic_methods', 'off')))
        drawings = self.state.get('drawings',[])
        for upload in job['uploads'][len(drawings):]:
            self.before_agent_boundary()
            drawings.append(self.import_upload(upload))
            self.state['drawings'] = drawings
            self.state['snapshot_id'] = drawings[0]['id']
            self.save()
        self.check()
        self.before_agent_boundary()
        if job.get('mode') == 'facts':
            self.progress('本地事实查询（无模型）')
            response = self.agent.post('/api/chat',json={'snapshot_id':drawings[0]['id'],
                'question':job['question'],'mode':'local'})
            response.raise_for_status()
            self.heartbeat()
            self.check()
            self.request('complete',result=response.json(),artifacts={'drawings':drawings},
                         delivery_status='local_query')
            return
        with self.model_slot():
            if not self.state.get('run_id'):
                self.before_agent_boundary()
                self.progress('提交图纸事实分析')
                payload = dict(snapshot_id=drawings[0]['id'],question=job['question'],mode='agent',
                    related_drawings=[{'snapshot_id':d['id']} for d in drawings[1:]],
                    runtime=(job.get('model_snapshot') or {}).get('runtime') or os.environ.get('CAD_FLEET_RUNTIME','native'),
                    model_profile=(job.get('model_snapshot') or {}).get('model_profile') or os.environ.get('CAD_FLEET_MODEL_PROFILE','api_sol'),allow_fallback=False,
                    source_read_batching=False,dynamic_methods=permission['mode'])
                response = self.agent.post('/api/fleet-runs',json=payload,
                    headers={'X-Fleet-Execution':self.state['attempt_id']})
                response.raise_for_status()
                self.state['run_id'] = response.json()['id']
                self.save()
            while True:
                self.check()
                response = self.agent.get('/api/chat-runs/'+self.state['run_id'],timeout=30)
                response.raise_for_status(); run = response.json()
                self.sync_operations(run)
                self.progress(run.get('stage') or '分析中')
                if run['status'] not in ('queued','running','paused'):
                    break
                time.sleep(2)
        self.progress('归档回答与证据')
        archive = self.archive(run)
        (self.root/(self.state['attempt_id']+'.result.json')).write_text(json.dumps(archive,ensure_ascii=False))
        self.heartbeat()
        self.check()
        if run['status'] != 'completed':
            self.request('fail',error=run.get('error') or {'code':'AGENT_'+run['status'].upper()},
                         retryable=False, cancelled=run['status']=='cancelled', artifacts=archive)
        else:
            self.request('complete',result=run.get('result'),artifacts=archive,
                         delivery_status=archive.get('task',{}).get('status','unknown'))

    def run_once(self):
        self.cancelled.clear(); self.lost.clear(); self.stop.clear()
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            job = self.state['job']; self.lease_seconds = self.state.get('lease_seconds',60)
        else:
            response = self.center.post('/api/fleet/worker/claim',json={'node_id':self.node_id,
                'model_profile':os.environ.get('CAD_FLEET_MODEL_PROFILE','api_sol')})
            response.raise_for_status(); value = response.json()
            job = value.get('job')
            if job is None:
                return False
            self.lease_seconds = value.get('lease_seconds',60)
            self.state = dict(node_id=self.node_id,job_id=job.get('job_id',job['id']),
                attempt_id=job['attempt_id'],job=job,lease_seconds=self.lease_seconds)
            self.save()
        self.last_heartbeat = time.monotonic()
        self.heartbeat()
        thread = threading.Thread(target=self.heartbeat_loop,daemon=True);thread.start()
        finished = False
        try:
            self.check(); self.execute(job); finished = True
        except (Cancelled,LostLease) as exc:
            self.cancel_agent()
            if isinstance(exc,Cancelled):
                with contextlib.suppress(LostLease):
                    self.request('fail',error={'code':'CANCELLED'},cancelled=True)
            finished = True
        except httpx.HTTPError as exc:
            # Keep state and same attempt/run IDs for transport retry and restart.
            if isinstance(exc,httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
                self.request('fail',error={'code':'HTTP_'+str(exc.response.status_code)},retryable=False)
                finished = True
            else:
                LOG.warning('Transport interruption; retaining execution checkpoint (%s)',type(exc).__name__)
        except Exception as exc:
            self.cancel_agent()
            LOG.warning('Task failed (%s)',type(exc).__name__)
            self.request('fail',error={'code':getattr(exc,'code',type(exc).__name__)},retryable=False)
            finished = True
        finally:
            self.stop.set();thread.join(12)
            if finished:
                self.state_path.unlink(missing_ok=True)
        return True

    def run(self):
        # A duplicate service cannot consume with the same node identity.
        with (self.root/'worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            while True:
                try:
                    worked = self.run_once()
                    time.sleep(2 if worked else 3)
                except Exception as exc:
                    LOG.warning('Worker loop interrupted (%s)',type(exc).__name__)
                    time.sleep(5)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    Worker().run()
