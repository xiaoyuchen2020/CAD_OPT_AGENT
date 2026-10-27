"""Task-owned code versions. Execution, source validation, and admission are separate.

No generated Python is imported or executed in this process. All executable
packages go through the pinned adaptation container, including regressions.
"""
import copy
import difflib
import time

from cad_agent.extensions.programs import kernels, package, validation, regression
from cad_agent.extensions.programs.contracts import (
    TOOLS, RESULT_TOOLS, VERSION, ExecuteProgram, ExecuteToolVersion, ReadResult,
)
from cad_agent.extensions.programs.model import propose
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.sandbox import runtime, execute
from cad_agent.extensions.table_recovery.bundle import prepare, prepare_region, digest, encoded
from cad_agent.extensions.table_recovery.sandbox import SandboxError
from cad_agent.analysis.task_methods import require_enabled, _Cancellation, _text_evidence
from cad_agent.execution import ExecutionCancelled
from cad_agent.adapters.model import ModelError

PROMPT='''
已授权动态代码时，还可 inspect_tool_implementation 获取 read_assembly 的真实可修改片段分组/排序源码；
只有既有 read_assembly 有局部原生行范围时，revise_tool 才能生成真实源码补丁并做旧新差异和独立回归，成功后为本任务安装版本。
execute_tool_version 可将已验证版本应用到另一次原生 read_assembly 观测；后续同一目标的 read_assembly 自动路由，失败回退原版并显示原因。
develop_program 用已有局部读取观测列表和有向无环 contract 生成多模块 Python 程序。contract.nodes 支持 source/select(字面OR)/field(首个前后分隔符)/normalize(NFKC去空白casefold)/number(显式单位)/count/sum/compare(有序值列表相等)/join(值和单位相等)/sort(字典序)/distinct(首项)，output指定输出节点。
program必须返回全部中间节点及精确来源；execute_program 对新观测重新执行，read_program_result 只分页读旧结果。
新数据必须先通过原生工具读取；这些工具不能扩大授权范围、联网或安装依赖。程序契约证明输入范围内变换，不证明工程身份、全图完整性或材料等价。
回答派生结果请逐字使用返回的 claim_templates，并引用其 observation_id；原文使用 evidence_ids。
register_program 要求同一代码在至少两份不同来源图纸上执行验证成功；注册后仍只为 experimental，可用 list_programs 查看并按适用契约在新输入重验。
'''


def _service(s):return AdaptationService(s.w,s.request.snapshot_id)
def _ledger(s):return s.task.data.setdefault('task_programs',{})
def _results(s):return s.task.data.setdefault('program_results',{})


def _index(s,sid):
    if sid not in s.allowed_snapshots:raise ValueError('PROGRAM_SOURCE_NOT_AUTHORIZED')
    if sid==s.request.snapshot_id:return s.index
    from cad_agent.analysis.general import DrawingIndex
    return DrawingIndex(s.w,sid)


def _sources(s,ids):
    out=[]
    for oid in ids:
        observation=s.observations.get(oid)
        if not observation or observation['tool'] in TOOLS:raise ValueError('PROGRAM_NATIVE_BASELINE_REQUIRED')
        scope=observation.get('task_method_scope')
        if not scope:raise ValueError('PROGRAM_NATIVE_SCOPE_REQUIRED')
        index=_index(s,observation['snapshot_id'])
        b=prepare(index,scope['table_id']) if scope.get('table_id') else prepare_region(index,scope['layout_id'],scope['bbox'])
        if b['coverage']['blocking_diagnostics']:raise ValueError('PROGRAM_INPUT_INCOMPLETE')
        AdaptationService(s.w,index.snapshot).source_check(b)
        out.append(dict(snapshot_id=index.snapshot,bundle=b,observation_id=oid))
    return out


def _input(s,ids,contract=None):
    refs=_sources(s,ids)
    body=dict(sources=[r['bundle'] for r in refs],contract=contract)
    body['input_id']=digest(body)
    # Validate declared plan/applicability and bounded intermediates BEFORE generation.
    if contract:validation.expected(body,contract)
    return body,refs


