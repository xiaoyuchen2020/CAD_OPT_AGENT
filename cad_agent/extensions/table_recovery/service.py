"""Separate synchronous bounded API. No changes to existing semantic reads."""
import hashlib
import threading
import time

from cad_agent.analysis.general import DrawingIndex
from cad_agent.contracts import stable_id
from cad_agent.cad.render import render_svg
from .bundle import prepare, digest, verify_bundle
from .contracts import METHOD, VALIDATOR
from .sandbox import execute, SandboxError
from .validator import validate


class RecoveryService:
    def __init__(self,workspace,snapshot_id,index=None):
        self.w=workspace;self.sid=snapshot_id
        self.index=index or DrawingIndex(workspace,snapshot_id)
        with workspace.lock:
            if not hasattr(workspace,'_table_recovery_lock'):
                workspace._table_recovery_lock=threading.Lock()

    def record(self,kind,key):
        item=self.w.store.get(kind,key)
        if item['snapshot_id']!=self.sid:raise KeyError(key)
        return item

    def diagnose(self,table_id):
        try:
            bundle=prepare(self.index,table_id)
        except ValueError as exc:
            if not str(exc).startswith('INPUT_LIMIT_EXCEEDED'):
                raise
            diagnostic_id=stable_id('trdiag',self.sid,table_id,str(exc))
            result=dict(diagnostic_id=diagnostic_id,snapshot_id=self.sid,table_id=table_id,
                        source_sha256=self.index.drawing['sha256'],input_id=None,input_available=False,
                        code='INPUT_LIMIT_EXCEEDED',message=str(exc),dynamic_code_enabled=False)
            self.w.store.put('table_recovery_diagnostic',diagnostic_id,result)
            return result
        self.w.store.put('table_recovery_input',bundle['input_id'],bundle)
        diagnostic_id=stable_id('trdiag',bundle['input_id'])
        result=dict(diagnostic_id=diagnostic_id,snapshot_id=self.sid,input_id=bundle['input_id'],
                    bundle_sha256=bundle['bundle_sha256'],input_available=True,table_id=table_id,baseline=bundle['baseline'],
                    coverage=bundle['coverage'],counts={k:len(bundle[k]) for k in ('texts','segments','excluded_geometry')},
                    methods=[dict(method_ref=METHOD,status='experimental',automatic_promotion=False)],
                    dynamic_code_enabled=False)
        self.w.store.put('table_recovery_diagnostic',diagnostic_id,result)
        return result

    def read_input(self,args):
        bundle=self.record('table_recovery_input',args.input_id);verify_bundle(bundle)
        values=bundle[args.view]
        return dict(input_id=args.input_id,bundle_sha256=bundle['bundle_sha256'],view=args.view,
                    snapshot_id=self.sid,table_id=bundle['table_id'],bbox=bundle['bbox'],
                    items=values[args.offset:args.offset+args.limit],offset=args.offset,
                    total=len(values),has_more=args.offset+args.limit<len(values))

    def run(self,args):
        # A single recovery worker per Workspace; persist idempotence and attempts.
        if not self.w._table_recovery_lock.acquire(blocking=False):
            raise ValueError('RECOVERY_BUSY')
        try:
            diag=self.record('table_recovery_diagnostic',args.diagnostic_id)
            bundle=self.record('table_recovery_input',args.input_id);verify_bundle(bundle)
            if diag['input_id']!=args.input_id:raise ValueError('DIAGNOSTIC_INPUT_MISMATCH')
            key=stable_id('trrun',self.sid,args.idempotency_key)
            request_hash=digest(args.model_dump())
            try:
                existing=self.record('table_recovery_run',key)
            except KeyError:
                existing=None
            if existing:
                if existing['request_sha256']!=request_hash:raise ValueError('IDEMPOTENCY_CONFLICT')
                if existing['execution_status']=='running':
                    existing.update(execution_status='failed',error='INTERRUPTED')
                    self.w.store.put('table_recovery_run',key,existing)
                return existing
            prior=[r for r in self.w.store.list('table_recovery_run') if r['input_id']==args.input_id]
            if len(prior)>=3:raise ValueError('RECOVERY_ATTEMPT_LIMIT')
            if sum(r.get('elapsed_seconds',120) for r in prior)>=120:raise ValueError('RECOVERY_TIME_LIMIT')
            # Re-read trusted input; stale or externally edited source artifacts are refused.
            fresh=prepare(DrawingIndex(self.w,self.sid),bundle['table_id'])
            if fresh['bundle_sha256']!=bundle['bundle_sha256']:raise ValueError('INPUT_CHANGED')
            run=dict(run_id=key,snapshot_id=self.sid,input_id=args.input_id,diagnostic_id=args.diagnostic_id,
                     method_ref=args.method_ref,request_sha256=request_hash,execution_status='running',
                     validation_status=None,review_status='pending',dynamic_code_enabled=False)
            self.w.store.put('table_recovery_run',key,run)
            started=time.monotonic()
            try:
                execution=execute(self.w.settings.data_dir,dict(operation='recover',bundle=bundle))
                if hashlib.sha256(self.w.store.artifact(self.sid,'source.dxf').read_bytes()).hexdigest()!=bundle['source_sha256']:
                    raise SandboxError('INPUT_CHANGED_DURING_RUN')
                validation=validate(fresh,execution['output'])
                rid=stable_id('trresult',key,execution['output_sha256'],VALIDATOR)
                result=dict(result_id=rid,snapshot_id=self.sid,table_id=bundle['table_id'],layout_id=bundle['layout_id'],
                            source_sha256=bundle['source_sha256'],bbox=bundle['bbox'],run_id=key,
                            bundle_sha256=bundle['bundle_sha256'],method_ref=METHOD,**validation)
                self.w.store.put('table_recovery_result',rid,result)
                # Full output is retained but never treated as trusted evidence.
                execution_id=stable_id('trexec',key)
                self.w.store.put('table_recovery_execution',execution_id,dict(snapshot_id=self.sid,**execution))
                run.update(execution_status='completed',result_id=rid,execution_id=execution_id,validation_status=validation['validation_status'],
                           execution={k:v for k,v in execution.items() if k!='output'})
            except SandboxError as exc:
                run.update(execution_status='timed_out' if exc.code=='SANDBOX_TIMEOUT' else 'failed',error=exc.code)
            except Exception:
                run.update(execution_status='failed',error='RECOVERY_INTERNAL_ERROR')
                self.w.store.put('table_recovery_run',key,run)
                raise
            run['elapsed_seconds']=round(time.monotonic()-started,3)
            self.w.store.put('table_recovery_run',key,run)
            return run
        finally:
            self.w._table_recovery_lock.release()

    def get_run(self,run_id):
        value=self.record('table_recovery_run',run_id)
        if value['execution_status']=='running' and not self.w._table_recovery_lock.locked():
            value.update(execution_status='failed',error='INTERRUPTED')
            self.w.store.put('table_recovery_run',run_id,value)
        return value

    def result(self,args):
        value=self.record('table_recovery_result',args.result_id)
        cells=value.pop('cells')
        return value | dict(items=cells[args.offset:args.offset+args.limit],offset=args.offset,
                            total=len(cells),has_more=args.offset+args.limit<len(cells))

    def overlay(self,result_id):
        value=self.record('table_recovery_result',result_id)
        bundle=self.record('table_recovery_input',value['input_id']);verify_bundle(bundle)
        if hashlib.sha256(self.w.store.artifact(self.sid,'source.dxf').read_bytes()).hexdigest()!=value['source_sha256']:
            raise ValueError('SOURCE_INTEGRITY_FAILED')
        a,b,c,d=value['bbox'];pad=max(c-a,d-b)*.05
        svg=render_svg(self.w.rendering(self.sid),value['layout_id'],[a-pad,b-pad,c+pad,d+pad])
        cells=''.join(f'<rect x="{cell["bbox"][0]}" y="{-cell["bbox"][3]}" width="{cell["bbox"][2]-cell["bbox"][0]}" height="{cell["bbox"][3]-cell["bbox"][1]}" fill="none" stroke="#d05a00" stroke-width="1.5" vector-effect="non-scaling-stroke"/>' for cell in value['cells'])
        return svg.replace('</svg>',cells+'<desc>候选网格，未进行工程语义验收。</desc></svg>')
