"""Isolated actual-drawing regression; --live explicitly uses subscription usage."""
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
from cad_agent.analysis.run_journal import public_value
def save(out, name, value):
    (out / name).write_text(json.dumps(public_value(value), ensure_ascii=False, indent=2) + '\n')


def seed(source, workspace, sid):
    drawing=None
    if (source/'index.sqlite3').exists():
        with sqlite3.connect(f'file:{source}/index.sqlite3?mode=ro', uri=True) as db:
            row=db.execute('SELECT data FROM records WHERE kind=? AND id=?',('drawing',sid)).fetchone()
            if row:
                drawing=json.loads(row[0])
                if fid:=drawing.get('latest_fact_set_id'):
                    fact=db.execute('SELECT kind,data FROM records WHERE id=?',(fid,)).fetchone()
                    if fact:workspace.store.put(fact[0],fid,json.loads(fact[1]))
    folder=source/'drawings'/sid
    if drawing is None and (source/sid/'drawing.json').exists():
        folder=source/sid
        drawing=json.loads((folder/'drawing.json').read_text())
        drawing.pop('latest_fact_set_id',None)
    if drawing is None:
        raise ValueError('Requested frozen drawing is absent.')
    workspace.store.put('drawing',sid,drawing)
    for name in ('source.dxf', 'source.dwg', 'oda.json', 'scene.json', 'regions.json', 'entities.json', 'render.json'):
        if (folder / name).exists():
            target = workspace.store.artifact(sid, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / name, target)
    actual = hashlib.sha256((folder / 'source.dxf').read_bytes()).hexdigest()
    return dict(snapshot=sid, dxf_sha256=actual,
                regions_sha256=hashlib.sha256((folder/'regions.json').read_bytes()).hexdigest())



def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source-data',type=Path,required=True)
    ap.add_argument('--account-data',type=Path)
    ap.add_argument('--snapshot',default='snap_0a8472b8804178ea90914866')
    ap.add_argument('--related',action='append',default=[])
    ap.add_argument('--question',default='帮我找下外墙做法')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--live',action='store_true')
    ap.add_argument('--baseline',action='store_true')
    ap.add_argument('--repeat',type=int,default=1)
    args=ap.parse_args()
    if (args.live and not args.account_data) or (args.baseline and not args.live):
        ap.error('Live tests require account data; baseline requires --live.')
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.analysis.source_record_workflow import try_run
    from cad_agent.runtimes.registry import run_agent
    from cad_agent.adapters.siwc import SiwcAccount
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    w=Workspace(Settings(data_dir=out/'data',provider='responses_subscription',source_record_workflow=not args.baseline))
    inputs=[seed(args.source_data,w,sid) for sid in [args.snapshot]+args.related]
    save(out,'inputs.json',inputs)
    if args.live:
        w.siwc_account.close();w.siwc_account=SiwcAccount(args.account_data)
        status=w.siwc_account.status()
        if not status['connected'] or status['active_runs']:
            raise ValueError('Account unavailable or busy; no settings changed.')
        save(out,'account-policy.json',{k:status[k] for k in ('connected','max_concurrency','configured_concurrency','active_runs')})
    try:
        for n in range(args.repeat):
            dest=out/str(n+1);dest.mkdir()
            request=ChatRequest(snapshot_id=args.snapshot,question=args.question,mode='agent',runtime='responses_subscription',
                model_profile='siwc_sol',source_record_mode='review' if args.live and not args.baseline else 'agent' if args.baseline else 'auto',
                related_drawings=[dict(snapshot_id=s) for s in args.related])
            session=CadAgentSession(w,request);session.journal.bind('source_record_test_'+str(n+1))
            result=None;error=None;start=time.monotonic()
            try:
                result=run_agent(session) if args.live else try_run(session)
                if result is None:
                    raise ValueError('Workflow handed off: '+str(getattr(session,'source_record_fallback',session.source_record_route)))
                save(dest,'result.json',result)
                (dest/'answer.md').write_text(result['answer'])
            except Exception as exc:
                error=dict(type=type(exc).__name__,code=getattr(exc,'code',None),message=str(exc))
                save(dest,'error.json',error)
            finally:
                for name,value in [('task',session.task.data),('trace',session.trace),('usage',session.usage),
                    ('evidence',session.evidence),('events',session.journal.snapshot()),
                    ('packet',getattr(session,'source_record_packet',{})),('route',getattr(session,'source_record_route',{})),
                    ('runtime-diagnostics',getattr(session,'runtime_diagnostics',{}))]:
                    save(dest,name+'.json',value)
                usage=session.usage
                summary=dict(status='passed' if result else 'failed',elapsed_seconds=round(time.monotonic()-start,3),
                    rounds=len(usage),input_tokens=sum(u.get('prompt_tokens',0) for u in usage),
                    output_tokens=sum(u.get('completion_tokens',0) for u in usage),
                    cached_tokens=sum(u['prompt_cache_hit_tokens'] for u in usage) if all(type(u.get('prompt_cache_hit_tokens')) is int for u in usage) else None,
                    answer_repairs=session.answer_repairs,validation_failures=sum(e['kind']=='validation' and e['status']=='failed' for e in session.journal.events),
                    tool_calls=session.calls,source_records=result.get('source_records',{}) if result else None,error=error)
                save(dest,'summary.json',summary)
                print(json.dumps({k:v for k,v in summary.items() if k!='source_records'},ensure_ascii=False),flush=True)
    finally:
        w.close()

if __name__=='__main__':
    main()