def _check_sources(s,refs):
    for ref in refs:
        _index(s,ref['snapshot_id'])
        AdaptationService(s.w,ref['snapshot_id']).source_check(ref['bundle'])


def _check(s,entry):
    if entry['version']!=VERSION or entry['sdk_sha256']!=package.sdk_hash():raise ValueError('PROGRAM_VERSION_CHANGED')
    if entry['kind']=='revision' and (entry['base_sha256']!=kernels.base_hash() or entry.get('kernel_host_sha256')!=kernels.host_hash()):raise ValueError('TOOL_BASE_VERSION_CHANGED')
    artifact=_service(s).artifact('program',entry['package_artifact_id'])
    if artifact!=package.manifest(artifact['package']):raise ValueError('PROGRAM_PACKAGE_CHANGED')
    return artifact['package']


def _load(s,pid):
    entry=_ledger(s).get(pid)
    if entry:return entry
    # Cross-task reuse is explicit and limited to registry metadata; no old
    # evidence/results are imported into the current task.
    try:registered=_service(s).artifact('program_registration',pid)
    except KeyError:raise ValueError('PROGRAM_NOT_OWNED_OR_REGISTERED')
    entry=registered['entry']
    if entry['kind']!='program':raise ValueError('REVISION_CROSS_TASK_NOT_SUPPORTED')
    _check(s,entry)
    entry=copy.deepcopy(entry);entry.update(task_id=s.task.data['task_id'],reserved_tokens=0,attempts=[],imported_from=pid)
    _ledger(s)[pid]=entry;s.task.save()
    return entry


def inspect(s,args):
    require_enabled(s)
    return dict(tool=args.tool,editable_kernel='source_spans',base_sha256=kernels.base_hash(),source=kernels.source(),
        interface='run(bundle) -> [[region_id,start,end,column],...]',
        input='host-selected texts(entity_id,origin,height), regions(region_id,text,parts), column_edges, bbox',
        contract='All native parts exactly once; no truncation, foreign text or changed columns. Exact column/y-desc/x-asc order.',
        protected=['source_loader','evidence_registration','binding_proofs','validator'],
        scope='task_only',automatic_global_promotion=False)


def _budget(s):
    from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
    from cad_agent.runtimes.token_budget import charged_tokens
    from cad_agent.runtimes.budget_policy import for_session
    policy=for_session(s)
    slot=min(32000,CodexSubscriptionRuntime().total_budget(s)-charged_tokens(s)-(policy.reserved_finalization_tokens if policy else 24000))
    if slot<8000:raise ValueError('PROGRAM_TOKEN_BUDGET')
    return slot


def _run(s,pkg,bundle,deadline):
    seconds=min(20,deadline-time.monotonic(),s._remaining()-30)
    if seconds<=0:raise ValueError('PROGRAM_TIME_BUDGET')
    output=execute(s.w.settings.data_dir,bundle,package.payload(pkg),cancel=_Cancellation(s),timeout=seconds)
    s._remaining()
    return output


def _batch(s,pkg,bundles,deadline):
    # A sandboxed wrapper runs the SAME package on all host-owned canaries.
    base=package.payload(pkg)
    wrapper=base.replace('def adapt(bundle):','def _candidate(bundle):',1)
    wrapper+='\ndef adapt(bundle):\n    return [_candidate(b) for b in bundle["cases"]]\n'
    seconds=min(20,deadline-time.monotonic(),s._remaining()-30)
    if seconds<=0:raise ValueError('PROGRAM_TIME_BUDGET')
    return execute(s.w.settings.data_dir,{'cases':bundles},wrapper,cancel=_Cancellation(s),timeout=seconds)


