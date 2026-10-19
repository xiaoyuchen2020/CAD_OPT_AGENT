"""Isolated replay/live regression. No source collection is modified.

Live runs use the existing authorized SIWC account through its ordinary adapter;
credentials never enter report files. Offline replay is explicitly not a model eval.
"""
from pathlib import Path
import argparse,json,sqlite3,shutil,time,sys,logging,fcntl,hashlib
logging.getLogger("ezdxf").setLevel(logging.ERROR)
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.room_batch import RoomBatchQuery,RoomBatchService
from cad_agent.analysis.unit_policy import resolve_units,user_unit_policy

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/agent-workflow-20261016'
SOURCES=[('hybrid-retrieval-20261015','snap_aea4896cab02f61ff67d4b6c'),
         ('dynamic-cross-drawing-20261016','snap_05bf3d6a79944998de079061'),
         ('opening-types-build-20261015','snap_e316a1345c7a5db25e19458f')]
CASES=[
 ('original_area',0,'十层平面图中有哪些卧室？卧室房间面积分别是多少？并高亮这些候选。如果没有尺寸标注，默认图纸单位为毫米。'),
 ('original_followup',0,'帮我高亮这些卧室轮廓'),
 ('paraphrase',0,'请把一层平面图里的卧室逐间圈出来，并给出每个圈内的面积；这次按毫米测量。'),
 ('other_drawing',1,'二层平面图里的厨房各有多大？请把对应的房间边线画出来。本次单位按毫米。'),
 ('other_names',2,'一层平面图里水泵房的面积是多少？把房间边界显示出来。本次测量按毫米。'),
]


def prepare(w):
 for folder,sid in SOURCES:
  source=ROOT/'artifacts'/folder/'data'
  with sqlite3.connect(f'file:{source}/index.sqlite3?mode=ro',uri=True) as c:
   d=json.loads(c.execute('select data from records where id=?',(sid,)).fetchone()[0])
  w.store.put('drawing',sid,d)
  dest=w.store.artifact_dir(sid)
  for name in ('source.dxf','scene.json','regions.json','entities.json','render.json'):
   p=source/'drawings'/sid/name
   if p.exists() and not (dest/name).exists():shutil.copy2(p,dest/name)


def main():
 ap=argparse.ArgumentParser();ap.add_argument('--live',action='store_true');ap.add_argument('--case',action='append');args=ap.parse_args()
 w=Workspace(replace(Settings.load(),data_dir=OUT/'data',agent_timeout_seconds=240))
 try:
  prepare(w)
  if not args.live:
   report=[]
   for idx,title,label in [(0,'十层平面图','卧室'),(1,'二层平面图','厨房'),(2,'一层平面图','水泵房')]:
    sid=SOURCES[idx][1];b=RoomBatchService(w,sid).run(RoomBatchQuery(floor_title=title,room_label=label))
    if b['scope']:
     b=resolve_units(w,sid,b,user_unit_policy(CASES[0][2]))
    report.append(dict(snapshot_id=sid,title=title,count=b['total'],scope=bool(b['scope']),unit_resolution=b.get('unit_resolution')))
    print(json.dumps({k:v for k,v in report[-1].items() if k!='unit_resolution'},ensure_ascii=False),flush=True)
   (OUT/'offline-scope.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));return
  account_dir=ROOT/'artifacts/hybrid-retrieval-20261015/data'
  if not (account_dir/'siwc-subscription/accounts.json').exists():account_dir=ROOT/'.data'
  w.siwc_account=SiwcAccount(account_dir)
  report_path=OUT/'live-results.json'
  reports=json.loads(report_path.read_text()) if report_path.exists() else []
  for name,idx,question in CASES:
   if args.case and name not in args.case:continue
   parent=next((r['run_id'] for r in reversed(reports) if r['case']=='original_area' and r['status']=='completed'),None) if name=='original_followup' else None
   if name=='original_followup' and not parent:
    # Preserve the original completed exchange for referent resolution if its
    # new rerun failed. It is context, never substituted for a successful rerun.
    source=ROOT/'artifacts/hybrid-retrieval-20261015/data/index.sqlite3'
    with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as c:
     old=json.loads(c.execute('select data from records where id=?',('chat_d4f49cc7c41843c4979ebbae411cdd2d',)).fetchone()[0])
    w.store.put('chat_run',old['id'],old);parent=old['id']
   request=ChatRequest(snapshot_id=SOURCES[idx][1],question=question,mode='agent',runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False,parent_run_id=parent)
   hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'cad_agent').rglob('*.py')}
   run=w.start_chat(request);print('START',name,run['id'],flush=True)
   (OUT/(run['id']+'-code.json')).write_text(json.dumps(hashes,indent=2))
   while run['status'] in ('queued','running'):
    time.sleep(1);run=w.store.get('chat_run',run['id'])
   result=run.get('result') or {};task=w.store.get('agent_task',run['task_id'])
   batches=list(task['batches'].values());items=[i for b in batches for i in b['items']]
   summary=dict(case=name,question=question,snapshot_id=request.snapshot_id,run_id=run['id'],task_id=task['task_id'],status=run['status'],error=run['error'],
     task_status=task['status'],publication_status=task.get('publication_status'),tools=[dict(tool=t['tool'],status=t['status'],error=t.get('error')) for t in run['trace']],
     candidates=len(items),boundaries=sum(i['highlight']['kind']=='candidate_boundary' for i in items),
     measured=sum(i.get('candidate_area_m2') is not None or i.get('area_m2') is not None for i in items),
     unit_resolutions=[b.get('unit_resolution') for b in batches],answer=result.get('answer'),
     tokens=sum(u.get('total_tokens',0) for u in run['usage']),validation_issue=run.get('validation_issue'))
   reports.append(summary);report_path.write_text(json.dumps(reports,ensure_ascii=False,indent=2))
   (OUT/(run['id']+'.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2))
   print('DONE',name,json.dumps({k:summary[k] for k in ('status','candidates','boundaries','measured','error','tokens')},ensure_ascii=False),flush=True)
 finally:w.close()

if __name__=='__main__':
 OUT.mkdir(parents=True,exist_ok=True)
 with (OUT/'regression.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  main()
