"""Bounded real-model runs on separately retained fresh-import real drawings.

Only explicit --live sends requests through the configured subscription adapter.
Credentials stay in the existing account provider; no credential copies/logs.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from cad_agent.artifact_storage import copy_artifact
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.adapters.siwc import SiwcAccount


def hashes():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT/'cad_agent').rglob('*.py'))}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--live',action='store_true')
    ap.add_argument('--case',action='append',choices=['ligong-east','property-bath'])
    ap.add_argument('--output',type=Path,default=ROOT/'artifacts/geometry-isolation-real-e2e-20261019/live')
    ap.add_argument('--fresh-data',type=Path,default=ROOT/'artifacts/geometry-isolation-real-e2e-20261019/data/spawn')
    args=ap.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((ROOT/'tests/fixtures/real_geometry_tasks.json').read_text())
    cases=[c for c in manifest['cases'] if not args.case or c['case'] in args.case]
    plan=dict(runtime='responses_subscription',profile='siwc_sol',model='gpt-6.1-sol',fallback=False,
              budget_per_case=dict(timeout_seconds=360,max_rounds=10,max_tool_calls=24),
              fresh_import_evidence='../evaluation.json',cases=[c['case'] for c in cases],
              note='Reuses fresh DXF import artifacts from the separately measured HTTP import; model run time excludes that import.')
    (out/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
    if not args.live:return
    if (out/'results.json').exists():raise ValueError('Choose a fresh output directory; never overwrite prior live attempts.')
    base=replace(Settings.load().model_catalog()['siwc_sol'],data_dir=out/'data',
                 agent_timeout_seconds=360,agent_max_rounds=10,agent_max_tool_calls=24)
    app=create_app(base);w=app.state.workspace
    account=ROOT/'artifacts/hybrid-retrieval-20261015/data'
    if not (account/'siwc-subscription/accounts.json').is_file():
        raise ValueError('Configured subscription account unavailable; no paid-API fallback.')
    w.siwc_account.close();w.siwc_account=SiwcAccount(account)
    rows=[];before=hashes()
    (out/'code-hashes.json').write_text(json.dumps(before,indent=2))
    with TestClient(app) as client:
        for case in cases:
            row=dict(case=case['case'],status='not_started',model_attempt=False)
            run=None
            started=time.perf_counter()
            try:
                with sqlite3.connect(f'file:{args.fresh_data}/index.sqlite3?mode=ro',uri=True) as conn:
                    records=[json.loads(r[0]) for r in conn.execute("SELECT data FROM records WHERE kind='drawing'")]
                drawing=next(d for d in records if d['sha256']==case['sha256'])
                sid=drawing['id'];source=args.fresh_data/'drawings'/sid
                if hashlib.sha256((source/'source.dxf').read_bytes()).hexdigest()!=case['sha256']:
                    raise ValueError('Fresh source hash mismatch')
                w.store.put('drawing',sid,drawing)
                for name in ('source.dxf','scene.json','regions.json','entities.json','render.json'):
                    if (source/name).is_file():copy_artifact(source/name,w.store.artifact(sid,name))
                lid=next(l['layout_id'] for l in drawing['layouts'] if l['name']=='Model')
                question=(f"仅检查 Model 布局（layout_id={lid}）中 WCS 范围 {case['bbox']}，本次单位按毫米。"
                    "请识别这个范围的门窗洞口候选，逐项列出类型、可识别的开启方式及来源依据。"
                    "说明哪些候选无法确认，不把窗洞当通行门，不将两个空间直接相邻当作已确认连通。"
                    "关系工具请从每页 limit=1 开始，逐页读取该范围候选，并明确未覆盖部分。")
                row.update(snapshot_id=sid,source_sha256=case['sha256'],question=question)
                started=time.perf_counter()
                response=client.post('/api/chat-runs',json=dict(snapshot_id=sid,question=question,mode='agent',
                    runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False,agent_policy='efficient'))
                response.raise_for_status();run=response.json();run_id=run['id']
                row.update(run_id=run_id,task_id=run['task_id'],model_attempt=True)
                print('START',case['case'],run_id,flush=True)
                while run['status'] in ('queued','running'):
                    if time.perf_counter()-started>360:
                        client.post(f'/api/chat-runs/{run_id}/cancel')
                    if time.perf_counter()-started>510:
                        raise TimeoutError('Run did not stop within the cancellation grace period')
                    time.sleep(1)
                    response=client.get(f'/api/chat-runs/{run_id}');response.raise_for_status();run=response.json()
                result=run.get('result') or {}
                task_response=client.get('/api/tasks/'+run['task_id']);task_response.raise_for_status()
                task=task_response.json()
                (out/(case['case']+'-run.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2))
                (out/(case['case']+'-task.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2))
                row.update(status=run['status'],run_id=run_id,task_id=run['task_id'],task_status=task.get('status'),
                    publication_status=task.get('publication_status'),answer=result.get('answer'),error=run.get('error'),
                    tools=[dict(tool=t['tool'],status=t['status'],error=t.get('error')) for t in run.get('trace',[])],
                    tokens=sum(u.get('total_tokens',0) for u in run.get('usage',[])),metrics=run.get('metrics'),
                    fallback_used=result.get('fallback_used'),code_unchanged=hashes()==before)
            except Exception as exc:
                row.update(status='failed',error=dict(type=type(exc).__name__,message=str(exc)))
                if run:
                    (out/(case['case']+'-last-run.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2))
                    if run.get('status') in ('queued','running'):
                        try:
                            cancellation=client.post('/api/chat-runs/'+run['id']+'/cancel')
                            row['cancellation_http_status']=cancellation.status_code
                        except Exception as cancel_exc:
                            row['cancellation_error_type']=type(cancel_exc).__name__
            row['seconds']=time.perf_counter()-started;rows.append(row)
            (out/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
            print('DONE',case['case'],row['status'],round(row['seconds'],3),flush=True)


if __name__=='__main__':main()