def _generate(s,kind,instruction,bundle,refs,contract=None,base_sha=None):
    key='prog_'+digest(dict(task=s.task.data['task_id'],kind=kind,input=bundle['input_id'],contract=contract,base=base_sha))
    if key in _ledger(s):return key,_ledger(s)[key]
    if len(_ledger(s))>=3 or len(_ledger(s))+len(s.task.data.get('task_methods',{}))>=4:raise ValueError('PROGRAM_TASK_LIMIT')
    runtime(s.w.settings.data_dir)
    service=_service(s)
    entry=dict(version=VERSION,kind=kind,program_id=key,task_id=s.task.data['task_id'],status='running',
               sdk_sha256=package.sdk_hash(),base_sha256=base_sha,kernel_host_sha256=kernels.host_hash() if kind=='revision' else None,contract=contract,reserved_tokens=0,attempts=[],
               input_artifact_id=service.immutable('program_input',dict(bundle=bundle,refs=refs)))
    _ledger(s)[key]=entry;s.task.save()
    deadline=time.monotonic()+min(180,max(0,s._remaining()-30));feedback=[]
    try:
        for _ in range(3):
            seconds=min(55,deadline-time.monotonic(),s._remaining()-30)
            if seconds<5:raise ValueError('PROGRAM_TIME_BUDGET')
            slot=_budget(s);_check_sources(s,refs)
            attempt=dict(status='generating',reserved_tokens=slot);entry['attempts'].append(attempt)
            entry['reserved_tokens']+=slot;s.task.save()
            s.progress(stage='工具修订/新程序 · 生成与独立验证',trace=s.trace,usage=s.usage)
            proposal,meta=propose(s,kind,instruction,bundle,feedback,slot,seconds,base=kernels.source() if kind=='revision' else None)
            attempt['model_artifact_id']=service.immutable('program_model',meta)
            usage=dict(meta.get('usage',{}),source='task_program',program_id=key,billing='subscription')
            s.usage.append(usage);entry['reserved_tokens']+=max(0,usage.get('total_tokens',0)-slot)
            if proposal.strategy=='abstain':entry['status']='unresolved';break
            try:
                if proposal.package is None:raise ValueError('PACKAGE_REQUIRED')
                pkg=proposal.package.model_dump();manifest=package.manifest(pkg)
                attempt['package_artifact_id']=service.immutable('program',manifest)
                if kind=='revision':
                    if len(pkg['files'])!=1 or pkg['entry']!='main':raise ValueError('REVISION_SINGLE_KERNEL_REQUIRED')
                    diff=''.join(difflib.unified_diff(kernels.source().splitlines(True),pkg['files']['main'].splitlines(True),fromfile='base',tofile='candidate'))
                    if not diff:raise ValueError('REVISION_NO_CODE_CHANGE')
                    attempt['diff_artifact_id']=service.immutable('program_diff',dict(base_sha256=base_sha,diff=diff))
                execution=_run(s,pkg,bundle,deadline);_check_sources(s,refs)
                attempt['execution_artifact_id']=service.immutable('program_execution',execution)
                if kind=='revision':
                    report=kernels.validate(bundle,execution['output'],strict_order=True)
                    fixtures=regression.cases(bundle)
                    checks=_batch(s,pkg,fixtures,deadline)
                    if not isinstance(checks['output'],list) or len(checks['output'])!=len(fixtures):raise ValueError('REVISION_REGRESSION_MISSING')
                    reports=[kernels.validate(b,o,strict_order=True) for b,o in zip(fixtures,checks['output'])]
                    baseline=_run(s,{'files':{'main':kernels.source()},'entry':'main'},bundle,deadline)
                    attempt['regression_artifact_id']=service.immutable('program_regression',dict(inputs=fixtures,execution=checks,reports=reports))
                    attempt['baseline_artifact_id']=service.immutable('program_execution',baseline)
                    report['changed_output']=baseline['output']!=execution['output']
                else:report=validation.validate(bundle,contract,execution['output'])
                _check_sources(s,refs)
                attempt['validation_artifact_id']=service.immutable('program_validation',report)
                attempt['status']='validated';entry.update(status='validated',package_artifact_id=attempt['package_artifact_id'],accepted_attempt=len(entry['attempts']))
                break
            except (SyntaxError,ValueError,KeyError,TypeError,RecursionError,SandboxError) as exc:
                error=getattr(exc,'code','PROGRAM_CANDIDATE_REJECTED')
                if isinstance(exc,ValueError) and str(exc).isupper() and len(str(exc))<100:error=str(exc)
                attempt.update(status='rejected',error=error)
                if error in ('SANDBOX_UNAVAILABLE','SANDBOX_CANCELLED','SANDBOX_ISOLATION_MISMATCH'):raise
                feedback.append(dict(package=proposal.package.model_dump() if proposal.package else None,errors=[error]))
            s.task.save()
        if entry['status']=='running':entry['status']='unresolved'
    except (ValueError,ModelError,SandboxError,ExecutionCancelled) as exc:
        entry.update(status='cancelled' if isinstance(exc,ExecutionCancelled) else 'refused',error=getattr(exc,'code','PROGRAM_REFUSED'))
        if isinstance(exc,ExecutionCancelled):raise
    finally:
        if entry['status']=='running':entry.update(status='interrupted',error='NO_AUTOMATIC_REGENERATION')
        s.task.save()
    return key,entry


