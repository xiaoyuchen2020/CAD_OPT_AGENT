"""Frozen, subscription-only cross-column pilot; never copies account credentials."""
import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from dataclasses import replace


def dump(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2))


def clean(value):
    if isinstance(value,dict):
        return {k:clean(v) for k,v in value.items() if k not in ('execution_token','access_token','refresh_token','id_token','api_key')}
    if isinstance(value,list):return [clean(v) for v in value]
    return value


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--account-data',type=Path,required=True)
    p.add_argument('--runtime-data',type=Path,required=True)
    p.add_argument('--execute',action='store_true')
    a=p.parse_args()
    if not a.execute:p.error('--execute required for subscription usage')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.contracts import ChatRequest
    from cad_agent.extensions.adaptation.sandbox import runtime
    from cad_agent.extensions.adaptation.service import AdaptationService
    root=Path(__file__).resolve().parents[1]
    manifest={str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((root/'cad_agent').rglob('*.py'))}
    data=out/'data';data.mkdir()
    for name in ('adaptation-runtime.json','table-recovery-runtime.json'):
        shutil.copyfile(a.runtime_data/name,data/name)
    runtime(data)
    settings=replace(Settings.load(),data_dir=data,provider='responses_subscription',llm_query_intent=False)
    w=Workspace(settings)
    w.siwc_account.close();w.siwc_account=SiwcAccount(a.account_data)
    try:
        assert w.siwc_account.status()['connected'],'subscription disconnected'
        d=w.ingest('cross-column-source.dxf',a.source.read_bytes())
        idx=DrawingIndex(w,d['id'])
        target=next(r for r in idx.regions if r.text.replace('\n','').replace(' ','')=='2涂料外墙(外墙有保温)')
        # Explicit route exercise on a real drawing. This does not claim an
        # unprompted autonomous fallback or improvement over native full paging.
        question=(f'请读取图中编号2“涂料外墙（外墙有保温）”跨栏行的局部原文。已定位标题region_id={target.region_id}。'
            '先用read_assembly核查已有结果，再用本轮动态text_spans方法，按原生位置从左到右、同栏从上到下选择包含“阳台”“设备平台”或“侧墙”的完整原文实体。'
            '现有工具未提供这个按多个词筛选并按跨栏位置排序的接口，请在具体gap中说明；不要改写或拼接原文。'
            '最终逐字列出所选原文并引用动态方法返回的原文证据，单独说明做法归属仍未确认，不做完整性或工程适用性判断。')
        request=ChatRequest(snapshot_id=d['id'],question=question,mode='agent',runtime='responses_subscription',model_profile='siwc_sol',
            allow_fallback=False,source_read_batching=False,source_record_mode='agent',dynamic_methods='task_verified')
        dump(out/'plan.json',dict(request=request.model_dump(),source_sha256=d['sha256'],code=manifest,
            validation_scope='real_source_explicit_dynamic_route_end_to_end',unprompted_autonomous_fallback=False,
            acceptance=['actual_generated_code_executed_in_Docker','independent_validation_passed','dynamic_source_evidence_used_by_current_answer','exact_source_spans','no_paid_fallback']))
        run=w.start_chat(request);dump(out/'submitted.json',dict(run_id=run['id']))
        last=None
        while run['status'] in ('queued','running'):
            time.sleep(2);run=w.store.get('chat_run',run['id'])
            state=(run['status'],run.get('stage'))
            if state!=last:print(json.dumps(dict(status=state[0],stage=state[1]),ensure_ascii=False),flush=True);last=state
        dump(out/'run.json',clean(run))
        task=w.store.get('agent_task',run['task_id']);dump(out/'task.json',clean(task))
        methods=task.get('task_methods',{})
        used={eid for c in (run.get('result') or {}).get('claims',[]) for eid in c.get('evidence_ids',[])}
        dynamic_evidence={e['evidence_id'] for o in task.get('observations',{}).values() if o['tool'] in ('develop_method','run_task_method') for e in o['data'].get('evidence',[])}
        exact=[]
        for e in (run.get('result') or {}).get('citations',[]):
            region=idx.by_id[e['region_id']]
            exact.append(region.text[slice(*e['text_span'])]==e['quote'])
        exported=[]
        for mid,entry in methods.items():
            svc=AdaptationService(w,d['id']);record=dict(entry=entry,attempts=[])
            for attempt in entry['attempts']:
                detail=dict(attempt=attempt)
                for kind,field in [('code','code_artifact_id'),('execution','execution_artifact_id'),('validation','validation_artifact_id'),('model','metadata_artifact_id')]:
                    if field in attempt:detail[kind]=svc.artifact(kind,attempt[field])
                record['attempts'].append(detail)
            dump(out/(mid+'.json'),clean(record));exported.append(mid)
        summary=dict(status=run['status'],error=run.get('error'),method_ids=exported,
            generated_attempts=sum(len(m['attempts']) for m in methods.values()),
            validated_methods=sum(m['status']=='validated' for m in methods.values()),
            dynamic_evidence_used=sorted(used&dynamic_evidence),all_citations_exact=bool(exact) and all(exact),
            usage=run.get('usage'),task_status=task.get('status'),claim_count=len((run.get('result') or {}).get('claims',[])),
            fallback_used=(run.get('result') or {}).get('fallback_used'),
            code_unchanged=all(hashlib.sha256((root/name).read_bytes()).hexdigest()==sha for name,sha in manifest.items()))
        summary['passed']=run['status']=='completed' and summary['validated_methods']>0 and bool(summary['dynamic_evidence_used']) and summary['all_citations_exact'] and summary['fallback_used'] is False and summary['code_unchanged']
        dump(out/'summary.json',summary)
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    finally:w.close()


if __name__=='__main__':main()
