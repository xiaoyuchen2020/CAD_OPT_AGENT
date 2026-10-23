"""Isolated real-drawing UI fixture; automatic source mode, zero model calls."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from scripts.test_source_record_workflow import seed


def main():
    p=argparse.ArgumentParser();p.add_argument('--source-data',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--serve',action='store_true');p.add_argument('--port',type=int,default=8769)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    app=create_app(Settings(data_dir=a.output/'data',provider='responses_subscription',source_record_workflow=True))
    w=app.state.workspace;sid='snap_0a8472b8804178ea90914866';seed(a.source_data,w,sid)
    record=w.start_chat(ChatRequest(snapshot_id=sid,question='帮我找下外墙做法',mode='agent',runtime='responses_subscription',model_profile='siwc_sol'))
    for _ in range(1000):
        run=w.store.get('chat_run',record['id'])
        if run['status'] not in ('queued','running'):break
        time.sleep(.05)
    result=run.get('result') or {}
    checks=dict(run_id=run['id'],status=run['status'],groups=len(result.get('source_records',{}).get('groups',[])),model_calls=len(result.get('usage',[])))
    (a.output/'validation.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));print(json.dumps(checks),flush=True)
    if checks['status']!='completed':w.close();raise SystemExit(1)
    if a.serve:
        import uvicorn
        uvicorn.run(app,host='127.0.0.1',port=a.port)
    else:w.close()
if __name__=='__main__':main()