def _unresolved(key,entry):
    return dict(program_id=key,status='unresolved',admitted=False,evidence=[],reason=entry.get('error',entry['status']))


def develop(s,args):
    require_enabled(s)
    contract=args.contract.model_dump()
    bundle,refs=_input(s,args.baseline_observation_ids,contract)
    pid,entry=_generate(s,'program',args.instruction,bundle,refs,contract)
    if entry['status']!='validated':return _unresolved(pid,entry)
    return execute_program(s,ExecuteProgram(program_id=pid,baseline_observation_ids=args.baseline_observation_ids))


def execute_program(s,args):
    require_enabled(s)
    entry=_load(s,args.program_id)
    if entry['status']!='validated':return _unresolved(args.program_id,entry)
    if entry['kind']!='program':raise ValueError('PROGRAM_KIND_MISMATCH')
    pkg=_check(s,entry);bundle,refs=_input(s,args.baseline_observation_ids,entry['contract'])
    rid='pr_'+digest(dict(program=args.program_id,input=bundle['input_id'],validator=validation.VERSION))
    if rid not in _results(s):
        if len(_results(s))>=12:raise ValueError('PROGRAM_EXECUTION_LIMIT')
        service=_service(s)
        record=dict(program_id=args.program_id,status='executing',
                    input_artifact_id=service.immutable('program_input',dict(bundle=bundle,refs=refs)))
        _results(s)[rid]=record;s.task.save()
        try:
            # Generation already executed this exact immutable input; reuse it.
            initial=service.artifact('program_input',entry['input_artifact_id'])
            if bundle==initial['bundle'] and entry.get('accepted_attempt') and entry.get('attempts'):
                attempt=entry['attempts'][entry['accepted_attempt']-1]
                execution=service.artifact('program_execution',attempt['execution_artifact_id'])
            else:execution=_run(s,pkg,bundle,time.monotonic()+30)
            _check_sources(s,refs)
            report=validation.validate(bundle,entry['contract'],execution['output'])
            record.update(status='validated',execution_artifact_id=service.immutable('program_execution',execution),
                          validation_artifact_id=service.immutable('program_validation',report))
        except (ValueError,ModelError,SandboxError,ExecutionCancelled) as exc:
            record.update(status='cancelled' if isinstance(exc,ExecutionCancelled) else 'rejected',error=getattr(exc,'code','PROGRAM_EXECUTION_REJECTED'))
            if isinstance(exc,ExecutionCancelled):raise
        finally:
            if record['status']=='executing':record['status']='interrupted'
            s.task.save()
    return read(s,ReadResult(result_id=rid))


def _verified_result(s,rid):
    record=_results(s).get(rid)
    if not record:raise ValueError('PROGRAM_RESULT_NOT_OWNED')
    if record['status']!='validated':raise ValueError('PROGRAM_RESULT_NOT_VALIDATED')
    entry=_load(s,record['program_id']);_check(s,entry);svc=_service(s)
    data=svc.artifact('program_input',record['input_artifact_id']);_check_sources(s,data['refs'])
    execution=svc.artifact('program_execution',record['execution_artifact_id'])
    report=validation.validate(data['bundle'],entry['contract'],execution['output'])
    if report!=svc.artifact('program_validation',record['validation_artifact_id']):raise ValueError('PROGRAM_VALIDATION_CHANGED')
    return record,entry,data,report


