"""Predeclared complex geometry eval on the three user-authorized drawings.

This runner does not modify production code/data or substitute scripted answers.
Publication is recorded separately from fulfillment and geometric accuracy.
"""
from pathlib import Path
import argparse,fcntl,hashlib,json,logging,sqlite3,sys,time
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.contracts import ChatRequest
from regress_agent_workflow import SOURCES,prepare,ROOT
logging.getLogger('ezdxf').setLevel(logging.ERROR)
OUT=ROOT/'artifacts/complex-repair-extensions-20261016'
SOURCES.extend([('opening-types-build-20261015','snap_77b384ae399a2c5c69f5a23a'),
                ('ar030-real-room-validation-20261015','snap_3dad7f8e24f6b0d90a08acfd')])
CASES=[
 dict(id='mean_rephrase',source=0,parent=None,question='请核查一层所有卧室的围合边界，列出各自候选面积，求这些候选的平均面积。本次按毫米，结果只代表围合口径。',acceptance=['逐间轮廓及面积','同口径平均值由本地程序计算','不把平均值冒充净面积']),
 dict(id='ascending_followup',source=0,parent='mean_rephrase',question='接着只显示面积最小的三处，按从小到大排列，面积一样的用原编号升序，其余高亮清掉。',acceptance=['继承原批次与单位','正确反向排序与稳定编号','画面实际只保留三处']),
 dict(id='other_dimension',source=3,parent=None,question='请在 D2#D3#楼二层平面图下方核对一条水平连续尺寸链：列出分段与总尺寸，并计算差值，再对照尺寸界点的几何长度。本次按毫米，显示实际核查位置。',acceptance=['新图纸范围有依据','尺寸链连续并且总分对应','实际计算差值及几何距离','定位有来源']),
 dict(id='other_unnamed',source=4,parent=None,question='在 I-A区地下室一层平面图选择有明确范围的一块局部区域，不使用房间名称作为识别条件，查找围合空间候选。核查其中面积最大的两处，给出围合面积和外环周长，显示候选边界；明确只比较已核查集合。本次按毫米。',acceptance=['新图纸实际几何发现','局部范围声明','核查最大两处而不是按名称枚举','面积周长与边界一致']),
]

def code_hashes():
 return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'cad_agent').rglob('*.py')}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--live',action='store_true');ap.add_argument('--case',action='append');args=ap.parse_args()
 OUT.mkdir(parents=True,exist_ok=True)
 plan=OUT/'plan.json'
 if not plan.exists():plan.write_text(json.dumps({'cases':CASES,'runtime':'responses_subscription','profile':'siwc_sol','fallback':False,'timeout_seconds':360,'grading':'publication != fulfillment != independent room accuracy'},ensure_ascii=False,indent=2))
 if not args.live:print(plan);return
 db=OUT/'data/index.sqlite3'
 if db.exists():
  with sqlite3.connect(db) as c:
   active=[json.loads(v)['id'] for v, in c.execute("select data from records where kind='chat_run'") if json.loads(v).get('status') in ('running','queued')]
  if active:raise RuntimeError('Existing active eval runs; do not open another Workspace: '+str(active))
 w=Workspace(replace(Settings.load(),data_dir=OUT/'data',agent_timeout_seconds=360,agent_max_tool_calls=28))
 try:
  prepare(w)
  account=ROOT/'artifacts/hybrid-retrieval-20261015/data'
  if not (account/'siwc-subscription/accounts.json').exists():account=ROOT/'.data'
  w.siwc_account=SiwcAccount(account)
  reports_path=OUT/'results.json';reports=json.loads(reports_path.read_text()) if reports_path.exists() else []
  for case in CASES:
   if args.case and case['id'] not in args.case:continue
   parent=next((r for r in reversed(reports) if r['case']==case['parent']),None) if case['parent'] else None
   if case['parent'] and (not parent or parent['status']!='completed'):
    reports.append(dict(case=case['id'],status='blocked_dependency',reason='Parent answer was not published; no fabricated conversation context.'))
    reports_path.write_text(json.dumps(reports,ensure_ascii=False,indent=2));print('BLOCKED',case['id'],flush=True);continue
   sid=SOURCES[case['source']][1];request=ChatRequest(snapshot_id=sid,question=case['question'],mode='agent',runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False,parent_run_id=parent['run_id'] if parent else None)
   before=code_hashes();run=w.start_chat(request);print('START',case['id'],run['id'],flush=True)
   (OUT/(run['id']+'-code.json')).write_text(json.dumps(before,indent=2))
   while run['status'] in ('queued','running'):
    time.sleep(1);run=w.store.get('chat_run',run['id'])
   result=run.get('result') or {};task=w.store.get('agent_task',run['task_id']);items={i['region_id']:i for b in task['batches'].values() for i in b['items']}
   summary=dict(case=case['id'],question=case['question'],acceptance=case['acceptance'],run_id=run['id'],task_id=run['task_id'],snapshot_id=sid,status=run['status'],error=run['error'],task_status=task['status'],publication_status=task.get('publication_status'),
     answer=result.get('answer'),tools=[dict(tool=t['tool'],status=t['status'],error=t.get('error')) for t in run['trace']],
     batch_candidates=len(items),batch_boundaries=sum(i.get('highlight',{}).get('kind')=='candidate_boundary' for i in items.values()),batch_areas=sum(i.get('candidate_area_m2') is not None or i.get('area_m2') is not None for i in items.values()),
     tokens=sum(u.get('total_tokens',0) for u in run['usage']),elapsed_seconds=result.get('elapsed_seconds'),fallback_used=result.get('fallback_used'),code_changed_during_run=before!=code_hashes(),validation_issue=run.get('validation_issue'))
   (OUT/(run['id']+'.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2));reports.append(summary);reports_path.write_text(json.dumps(reports,ensure_ascii=False,indent=2))
   print('DONE',case['id'],json.dumps({k:summary[k] for k in ['status','task_status','batch_candidates','batch_boundaries','batch_areas','tokens','error','code_changed_during_run']},ensure_ascii=False),flush=True)
 finally:w.close()

if __name__=='__main__':
 OUT.mkdir(parents=True,exist_ok=True)
 with (OUT/'runner.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);main()
