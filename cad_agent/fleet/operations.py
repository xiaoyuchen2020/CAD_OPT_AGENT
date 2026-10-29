"""Durable operator commands and bounded, evidence-backed live projections.

The queue lease remains authoritative. Control commands are never reported as
applied merely because the browser saved them; worker receipts carry that state.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from datetime import datetime, timezone
import time
from .store import Conflict, dump, iso

SCHEMA = 'fleet-operations@1'
TERMINAL = {'completed', 'failed', 'cancelled'}


def validate_command(action, payload, *, exploration=False):
    if action not in {'pause', 'resume', 'guidance', 'configure'} or not isinstance(payload, dict):
        raise ValueError('未知控制命令')
    if action in {'pause', 'resume'}:
        if payload:
            raise ValueError('暂停/继续命令不接受额外配置')
    elif action == 'guidance':
        if set(payload) != {'message'} or not isinstance(payload['message'], str) or not payload['message'].strip() or len(payload['message']) > 6000:
            raise ValueError('引导内容应为 1–6000 字符')
    else:
        allowed = {'budget', 'sandbox', 'validator'} | ({'dispatch_budget'} if exploration else set())
        if not payload or set(payload) - allowed:
            raise ValueError('不支持此热配置字段')
        ranges = {
            'budget': {'max_rounds': (1,128), 'max_tool_calls': (1,512), 'timeout_seconds': (1,7200), 'max_reported_tokens': (1,10000000)},
            'sandbox': {'timeout_seconds': (1,300), 'memory_mb': (64,512), 'max_output_bytes': (1024,2097152)},
            'validator': {'min_evidence_count': (0,20)},
            'dispatch_budget': {'max_case_starts': (1,10000), 'max_concurrent_cases': (1,100), 'max_reported_tokens': (1,100000000)},
        }
        for section, values in payload.items():
            if not isinstance(values, dict) or not values:
                raise ValueError('配置分组不能为空')
            for key, value in values.items():
                if section == 'validator' and key == 'require_evidence' and isinstance(value, bool):
                    continue
                if key not in ranges[section] or isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError('无效的热配置值: '+section+'.'+key)
                lo, hi = ranges[section][key]
                if not lo <= value <= hi:
                    raise ValueError('热配置超出边界: '+section+'.'+key)
    return deepcopy(payload)


def usage_summary(run):
    values = run.get('usage') or []
    if not isinstance(values, list):
        values = []
    official = [x for x in values if isinstance(x, dict)]
    known = bool(official) and all(isinstance(x.get('total_tokens'), int) and not isinstance(x.get('total_tokens'), bool) for x in official)
    total = sum(max(0, x.get('total_tokens', 0)) for x in official if isinstance(x.get('total_tokens'), int))
    def sum_field(*names):
        vals = [next((x[k] for k in names if isinstance(x.get(k), int)), None) for x in official]
        return sum(vals) if vals and all(v is not None for v in vals) else None
    events = run.get('run_events') or []
    model_calls = sum(e.get('kind') == 'model' for e in events)
    return {'known': known, 'total_tokens': total if known else None, 'reported_tokens': total,
            'input_tokens': sum_field('input_tokens', 'prompt_tokens'), 'output_tokens': sum_field('output_tokens', 'completion_tokens'),
            'model_calls': model_calls or (len(official) if official else None),
            'tool_calls': len(run['trace']) if isinstance(run.get('trace'), list) else None,
            'cost': None, 'cost_note': '未配置可核验价格；不估算金额'}


def bounded_telemetry(run, control=None, events=None):
    control = control or {}
    journal = [{k: e[k] for k in ('id','sequence','kind','title','status','created_at','elapsed_seconds','duration_ms','tool','tool_name') if k in e}
               for e in (run.get('run_events') or [])[-120:] if isinstance(e, dict)]
    traces = [{k: e[k] for k in ('call_id','tool','status','started_at','finished_at','elapsed_ms') if k in e}
              for e in (run.get('trace') or [])[-100:] if isinstance(e, dict)]
    clean_events = []
    for event in (events or [])[-200:]:
        payload = event.get('payload') or {}
        clean_events.append({k: event[k] for k in ('event_id','sequence','kind','created_at','call_id') if k in event} |
                            {'payload': {k: payload[k] for k in ('tool','name','status','ordinal','round','boundary','revision','command_id','source') if k in payload}})
    return {'run_id':run.get('id'), 'status':run.get('status'), 'stage':str(run.get('stage') or '')[:400],
            'usage':usage_summary(run), 'journal':journal, 'trace':traces, 'events':clean_events,
            'control':{k:control[k] for k in ('revision','applied_revision','state','effective','paused_seconds') if k in control}, 'resources':control.get('resources') or {}, 'sampled_at':iso(),
            'metrics': {k:v for k,v in (run.get('metrics') or {}).items() if isinstance(v,(int,float)) and math.isfinite(v)}}


class OperationsStore:
    def __init__(self, fleet, exploration=None):
        self.fleet, self.exploration = fleet, exploration
        with fleet.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS operation_controls(job_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_commands(job_id TEXT NOT NULL,command_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(job_id,command_id));
            CREATE TABLE IF NOT EXISTS operation_attempt_telemetry(job_id TEXT NOT NULL,attempt_id TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(job_id,attempt_id));
            CREATE TABLE IF NOT EXISTS operation_telemetry(job_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL);
            ''')

    @staticmethod
    def _empty():
        return {'revision':0,'state':'running','desired_state':'running','effective':{},'desired':{},'applied_revision':0,'commands':[]}

    def _control(self, db, job_id):
        row = db.execute('SELECT data FROM operation_controls WHERE job_id=?',(job_id,)).fetchone()
        value = json.loads(row[0]) if row else self._empty()
        commands = db.execute('SELECT data FROM operation_commands WHERE job_id=? ORDER BY revision DESC LIMIT 100',(job_id,)).fetchall()
        value['commands'] = [json.loads(x[0]) for x in reversed(commands)]
        return value

    def control(self, job_id):
        with self.fleet.connection() as db:
            job = self.fleet._job(db, job_id, summary=True)
            value = self._control(db, job_id)
            if job['status'] in TERMINAL:
                value['state']='terminal'
            return value

    def _save(self, db, job_id, value):
        data={k:v for k,v in value.items() if k!='commands'}
        db.execute('INSERT INTO operation_controls VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET revision=excluded.revision,data=excluded.data',
                   (job_id,value['revision'],dump(data)))

    def submit(self, kind, ident, command_id, expected_revision, action, payload, source='human'):
        if kind == 'exploration':
            result = self.exploration.control_run(ident, command_id, expected_revision, action, payload, source)
            self.reconcile_exploration(ident)
            return self.exploration_detail(ident)['control']
        if kind != 'job':
            raise ValueError('未知控制对象')
        payload=validate_command(action,payload)
        if source=='supervisor' and action!='guidance':
            raise ValueError('外部模型仅可提交建议引导')
        if not isinstance(command_id,str) or not 1 <= len(command_id) <= 160 or not re.fullmatch(r'[A-Za-z0-9_.:-]+',command_id) or source not in {'human','supervisor'}:
            raise ValueError('无效的命令标识或来源')
        if isinstance(expected_revision,bool) or not isinstance(expected_revision,int) or expected_revision<0:
            raise ValueError('必须提供当前控制版本')
        with self.fleet.connection(True) as db:
            self.fleet._expire(db)
            job = self.fleet._job(db,ident)
            old=db.execute('SELECT data FROM operation_commands WHERE job_id=? AND command_id=?',(ident,command_id)).fetchone()
            if old:
                prior=json.loads(old[0])
                if (prior['action'],prior['payload'],prior['source']) != (action,payload,source):
                    raise Conflict('命令标识已绑定另一操作')
                return self._control(db,ident)
            control=self._control(db,ident)
            if control['revision'] != expected_revision:
                raise Conflict('控制版本已改变，请刷新后重试')
            if job['status'] in TERMINAL or job['status']=='cancel_requested':
                raise Conflict('已结束或取消中的任务不能接受控制')
            revision=control['revision']+1
            command={'command_id':command_id,'revision':revision,'action':action,'payload':payload,'source':source,
                     'status':'pending','requested_at':iso(),'before':deepcopy(control.get('desired',{})),
                     'target_attempt_id':job.get('attempt_id')}
            control['revision']=revision
            if action in {'pause','resume'}:
                control['desired_state']='paused' if action=='pause' else 'running'
                control['state']='pause_requested' if action=='pause' else 'resume_requested'
                if job['status']=='queued':
                    control['state']=control['desired_state']
                    command.update(status='applied',applied_at=iso(),boundary='before_worker_claim')
                    control['applied_revision']=revision
            elif action=='configure':
                for section,values in payload.items():
                    control.setdefault('desired',{}).setdefault(section,{}).update(values)
            command['after']=deepcopy(control.get('desired',{}))
            self._save(db,ident,control)
            db.execute('INSERT INTO operation_commands VALUES(?,?,?,?)',(ident,command_id,revision,dump(command)))
            return self._control(db,ident)

    def pending(self, job_id, node_id, attempt_id):
        with self.fleet.connection() as db:
            self.fleet._fence(db,job_id,node_id,attempt_id)
            control=self._control(db,job_id)
            # Ordered replay restores desired constraints on a replacement attempt.
            rows=db.execute('SELECT data FROM operation_commands WHERE job_id=? ORDER BY revision',(job_id,)).fetchall()
            result=[]
            for row in rows:
                c=json.loads(row[0])
                if c['status']=='rejected':continue
                if c['status']=='applied' and (c.get('delivered_attempt')==attempt_id or c['action']=='guidance'):continue
                result.append({k:c[k] for k in ('command_id','revision','action','payload','source')})
            return {'revision':control['revision'],'desired_state':control['desired_state'],'commands':result[:100]}

    def report(self, job_id, node_id, attempt_id, telemetry=None, receipts=None):
        with self.fleet.connection(True) as db:
            job=self.fleet._fence(db,job_id,node_id,attempt_id)
            if telemetry is not None:
                if not isinstance(telemetry,dict) or len(dump(telemetry))>300000:
                    raise ValueError('遥测数据过大或无效')
                if job['run_id'] and telemetry.get('run_id') != job['run_id']:
                    raise Conflict('遥测不属于当前运行')
                db.execute('INSERT INTO operation_telemetry VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET attempt_id=excluded.attempt_id,data=excluded.data,updated_at=excluded.updated_at',
                           (job_id,attempt_id,dump(telemetry),iso()))
                db.execute('INSERT INTO operation_attempt_telemetry VALUES(?,?,?,?) ON CONFLICT(job_id,attempt_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',(job_id,attempt_id,dump(telemetry),iso()))
            control=self._control(db,job_id)
            for receipt in receipts or []:
                row=db.execute('SELECT data FROM operation_commands WHERE job_id=? AND command_id=?',(job_id,receipt.get('command_id'))).fetchone()
                if not row:raise Conflict('未知控制回执')
                command=json.loads(row[0])
                if receipt.get('status') not in {'pending','applied','rejected'}:raise ValueError('无效控制回执状态')
                # An applied acknowledgement cannot regress because a stale poll arrived later.
                if command.get('delivered_attempt')==attempt_id and command['status'] in {'applied','rejected'}:continue
                command.update({k:receipt[k] for k in ('status','applied_at','boundary','error','reason','rejected_at') if k in receipt},delivered_attempt=attempt_id,acknowledged_at=iso())
                db.execute('UPDATE operation_commands SET data=? WHERE job_id=? AND command_id=?',(dump(command),job_id,command['command_id']))
            remote=(telemetry or {}).get('control') or {}
            if remote:
                control['effective']=remote.get('effective',{})
                # Pending newer commands outrank an old runtime status sample.
                pending=db.execute('SELECT data FROM operation_commands WHERE job_id=? ORDER BY revision',(job_id,)).fetchall()
                all_commands=[json.loads(x[0]) for x in pending]
                pending_state=next((c for c in reversed(all_commands) if c['action'] in {'pause','resume'} and c['status']=='pending'),None)
                control['state']=('pause_requested' if pending_state['action']=='pause' else 'resume_requested') if pending_state else remote.get('state','running')
                control['applied_revision']=max((c['revision'] for c in all_commands if c['status']=='applied'),default=0)
                self._save(db,job_id,control)
            return self._control(db,job_id)

    def telemetry(self, job_id, attempt_id):
        with self.fleet.connection() as db:
            row=db.execute('SELECT * FROM operation_telemetry WHERE job_id=?',(job_id,)).fetchone()
        if not row:return {}
        value=json.loads(row['data']);value['updated_at']=row['updated_at']
        value['stale_attempt']=row['attempt_id']!=attempt_id
        value['age_seconds']=max(0,time.time()-datetime.fromisoformat(row['updated_at']).timestamp())
        return value

    def job_detail(self, job_id):
        with self.fleet.connection(True) as db:
            self.fleet._expire(db)
            job=self.fleet._job(db,job_id,summary=True)
        telemetry=self.telemetry(job_id,job.get('attempt_id'))
        if telemetry.get('stale_attempt'):
            telemetry={k:v for k,v in telemetry.items() if k in {'stale_attempt','age_seconds','updated_at'}}
        control=self.control(job_id)
        stage=telemetry.get('stage') or job.get('progress') or job['status']
        usage=deepcopy(telemetry.get('usage') or usage_summary(job.get('result') or {}))
        with self.fleet.connection() as db:
            past=[json.loads(r[0]).get('usage',{}) for r in db.execute('SELECT data FROM operation_attempt_telemetry WHERE job_id=? AND attempt_id!=?',(job_id,job.get('attempt_id') or ''))]
        if past:
            current=deepcopy(usage)
            usage['current_attempt']=current
            usage['reported_tokens']=sum(x.get('reported_tokens',0) for x in [*past,current])
            usage['known']=all(x.get('known',False) for x in [*past,current])
            usage['total_tokens']=usage['reported_tokens'] if usage['known'] else None
            usage['attempts_with_telemetry']=len(past)+int(bool(telemetry.get('usage')))
            for field in ('input_tokens','output_tokens','model_calls','tool_calls'):
                vals=[x.get(field) for x in [*past,current]]
                usage[field]=sum(vals) if all(isinstance(x,int) for x in vals) else None
        sampled_attempts=len(past)+int(bool(telemetry.get('usage')))
        usage['unknown_attempts']=max(0,job.get('attempts',0)-sampled_attempts)
        if usage['unknown_attempts']:
            usage['known']=False;usage['total_tokens']=None
        graph=self.job_graph(job,telemetry,control)
        return {'schema_version':SCHEMA,'id':job_id,'kind':'job','label':job.get('question','')[:160],
                'status':job['status'],'progress':stage,'usage':usage,'resources':telemetry.get('resources') or {},
                'control':control,'graph':graph,'events':telemetry.get('events') or [],
                'updated_at':telemetry.get('updated_at') or job['updated_at'],'telemetry_stale':telemetry.get('stale_attempt',False) or telemetry.get('age_seconds',0)>30,
                'telemetry_age_seconds':telemetry.get('age_seconds'),'elapsed_seconds':max(0,(datetime.fromisoformat(job['updated_at']).timestamp() if job['status'] in TERMINAL else time.time())-datetime.fromisoformat(job['created_at']).timestamp()),
                'paused_seconds':(telemetry.get('control') or {}).get('paused_seconds'),
                'elapsed_basis':'wall_time_since_queue; paused duration separately reported',
                'node_id':job.get('node_id'),'attempt_id':job.get('attempt_id'),'run_id':job.get('run_id'),
                'exploration_run_id':job.get('exploration_run_id'),'archive_url':job.get('archive_url'),
                'capabilities':{'pause_boundary':'next_safe_boundary; in-flight calls are allowed to finish',
                                'resources_scope':'shared process, not isolated per-task allocation','cost_known':False}}

    @staticmethod
    def job_graph(job,t,c):
        graph_state=c.get('state') if c.get('state') in {'paused','pause_requested','resume_requested'} else job['status']
        nodes=[{'id':'job:'+job['id'],'label':'任务协调','kind':'coordinator','status':graph_state}]
        edges=[]
        parent=nodes[0]['id']
        if job.get('node_id'):
            ident='worker:'+job['node_id']+':'+job['id']
            nodes.append({'id':ident,'label':'Worker '+job['node_id'],'kind':'worker','status':graph_state})
            edges.append({'id':'dispatch:'+job['id'],'source':parent,'target':ident,'kind':'dispatch','status':graph_state,'label':'执行租约'})
            parent=ident
        if job.get('run_id'):
            agent='agent:'+job['run_id']; state=c.get('state') if c.get('state') in {'paused','pause_requested','resume_requested'} else t.get('status',job['status'])
            nodes.append({'id':agent,'label':'图纸事实 Agent','kind':'agent','status':state,'detail':t.get('stage','')})
            edges.append({'id':'agent-dispatch:'+job['id'],'source':parent,'target':agent,'kind':'dispatch','status':state,'label':'分析执行'})
            for event in t.get('journal',[])[-60:]:
                if event.get('kind') not in {'model','tool'}:continue
                ident='step:'+event['id']; kind=event['kind']
                nodes.append({'id':ident,'label':event.get('title') or kind,'kind':kind,'status':event.get('status','unknown'),'detail':event.get('tool') or ''})
                edges.append({'id':'edge:'+ident,'source':agent,'target':ident,'kind':'request' if kind=='model' else 'tool_call','status':event.get('status','unknown'),'label':'请求 / 响应' if kind=='model' else '调用 / 返回'})
            for command in c.get('commands',[])[-15:]:
                if command['action']!='guidance':continue
                ident='guide:'+command['command_id'];kind=command['source']
                nodes.append({'id':ident,'label':'人工引导' if kind=='human' else '模型监督引导','kind':kind,'status':command['status'],'detail':command['payload'].get('message','')})
                edges.append({'id':'edge:'+ident,'source':ident,'target':agent,'kind':'guidance','status':command['status'],'label':'已生效' if command['status']=='applied' else '等待安全节点'})
        return {'nodes':nodes,'edges':edges,'note':'展示可核验调用与交互事件，不包含模型内部私有推理；单执行 Agent 不伪装为多个 Agent。'}

    def reconcile_exploration(self, run_id):
        run=self.exploration.get_run(run_id)
        for command in run.get('control',{}).get('commands',[]):
            payload={k:v for k,v in command['payload'].items() if k!='dispatch_budget'}
            if command['action']=='configure' and not payload:continue
            for case in run['cases']:
                ident=case.get('job_id')
                if not ident and case.get('dispatch_key'):
                    pending=self.fleet.job_for_key(case['dispatch_key'])
                    ident=pending['id'] if pending else None
                if not ident:continue
                job=self.fleet.job(ident)
                if job['status'] in TERMINAL or job['status']=='cancel_requested':continue
                key='e0:'+sha256((run_id+command['command_id']+ident).encode()).hexdigest()
                # Idempotency makes restart/readback reconciliation safe.
                current=self.control(ident)
                try:self.submit('job',ident,key,current['revision'],command['action'],payload,command['source'])
                except Conflict:continue

    def reconcile_all(self):
        if not self.exploration:return
        for run in self.exploration.list_runs():
            if not run.get('closed'):self.reconcile_exploration(run['run_id'])

    def exploration_detail(self, run_id):
        run=self.exploration.get_run(run_id);self.reconcile_exploration(run_id)
        children=[self.job_detail(c['job_id']) for c in run['cases'] if c.get('job_id')]
        control=deepcopy(run.get('control') or self._empty())
        control.setdefault('effective',{})['dispatch_budget']=run['budget']
        control['effective_by_job']=[{'job_id':c['id'],'status':c['status'],'state':c['control']['state'],
                                      'effective':deepcopy(c['control'].get('effective',{}))} for c in children]
        control['mixed_sections']=[]
        for section in ('budget','sandbox','validator'):
            values=[c['control'].get('effective',{}).get(section) for c in children]
            if values and all(v is not None and v==values[0] for v in values):control['effective'][section]=deepcopy(values[0])
            else:
                control['effective'].pop(section,None)
                if values:control['mixed_sections'].append(section)
        control['effective_consistent']=bool(children) and not control['mixed_sections']
        # The browser command tail is paginated, but receipt lookup must cover all
        # historical parent commands. Omitted receipt is pending, never success.
        child_receipts={}
        with self.fleet.connection() as db:
            for child in children:
                entries={}
                for row in db.execute('SELECT command_id,data FROM operation_commands WHERE job_id=?',(child['id'],)):
                    value=json.loads(row['data'])
                    entries[row['command_id']]={k:value[k] for k in ('status','boundary','reason','error','rejected_at') if k in value}
                child_receipts[child['id']]=entries
        for command in control.get('commands',[]):
            forwarded=command['action']!='configure' or any(k!='dispatch_budget' for k in command['payload'])
            if not forwarded:continue
            statuses=[]
            for child in children:
                key='e0:'+sha256((run_id+command['command_id']+child['id']).encode()).hexdigest()
                receipt=child_receipts[child['id']].get(key)
                if receipt:
                    statuses.append({'job_id':child['id'],**receipt})
                elif child['status'] not in TERMINAL and child['status']!='cancel_requested':
                    statuses.append({'job_id':child['id'],'status':'pending','boundary':'awaiting_delivery','reason':'receipt_not_received'})
            command['targets']=statuses
            if statuses:
                command['status']='rejected' if any(s['status']=='rejected' for s in statuses) else 'applied' if all(s['status']=='applied' for s in statuses) else 'pending'
            elif command['action'] not in {'pause','resume'}:
                command['status']='pending';command['boundary']='awaiting_agent'
            if command['status']!='applied':command.pop('applied_at',None)
        control['commands']=control.get('commands',[])[-100:]
        control['applied_revision']=max((c['revision'] for c in control['commands'] if c['status']=='applied'),default=0)
        nodes=[{'id':run_id,'label':run.get('label') or '规则探索协调','kind':'coordinator','status':control.get('state') if control.get('desired_state')=='paused' else run['status']}]
        edges=[]
        for case in run['cases']:
            child=next((j for j in children if j['id']==case.get('job_id')),None)
            status=child['control']['state'] if child and child['control']['state'] in {'paused','pause_requested'} else case['status']
            nodes.append({'id':case['case_id'],'label':case['rule'].get('rule_id') or case['rule_ref'],'kind':'case','status':status,'job_id':case.get('job_id')})
            edges.append({'id':'dispatch:'+case['case_id'],'source':run_id,'target':case['case_id'],'kind':'dispatch','status':status,'label':'规则探索'})
            if child:
                for n in child['graph']['nodes']:
                    if not any(x['id']==n['id'] for x in nodes):nodes.append(n)
                edges.extend(child['graph']['edges'])
                edges.append({'id':'case-job:'+case['case_id'],'source':case['case_id'],'target':'job:'+child['id'],'kind':'dispatch','status':status,'label':'执行任务'})
        active_children=[c for c in children if c['status'] not in TERMINAL and c['status']!='cancel_requested']
        if run.get('closed'):
            control['state']='terminal'
        elif control.get('desired_state')=='paused':
            control['state']='pause_requested' if any(c['control']['state']!='paused' for c in active_children) else 'paused'
        else:
            control['state']='resume_requested' if any(c['control']['state'] in {'paused','pause_requested','resume_requested'} for c in active_children) else 'running'
        self.exploration.save_control_projection(run_id,control)
        usages=[x['usage'] for x in children];known=bool(usages) and all(x['known'] for x in usages)
        reported=sum(x.get('reported_tokens',0) for x in usages)
        return {'schema_version':SCHEMA,'id':run_id,'kind':'exploration','label':run.get('label') or run_id,'status':run['status'],'closed':run.get('closed',False),
                'progress':f"{run['coverage']['terminal_cases']} / {len(run['cases'])} 条探索结束（不代表规则判断完成）",
                'usage':{'known':known,'total_tokens':reported if known else None,'reported_tokens':reported,'cost':None},
                'resources':{'scope':'per_child_shared_process','items':[{'job_id':c['id'],**c['resources']} for c in children]},
                'control':control,'graph':{'nodes':nodes,'edges':edges},'events':[],'updated_at':iso(),
                'telemetry_sampled_at':min((c['resources']['sampled_at'] for c in children if c['resources'].get('sampled_at')),default=None),
                'telemetry_stale':any(c.get('telemetry_stale') for c in children),'telemetry_age_seconds':max((c.get('telemetry_age_seconds') or 0 for c in children),default=None),
                'budget':run['budget'],'budget_usage':run['budget_usage'],'scope_hash':run['scope_hash'],'project_id':run['project_id'],
                'cases':[{k:c.get(k) for k in ('case_id','job_id','status')} for c in run['cases']]}

    def snapshot(self,kind,ident):
        return self.job_detail(ident) if kind=='job' else self.exploration_detail(ident)

    def overview(self):
        items=[]
        for j in self.fleet.jobs():
            d=self.job_detail(j['id']);summary={k:v for k,v in d.items() if k not in {'graph','events','capabilities'}}
            summary['control']={k:v for k,v in d['control'].items() if k!='commands'}
            items.append(summary)
        explorations=[]
        if self.exploration:
            for run in self.exploration.list_runs()[:100]:
                explorations.append({'id':run['run_id'],'kind':'exploration','label':run.get('label') or run['run_id'],'status':run['status'],
                                     'project_id':run['project_id'],'control':{k:v for k,v in run.get('control',{}).items() if k!='commands'},'budget_usage':run['budget_usage'],'updated_at':run['created_at']})
        return {'schema_version':SCHEMA,'updated_at':iso(),'items':items,'exploration_runs':explorations,'nodes':self.fleet.nodes(),
                'totals':{'jobs':len(items),'running':sum(x['status']=='running' for x in items),'queued':sum(x['status']=='queued' for x in items),
                          'reported_tokens':sum(x['usage'].get('reported_tokens',0) for x in items),'usage_unknown_jobs':sum(not x['usage']['known'] for x in items)}}