def read(s,args):
    require_enabled(s)
    record=_results(s).get(args.result_id)
    if record and record['status']!='validated':return dict(result_id=args.result_id,kind='program_result',admitted=False,status=record['status'],evidence=[])
    record,entry,data,report=_verified_result(s,args.result_id)
    rows=report['nodes'][report['output']]
    if args.offset>len(rows):raise ValueError('PROGRAM_OFFSET_INVALID')
    items=copy.deepcopy(rows[args.offset:args.offset+args.limit]);evidence=[];templates=[]
    for row in items:
        local=[]
        for ref in row['refs']:
            src=data['refs'][ref['source']];b=src['bundle'];index=_index(s,src['snapshot_id'])
            text=next(t for t in b['texts'] if t['entity_id']==ref['entity_id'])
            # Rebuild exact source evidence in the owning snapshot, not from code.
            from types import SimpleNamespace
            selected=_text_evidence(SimpleNamespace(index=index),b,dict(ref,quote=text['text'][ref['start']:ref['end']]))
            local.extend(selected)
        row['evidence_ids']=list(dict.fromkeys(e['evidence_id'] for e in local));evidence.extend(local)
        if row.get('transformed'):
            # Exact templates are bound to the cited observation and its contract.
            text=f"按所示程序契约，在本次已读取局部输入上得到：{row['value']}{row['unit']}。"
            templates.append(dict(text=text,state='derived',evidence_basis='tool_observation'))
        else:
            templates.extend(dict(text=e['quote'],state='observed',evidence_basis='source',evidence_ids=[e['evidence_id']]) for e in local)
    out=dict(kind='program_result',result_id=args.result_id,program_id=record['program_id'],admitted=True,status='validated',
        items=items,evidence=evidence,claim_templates=templates,contract=entry['contract'],validator=validation.VERSION,
        offset=args.offset,total=len(rows),has_more=args.offset+len(items)<len(rows),
        source_binding=dict(state='candidate',basis='validated_source_only',attribution='unconfirmed'),
        semantic_acceptance=False,coverage='complete_in_bound_inputs_only',
        note='契约仅证明给定输入的字面变换与计算；不证明做法归属、材料等价、整图完整或工程适用性。')
    nodes=entry['contract']['nodes']
    operations=[]
    if any(n['op']=='select' and all(k in s.request.question for k in n['keywords']) for n in nodes):operations.append('selection')
    if any(n['op'] in ('count','sum') for n in nodes):operations.append('arithmetic')
    out['operation_receipts']=[dict(operation=op,fulfilled=True,basis='independent_program_contract_replay',
        reference=args.result_id,scope='bound_inputs_only',engineering_identity=False) for op in operations]
    if out['has_more']:out['next_call']=dict(tool='read_program_result',arguments=dict(result_id=args.result_id,offset=args.offset+len(items)))
    unbound=s.task.data.setdefault('program_unbound_evidence',{})
    for e in evidence:
        if e['evidence_id'] not in s.evidence:unbound[e['evidence_id']]=args.result_id
    return s._register_tree(out)


def claim_issues(s,claim):
    unbound=s.task.data.get('program_unbound_evidence',{})
    touched=set(claim.evidence_ids)&unbound.keys()
    for eid in touched:_verified_result(s,unbound[eid])
    if touched and claim.state!='unknown' and not any(claim.text==s.evidence[e]['quote'] for e in touched):
        return ['程序来源片段仅支持逐字引用，归属尚未确认。']
    for oid in claim.observation_ids:
        obs=s.observations.get(oid,{})
        if obs.get('tool') not in RESULT_TOOLS:continue
        d=obs['data']
        if d.get('kind')!='program_result' or not d.get('admitted'):
            if claim.state!='unknown':return ['代码修订或未通过验证的程序记录不是事实证据。']
            continue
        _verified_result(s,d['result_id'])
        if claim.state=='unknown':continue
        if not any(t['text']==claim.text and t['state']==claim.state for t in d['claim_templates']):
            return ['程序派生结果必须使用已验证输出的claim_templates，不能扩大范围或工程语义。']
    return []


