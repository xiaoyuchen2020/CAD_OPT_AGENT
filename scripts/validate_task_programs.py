"""Subscription-only stage 2/3 probe on an explicitly authorized source.

Controlled integration (not an autonomous routing benchmark): real generation,
real Docker, independent validation, and a real model final answer admitted by
normal answer validation. Credentials stay in the configured account store.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
from dataclasses import replace


def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--account-data',type=Path,required=True)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--agent',action='store_true',help='natural instruction without tool names; real Agent routing probe')
    a=p.parse_args()
    if not a.execute:p.error('--execute required for real subscription use')
    root=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis import task_programs as tp
    from cad_agent.extensions.programs.contracts import Pipeline,Node
    from cad_agent.extensions.adaptation.model import subscription_turn
    from cad_agent.extensions.adaptation.sandbox import runtime
    from scripts.validate_task_methods import clean
    data=out/'data';data.mkdir()
    shutil.copyfile(a.account_data/'adaptation-runtime.json',data/'adaptation-runtime.json')
    runtime(data)
    settings=replace(Settings.load(),data_dir=data,provider='responses_subscription',llm_query_intent=False)
    w=Workspace(settings);w.siwc_account.close();w.siwc_account=SiwcAccount(a.account_data)
    summary=dict(validation_scope='controlled_real_generation_execution_final_answer',autonomous_fallback=False,passed=False)
    try:
        assert w.siwc_account.status()['connected']
        drawing=w.ingest('authorized-q06.dxf',a.source.read_bytes())
        if a.agent:
            from cad_agent.analysis.general import DrawingIndex
            index=DrawingIndex(w,drawing['id'])
            target=next(r for r in index.regions if r.text.replace('\n','').replace(' ','')=='2涂料外墙(外墙有保温)')
            question=(f'请编写并实际运行一个可复用的局部分析程序，统计编号2“涂料外墙（外墙有保温）”跨栏行内，'
                      f'完整文字实体中包含“阳台”“设备平台”或“侧墙”的实体条数。标题已定位为{target.region_id}。'
                      '程序需要保留字面筛选与计数的中间结果和来源，请用验证后的结果回答，保留做法归属未知。允许使用动态代码。')
            request=ChatRequest(snapshot_id=drawing['id'],question=question,mode='agent',runtime='responses_subscription',
                model_profile='siwc_sol',source_record_mode='agent',allow_fallback=False)
            dump(out/'manifest.json',dict(source_sha256=drawing['sha256'],request=request.model_dump(),
                code={str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((root/'cad_agent').rglob('*.py'))}))
            run=w.start_chat(request);last=None
            while run['status'] in ('queued','running'):
                time.sleep(2);run=w.store.get('chat_run',run['id'])
                state=(run['status'],run.get('stage'))
                if state!=last:print(json.dumps(dict(status=state[0],stage=state[1]),ensure_ascii=False),flush=True);last=state
            task=w.store.get('agent_task',run['task_id'])
            dump(out/'run.json',clean(run));dump(out/'task.json',clean(task))
            tools=[o['tool'] for o in task.get('observations',{}).values()]
            cited={oid for c in (run.get('result') or {}).get('claims',[]) for oid in c.get('observation_ids',[])}
            dynamic={oid for oid,o in task.get('observations',{}).items() if o['tool'] in ('develop_program','execute_program','read_program_result') and o['data'].get('admitted')}
            summary.update(validation_scope='natural_instruction_agent_routing_no_tool_names',autonomous_fallback=False,
                natural_language_program_request=True,run_status=run['status'],task_status=task['status'],tools=tools,
                generated_programs=len(task.get('task_programs',{})),cited_program_observations=sorted(cited & dynamic),
                passed=run['status']=='completed' and bool(cited & dynamic),usage=run.get('usage',[]))
            print(json.dumps(summary,ensure_ascii=False),flush=True)
            return
        request=ChatRequest(snapshot_id=drawing['id'],question='读取局部原文候选并保留来源，允许使用动态代码',
            mode='agent',runtime='responses_subscription',model_profile='siwc_sol',source_record_mode='agent',agent_policy='legacy',allow_fallback=False)
        s=CadAgentSession(w,request)
        if s.task.complex:
            from cad_agent.analysis.tasks import TaskPlan,PlanStep
            s.task.plan(TaskPlan(steps=[PlanStep(requirement_id=p['id'],kind='lookup',acceptance='引用已读取局部原文或已验证程序结果') for p in s.task.parts]))
        def call(name,args):
            return s.invoke_tool(name,dict(args,task_ids=[p['id'] for p in s.task.parts]))
        target=next(r for r in s.index.regions if r.text.replace('\n','').replace(' ','')=='2涂料外墙(外墙有保温)')
        manifest={str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for folder in ('cad_agent','scripts') for f in sorted((root/folder).rglob('*.py'))}
        dump(out/'manifest.json',dict(source_sha256=drawing['sha256'],request=request.model_dump(),code=manifest))
        native=json.loads(call('read_assembly',dict(region_id=target.region_id)))
        dump(out/'native.json',native)
        inspect=json.loads(call('inspect_tool_implementation',{}))
        print('Generating real tool revision',flush=True)
        revision=json.loads(call('revise_tool',dict(baseline_observation_id=native['observation_id'],base_sha256=inspect['base_sha256'],
            gap='原工具按量化基线排序，不能保证细小高差下的精确纵坐标顺序。',instruction='微调现有run源码，仅将排序的量化纵坐标改为精确纵坐标；保留原有片段分组和全部来源。')))
        dump(out/'revision.json',revision)
        assert revision.get('status')=='installed_for_task',revision
        updated=json.loads(call('read_assembly',dict(region_id=target.region_id)))
        dump(out/'updated.json',updated)
        assert updated.get('tool_version'),updated
        contract=Pipeline(nodes=[Node(id='source',op='source'),Node(id='selected',op='select',inputs=['source'],keywords=['阳台','设备平台','侧墙']),
                                 Node(id='count',op='count',inputs=['selected'])],output='count')
        print('Generating real multi-step program',flush=True)
        program=json.loads(call('develop_program',dict(baseline_observation_ids=[updated['observation_id']],
            instruction='生成程序按提供契约筛选完整文字实体并计数，保留全部中间节点及来源；使用cad_sdk辅助函数。',contract=contract.model_dump())))
        dump(out/'program.json',program)
        assert program.get('admitted'),program
        templates=[dict(t,observation_ids=[program['observation_id']],question_part_ids=[p['id'] for p in s.task.parts]) for t in program['claim_templates']]
        print('Requesting real final answer and validating it',flush=True)
        messages=[dict(role='system',content='Return only JSON with claims and missing_information. Use the supplied verified claim templates verbatim, including state, evidence_basis and observation_ids. Do not add engineering identity, units or coverage claims.'),
                  dict(role='user',content=json.dumps(dict(question=request.question,verified_templates=templates),ensure_ascii=False))]
        raw,usage,diagnostics=subscription_turn(s.w.settings,messages,55,w.siwc_account,check_cancelled=s._remaining)
        dump(out/'final-model.json',dict(message=raw,usage=usage,diagnostics=diagnostics))
        answer=s.validate_answer(raw['content'])
        dump(out/'answer.json',answer)
        summary.update(passed=True,revision=revision['version_id'],program=program['program_id'],result_id=program['result_id'],
            claims=len(answer['claims']),used_observations=[o for c in answer['claims'] for o in c.get('observation_ids',[])],
            generation_usage=s.usage,final_usage=usage,paid_fallback=False)
    except Exception as exc:
        summary.update(error_type=type(exc).__name__,error=str(exc)[:1200])
        raise
    finally:
        if 's' in locals():
            dump(out/'task.json',clean(s.task.data));dump(out/'trace.json',clean(s.trace))
            exported=[];svc=tp._service(s)
            for pid,entry in s.task.data.get('task_programs',{}).items():
                detail=dict(program_id=pid,entry=entry,attempts=[])
                for attempt in entry['attempts']:
                    row=dict(attempt=attempt)
                    for kind,field in [('program_model','model_artifact_id'),('program','package_artifact_id'),('program_diff','diff_artifact_id'),
                                       ('program_execution','execution_artifact_id'),('program_execution','baseline_artifact_id'),
                                       ('program_regression','regression_artifact_id'),('program_validation','validation_artifact_id')]:
                        if field in attempt:row[field]=svc.artifact(kind,attempt[field])
                    detail['attempts'].append(row)
                exported.append(detail)
            dump(out/'program-artifacts.json',clean(exported))
        dump(out/'summary.json',clean(summary));w.close()
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
