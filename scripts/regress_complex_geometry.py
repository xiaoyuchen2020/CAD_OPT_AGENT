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
OUT=ROOT/'artifacts/complex-geometry-20261016'
CASES=[
 dict(id='area_ranking',source=0,parent=None,question='一层平面图里的卧室，按毫米测量。请逐间核查轮廓，按候选围合面积从大到小排序，并算出最大与最小的面积差；高亮对应轮廓。不要把候选面积说成确认净面积。',
      acceptance=['实际核查全部匹配候选的边界','排序使用同口径计算值','最大减最小可独立复算','轮廓与计算多边形一致']),
 dict(id='filter_followup',source=0,parent='area_ranking',question='只保留刚才面积最大的两处卧室高亮，列出它们的面积和编号，其他房间取消高亮。若面积相同，按原编号从小到大取前两处。',
      acceptance=['继承上一轮对象和单位','按面积与编号稳定选出两处','实际交付高亮只有两处，不仅文字声称筛选']),
 dict(id='area_bases',source=2,parent=None,question='一层平面图的水泵房按毫米测量。请分别给出外轮廓围合面积、轮廓周长，以及仅扣除已证实穿楼板洞口后的面积；设备、基础和普通闭合线不能自动当洞口扣除。对照图内面积标注检查差异，并高亮外轮廓和实际扣除的对象。',
      acceptance=['面积与周长来自同一有效轮廓','扣除对象有洞口用途依据并避免重复扣除','未确认洞口不能输出假净面积','原图标注与计算口径分开','高亮反映真实扣除集合']),
 dict(id='door_width',source=2,parent=None,question='一层平面图中，水泵房通向天井的门洞有几处？请看局部图核对门洞位置，测量可识别门洞的最小净宽，并与邻近尺寸标注对照，高亮用于测距的两侧边线。本次使用毫米；无法证明直接连通时请指出缺少什么。',
      acceptance=['先验证水泵房与天井的门洞连接关系','门洞净宽不以名称框距离或门扇弧半径代替','测距两侧实体来源可追溯','与尺寸标注比较保持对象关联']),
 dict(id='adjacency',source=1,parent=None,question='二层平面图里各厨房与餐厅是敞开连通、经门洞连通，还是隔墙分开？请结合墙线、开口和局部图逐处判断，高亮用于判断的边界或开口。不要仅凭文字相邻得出连通结论；同时排除装修表中的房间名称。',
      acceptance=['排除表格名称','逐处识别空间对应关系','用墙与开口证据区分三种关系','无法确定的项不能假装确认连通']),
 dict(id='dimension_chain',source=1,parent=None,question='请检查二层平面图下侧的一条水平尺寸链：选取能明确对应同一总尺寸的连续分尺寸，列出原文，计算其和并与总尺寸比较；再检查尺寸界线对应的几何距离，指出是标注差异、图形差异还是证据不足。本次按毫米测量，并定位这条尺寸链。',
      acceptance=['选出同一条链且分尺寸连续无重复','引用分尺寸和总尺寸原文','算术结果独立复算','几何比较使用界线对应端点','不能把任意邻近尺寸加总']),
 dict(id='unnamed_spaces',source=0,parent=None,question='在一层平面图中，不依赖房间名称文字，按几何围合查找空间候选；在已检查范围中选出面积最大的三处，给出候选面积并高亮，说明检查范围和未覆盖部分。家具框、表格格子和图框不能算房间。本次按毫米测量。',
      acceptance=['实际使用几何空间发现而非仅搜索名称','排除家具表格与图框','最大三处范围与已检查集合一致','面积与高亮一致','分页未读不声称整层前三']),
]

def code_hashes():
 return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'cad_agent').rglob('*.py')}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--live',action='store_true');ap.add_argument('--case',action='append');args=ap.parse_args()
 OUT.mkdir(parents=True,exist_ok=True)
 plan=OUT/'plan.json'
 if not plan.exists():plan.write_text(json.dumps({'cases':CASES,'runtime':'responses_subscription','profile':'siwc_sol','fallback':False,'timeout_seconds':240,'grading':'publication != fulfillment != independent room accuracy'},ensure_ascii=False,indent=2))
 if not args.live:print(plan);return
 db=OUT/'data/index.sqlite3'
 if db.exists():
  with sqlite3.connect(db) as c:
   active=[json.loads(v)['id'] for v, in c.execute("select data from records where kind='chat_run'") if json.loads(v).get('status') in ('running','queued')]
  if active:raise RuntimeError('Existing active eval runs; do not open another Workspace: '+str(active))
 w=Workspace(replace(Settings.load(),data_dir=OUT/'data',agent_timeout_seconds=240))
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