def _assembly(s,oid):
    refs=_sources(s,[oid]);obs=s.observations[oid]
    if obs['tool']!='read_assembly':raise ValueError('REVISION_ASSEMBLY_BASELINE_REQUIRED')
    args=obs.get('tool_arguments')
    band=obs['data'].get('row_structure')
    if not args or not band or args.get('view','record')!='record':raise ValueError('REVISION_NATIVE_ROW_REQUIRED')
    index=_index(s,obs['snapshot_id'])
    target=index.by_id[obs['data']['target_region_id']]
    return kernels.prepare(index,target,band),refs,args,obs['snapshot_id']


def revise(s,args):
    require_enabled(s)
    if args.base_sha256!=kernels.base_hash():raise ValueError('TOOL_BASE_VERSION_CHANGED')
    bundle,refs,_,_=_assembly(s,args.baseline_observation_id)
    pid,entry=_generate(s,'revision',args.instruction,bundle,refs,base_sha=args.base_sha256)
    if entry['status']!='validated':return _unresolved(pid,entry)
    return execute_tool_version(s,ExecuteToolVersion(version_id=pid,baseline_observation_id=args.baseline_observation_id))


def _route_key(sid,args):
    return digest(dict(snapshot=sid,region=args['region_id'],heading=args.get('heading_start'),anchor=args.get('anchor_entity_id')))


def execute_tool_version(s,args):
    require_enabled(s)
    entry=_ledger(s).get(args.version_id)
    if not entry or entry['kind']!='revision':raise ValueError('TOOL_VERSION_NOT_OWNED')
    if entry['status']!='validated':return _unresolved(args.version_id,entry)
    pkg=_check(s,entry);bundle,refs,native,sid=_assembly(s,args.baseline_observation_id)
    output=_run(s,pkg,bundle,time.monotonic()+30)
    _check_sources(s,refs)
    proof=kernels.validate(bundle,output['output'],strict_order=True)
    svc=_service(s)
    route=dict(version_id=args.version_id,snapshot_id=sid,scope=refs[0]['bundle']['bbox'],
        refs_artifact_id=svc.immutable('program_input',dict(bundle=bundle,refs=refs)),
        executions={bundle['input_id']:dict(input_artifact_id=svc.immutable('program_kernel_input',bundle),execution_artifact_id=svc.immutable('program_execution',output),
            validation_artifact_id=svc.immutable('program_validation',proof))})
    s.task.data.setdefault('tool_version_routes',{})[_route_key(sid,native)]=route;s.task.save()
    attempt=entry['attempts'][entry['accepted_attempt']-1]
    return dict(kind='tool_revision',version_id=args.version_id,tool='read_assembly',status='installed_for_task',
        admitted=False,evidence=[],source_parts=proof['source_parts'],
        diff_artifact_id=attempt['diff_artifact_id'],regression_artifact_id=attempt['regression_artifact_id'],
        next_call=dict(tool='read_assembly',arguments=dict(native,offset=0)),
        note='版本已验证并绑定本轮目标；执行返回的read_assembly调用获取宿主登记的来源证据。')


