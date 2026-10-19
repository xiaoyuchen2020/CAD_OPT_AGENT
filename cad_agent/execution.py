"""Single-host bounded execution, fenced leases and inspectable tool checkpoints.

Expired work is interrupted, never automatically replayed: a model/network/tool
side effect may have happened before its completion receipt reached SQLite.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
import threading
import time
from uuid import uuid4

from cad_agent.adapters.store import now

ACTIVE = {'queued', 'running'}

class ExecutionCancelled(RuntimeError):
    pass

class BoundedExecutor:
    def __init__(self, max_workers=2, capacity=4, thread_name_prefix='cad'):
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.slots = threading.BoundedSemaphore(capacity)
        self.capacity = capacity
        self.pending = 0
        self.lock = threading.Lock()

    def submit(self, fn, *args, **kwargs):
        if not self.slots.acquire(blocking=False):
            raise ValueError('任务队列已满，请等待完成后重试。')
        with self.lock: self.pending += 1
        try:
            future = self.pool.submit(fn, *args, **kwargs)
        except BaseException:
            self._done(None)
            raise
        future.add_done_callback(self._done)
        return future

    def _done(self, _):
        with self.lock: self.pending -= 1
        self.slots.release()

    def shutdown(self, **kwargs):
        return self.pool.shutdown(**kwargs)

class ExecutionManager:
    def __init__(self, store, lease_seconds=30):
        self.store = store
        self.owner = uuid4().hex
        self.lease_seconds = lease_seconds
        self.stop = threading.Event()
        self.recover()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True, name='cad-leases')
        self.thread.start()

    @contextmanager
    def transaction(self):
        with self.store.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            yield c

    @staticmethod
    def read(c, kind, key):
        row = c.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind,key)).fetchone()
        if row is None: raise KeyError(key)
        return json.loads(row[0])

    @staticmethod
    def write(c, kind, key, value):
        c.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                  (key,kind,json.dumps(value,ensure_ascii=False),value.get('created_at',now())))

    def claim(self, kind, record, task=None):
        with self.transaction() as c:
            if task is not None:
                row=c.execute('SELECT data FROM records WHERE kind=? AND id=?',('agent_task',task['task_id'])).fetchone()
                if row:
                    old=json.loads(row[0])
                    if old.get('execution_token') and old.get('execution_lease_until',0)>time.time():
                        raise ValueError('任务已经在运行。')
                    if old.get('revision',0)!=task.get('revision',0):
                        raise ValueError('任务已发生变化，请刷新后继续。')
            if kind=='job':
                for row in c.execute("SELECT data FROM records WHERE kind='job' AND json_extract(data,'$.cache_id')=? AND json_extract(data,'$.status') IN ('queued','running','completed','partial')", (record.get('cache_id'),)):
                    old=json.loads(row[0])
                    if old.get('cache_id')==record.get('cache_id') and old.get('status') in ACTIVE|{'completed','partial'}:
                        return old
            lease=dict(execution_owner=self.owner,execution_token=uuid4().hex,execution_lease_until=time.time()+self.lease_seconds)
            record.update(lease)
            self.write(c,kind,record['id'],record)
            if task is not None:
                task.update(lease,status='running')
                task['run_ids'].append(record['id'])
                task['revision']=task.get('revision',0)+1
                self.write(c,'agent_task',task['task_id'],task)
        return record

    def update(self, kind, key, token, fields):
        with self.transaction() as c:
            value=self.read(c,kind,key)
            if value.get('execution_token')!=token or value.get('execution_lease_until',0)<time.time():
                raise ExecutionCancelled('任务执行租约已失效。')
            if value.get('cancel_requested') and fields.get('status') not in (None,'cancelled','failed'):
                raise ExecutionCancelled('任务已取消。')
            if value.get('cancel_requested') and fields.get('status')=='failed':
                fields=dict(fields,status='cancelled',error={'code':'CANCELLED','message':'任务已取消；已保存证据保持可用。'})
            value.update(fields)
            self.write(c,kind,key,value)
            return value

    def cancel(self, run_id, kind="chat_run"):
        with self.transaction() as c:
            value=self.read(c,kind,run_id)
            if value['status'] in ACTIVE:
                value.update(cancel_requested=True,stage='正在取消，等待当前操作检查点')
                self.write(c,kind,run_id,value)
            return value

    def recover(self):
        with self.transaction() as c:
            rows=c.execute("SELECT kind,id,data FROM records WHERE kind IN ('job','chat_run','agent_task') AND json_extract(data,'$.status') IN ('queued','running') AND (json_extract(data,'$.execution_lease_until') IS NULL OR json_extract(data,'$.execution_lease_until')<=?) UNION SELECT kind,id,data FROM records WHERE kind='agent_task' AND json_extract(data,'$.execution_token')>'' AND json_extract(data,'$.execution_lease_until')<=?", (time.time(),time.time())).fetchall()
            for kind,key,raw in rows:
                value=json.loads(raw)
                if value.get('execution_lease_until',0)>time.time(): continue
                if value.get('status') not in ACTIVE and not (kind=='agent_task' and value.get('execution_token')): continue
                value.update(status='partial' if kind=='agent_task' else 'failed',execution_token=None,
                             error={'code':'INTERRUPTED','message':'执行租约已过期；已保存证据可显式继续，不自动重放操作。'})
                if kind=='agent_task':
                    value['revision']=value.get('revision',0)+1
                    for step in value.get('subtasks',[]):
                        if step['status']=='running': step['status']='pending'
                self.write(c,kind,key,value)

    def _heartbeat(self):
        while not self.stop.wait(max(.05,self.lease_seconds/3)):
            try:
                with self.transaction() as c:
                    for kind,key,raw in c.execute("SELECT kind,id,data FROM records WHERE kind IN ('job','chat_run','agent_task') AND json_extract(data,'$.execution_owner')=? AND json_extract(data,'$.execution_token') IS NOT NULL", (self.owner,)).fetchall():
                        value=json.loads(raw)
                        if value.get('execution_owner')==self.owner and value.get('execution_token') and value.get('execution_lease_until',0)>time.time():
                            value['execution_lease_until']=time.time()+self.lease_seconds
                            self.write(c,kind,key,value)
                self.recover()
            except Exception:
                # Failed renewal expires naturally; workers are fenced at their next checkpoint.
                continue

    def finish(self, kind, key, token, task=None):
        with self.transaction() as c:
            for k,i in [(kind,key)]+([('agent_task',task['task_id'])] if task else []):
                value=self.read(c,k,i)
                if value.get('execution_token')==token and value.get('execution_lease_until',0)>time.time():
                    value.update(execution_token=None,execution_lease_until=0)
                    self.write(c,k,i,value)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)

class ExecutionContext:
    def __init__(self, manager, kind, key, token):
        self.manager,self.kind,self.key,self.token=manager,kind,key,token

    def check_cancelled(self):
        value=self.manager.store.get(self.kind,self.key)
        if value.get('cancel_requested') or value.get('execution_token')!=self.token or value.get('execution_lease_until',0)<time.time():
            raise ExecutionCancelled('任务取消或执行租约已失效。')

    def before_tool(self, name, args):
        self.check_cancelled()
        checkpoint=uuid4().hex
        self.manager.update(self.kind,self.key,self.token,{'tool_checkpoint':dict(id=checkpoint,tool=name,
            input_sha256=sha256(json.dumps(args,sort_keys=True,default=str).encode()).hexdigest(),status='started',at=now())})
        self.check_cancelled()
        return checkpoint

    def after_tool(self, checkpoint_id, result=None):
        # Record completion even if cancellation arrived during the tool. Do not replay it.
        with self.manager.transaction() as c:
            value=self.manager.read(c,self.kind,self.key)
            if value.get('execution_token')!=self.token or value.get('execution_lease_until',0)<time.time(): raise ExecutionCancelled('执行租约已失效。')
            checkpoint=value.get('tool_checkpoint',{})
            if checkpoint.get('id')==checkpoint_id:
                checkpoint.update(status='completed',completed_at=now())
                self.manager.write(c,self.kind,self.key,value)
        self.check_cancelled()
