"""Isolated product-entry validation against immutable real drawing snapshots.

Uses the existing subscription account and its shared concurrency lock. No API
fallback, credential copy, production task mutation or synthetic answer injection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-data',type=Path,required=True)
    parser.add_argument('--account-data',type=Path)
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--question',default='帮我找下地下室内墙4做法')
    parser.add_argument('--parent-run-id')
    parser.add_argument('--resume-task-id')
    parser.add_argument('--case-name', default='')
    parser.add_argument('--queue-timeout', type=int, default=600)
    parser.add_argument('--legacy',action='store_true')
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    product=out/'product'
    if not product.exists():
        shutil.copytree(root/'cad_agent',product/'cad_agent',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        manifest={str(p.relative_to(product)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in product.rglob('*.py')}
        (out/'code-manifest.json').write_text(json.dumps(manifest,indent=2))
    sys.path.insert(0,str(product));os.environ['PYTHONPATH']=str(product)
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.agent import AgentRunner
    w=Workspace(Settings(data_dir=out/'data',llm_query_intent=not args.legacy,siwc_queue_timeout_seconds=args.queue_timeout))
    try:
        with sqlite3.connect(f'file:{args.source_data.resolve()}/index.sqlite3?mode=ro',uri=True) as db:
            row=db.execute("select data from records where id=? and kind='drawing'",(args.snapshot,)).fetchone()
        if not row:raise ValueError('Drawing snapshot not found')
        drawing=json.loads(row[0]);w.store.put('drawing',args.snapshot,drawing)
        source=args.source_data/'drawings'/args.snapshot
        target=out/'data'/'drawings'/args.snapshot
        if not target.exists():shutil.copytree(source,target)
        case_out=out/args.case_name if args.case_name else out
        case_out.mkdir(parents=True,exist_ok=True)
        def save(name,value):
            (case_out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        save('source.json',dict(snapshot=args.snapshot,name=drawing['name'],source_sha256=drawing['sha256'],
            artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir() if p.is_file()}))
        request=ChatRequest(snapshot_id=args.snapshot,question=args.question,mode='agent',
            runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False,
            source_read_batching=False,parent_run_id=args.parent_run_id,resume_task_id=args.resume_task_id)
        save('request.json',request.model_dump())
        if args.offline:
            s=AgentRunner(w.for_model('siwc_sol'),request)
            value=dict(action='lookup',targets=[dict(target_id='wall4',requirement_id='q1',
                subject=dict(value='内墙',question_quote='内墙4'),identifier=dict(value='4',question_quote='内墙4'),
                conditions=[dict(value='地下室',question_quote='地下室',kind='scope',operator='include')])])
            result=json.loads(s.invoke_tool('search_source_records',dict(target_id='wall4',query_intent=value)))
            save('discovery.json',result)
            print('DISCOVERY',result.get('total'),result.get('error'),flush=True)
            for i,c in enumerate(result.get('items',[])):
                action=c['next_action'];page=json.loads(s.invoke_tool(action['tool'],action['arguments']|{'result_view':'full'}))
                save(f'candidate-{i}-page.json',page)
                print('READ',c['title'],page.get('error'),page.get('total'),page.get('checks'),flush=True)
            save('task.json',s.task.data)
            return
        if not args.account_data:raise ValueError('--account-data is required for subscription E2E')
        w.siwc_account.close();w.siwc_account=SiwcAccount(args.account_data,1)
        record=w.start_chat(request)
        print('STARTED',record['id'],flush=True)
        previous=None;deadline=time.monotonic()+args.queue_timeout+900
        while True:
            record=w.store.get('chat_run',record['id'])
            progress=(record['status'],record.get('stage'),len(record.get('trace',[])))
            if progress!=previous:print('PROGRESS',progress,flush=True);previous=progress
            if record['status'] not in ('queued','running'):break
            if time.monotonic()>deadline:w.cancel_chat(record['id']);deadline=float('inf')
            time.sleep(2)
        save('run.json',record)
        save('task.json',w.store.get('agent_task',record['task_id']))
        for event in record.get('run_events',[]):
            for name,section in event.get('sections',{}).items():
                if name in ('model_result','full_result','context','query'):
                    try:save(section['id']+'.json',w.store.get('run_section',section['id']))
                    except KeyError:pass
        print('FINISHED',record['status'],record.get('error'),flush=True)
    finally:w.close()


if __name__=='__main__':main()