def routed_read(s,index,args,native_read):
    """Context-local override; canonical caches include version and source hash.

    Failure discards the whole revised page and reads the native version again.
    No partially computed evidence is registered by this function.
    """
    from .task_methods import enabled
    if not enabled(s):return native_read()
    key=_route_key(index.snapshot,args.model_dump())
    route=s.task.data.get('tool_version_routes',{}).get(key)
    if not route:return native_read()
    svc=_service(s);entry=_ledger(s).get(route['version_id'])
    try:
        require_enabled(s)
        if not entry:raise ValueError('TOOL_VERSION_NOT_OWNED')
        pkg=_check(s,entry)
        initial=svc.artifact('program_input',route['refs_artifact_id']);_check_sources(s,initial['refs'])
        def callback(bundle):
            a,b,c,d=route['scope'];x,y,z,w=bundle['bbox']
            if not(a<=x<=z<=c and b<=y<=w<=d):
                # Native header discovery can inspect a neighboring band. The
                # patch has no authorization there, so use the native kernel.
                return kernels.assembly_kernel.run(bundle)
            saved=route['executions'].get(bundle['input_id'])
            if saved:
                execution=svc.artifact('program_execution',saved['execution_artifact_id'])
                proof=kernels.validate(bundle,execution['output'],strict_order=True)
                if proof!=svc.artifact('program_validation',saved['validation_artifact_id']):raise ValueError('TOOL_VALIDATION_CHANGED')
            else:
                if len(route['executions'])>=16:raise ValueError('TOOL_EXECUTION_LIMIT')
                execution=_run(s,pkg,bundle,time.monotonic()+30)
                proof=kernels.validate(bundle,execution['output'],strict_order=True)
                route['executions'][bundle['input_id']]=dict(input_artifact_id=svc.immutable('program_kernel_input',bundle),execution_artifact_id=svc.immutable('program_execution',execution),
                    validation_artifact_id=svc.immutable('program_validation',proof));s.task.save()
            return execution['output']
        # Verify cached execution artifacts even if the native page cache hits.
        for saved in route['executions'].values():
            old_input=svc.artifact('program_kernel_input',saved['input_artifact_id'])
            old_execution=svc.artifact('program_execution',saved['execution_artifact_id'])
            proof=kernels.validate(old_input,old_execution['output'],strict_order=True)
            if proof!=svc.artifact('program_validation',saved['validation_artifact_id']):raise ValueError('TOOL_VALIDATION_CHANGED')
        with kernels.using(route['version_id'],callback):result=native_read()
        _check_sources(s,initial['refs'])
        result['tool_version']=dict(version_id=route['version_id'],base_sha256=entry['base_sha256'],
            validator=kernels.VALIDATOR,scope='task_target',semantic_acceptance=False)
        return result
    except (ValueError,KeyError,SandboxError) as exc:
        s.task.data['tool_version_routes'].pop(key,None);s.task.save()
        result=native_read()
        result['tool_version_fallback']=dict(version_id=route['version_id'],reason=getattr(exc,'code','VERSION_SOURCE_OR_VALIDATION_FAILED'))
        return result


def register(s,args):
    require_enabled(s)
    entry=_load(s,args.program_id)
    if entry['kind']!='program' or entry['status']!='validated':raise ValueError('ONLY_VALIDATED_PROGRAM_REGISTRABLE')
    _check(s,entry);hashes=set();proofs=[]
    for rid,record in _results(s).items():
        if record['program_id']!=args.program_id or record['status']!='validated':continue
        _,_,data,report=_verified_result(s,rid)
        # Require two independently executed input sets, not two files in one call.
        hashes.add(tuple(sorted({ref['bundle']['source_sha256'] for ref in data['refs']})))
        proofs.append(dict(result_id=rid,input_artifact_id=record['input_artifact_id'],
                           validation_artifact_id=record['validation_artifact_id']))
    if len(hashes)<2:raise ValueError('PROGRAM_CROSS_DRAWING_REGRESSION_REQUIRED')
    frozen=copy.deepcopy(entry)
    frozen['attempts']=[];frozen['reserved_tokens']=0
    reg=dict(name=args.name,status='experimental',entry=frozen,regressions=proofs,
             validator=validation.VERSION,applicability=entry['contract'],semantic_acceptance=False,
             automatic_default=False)
    pid=_service(s).immutable('program_registration',reg)
    return dict(program_id=pid,name=args.name,status='experimental',regression_inputs=len(hashes),
                note='仅登记代码与适用契约；新输入须execute_program重验，不继承原结果。')


def list_programs(s,args):
    require_enabled(s)
    values=s.w.store.list('adaptation_program_registration',limit=args.limit,offset=args.offset)
    items=[]
    for value in values:
        pid='ad_program_registration_'+digest(value)
        _service(s).artifact('program_registration',pid)
        if value.get('validator')!=validation.VERSION:continue
        try:_check(s,value['entry'])
        except (ValueError,KeyError):continue
        items.append(dict(program_id=pid,name=value['name'],status=value['status'],applicability=value['applicability']))
    return dict(items=items,offset=args.offset,has_more=len(values)==args.limit)


HANDLERS={'inspect_tool_implementation':inspect,'revise_tool':revise,'execute_tool_version':execute_tool_version,
          'develop_program':develop,'execute_program':execute_program,'read_program_result':read,
          'register_program':register,'list_programs':list_programs}
