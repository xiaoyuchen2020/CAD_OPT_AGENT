"""Build a delivery report from retained real runs, not publication counts alone."""
from pathlib import Path
import json,sqlite3,html,re
ROOT=Path(__file__).resolve().parents[1];folders=sorted((ROOT/'artifacts').glob('spatial-repair-live-20261016-spatial*'))
latest={};inventory=[]
for folder in folders:
 if not (folder/'results.json').exists():continue
 rows=json.loads((folder/'results.json').read_text())
 for row in rows:
  if row.get('fixture_only'):continue
  inventory.append(row|{'directory':str(folder.relative_to(ROOT))})
  latest[row['case']]=row|{'directory':str(folder.relative_to(ROOT))}
results=[]
for case,row in latest.items():
 folder=ROOT/row['directory'];c=sqlite3.connect('file:'+str(folder/'data/index.sqlite3')+'?mode=ro',uri=True)
 def get(key):return json.loads(c.execute('select data from records where id=?',(key,)).fetchone()[0])
 run=get(row['run_id']);task=get(run['task_id']);res=run.get('result') or {};action=task.get('highlight_action') or {}
 measurements=[];selected=[];classes=[]
 for review in task.get('spatial_results',[]):
  if review['kind']=='connection_review':
   for i in review['items']:
    classes.append({'number':i['number'],'classification':i['decision']['classification']})
    measurements.extend({'number':i['number'],'value':m['value'],'unit':m['unit'],'basis':m['basis']} for m in i['selected_measurements'])
  if review['kind']=='discovery_review':
   selected=[{k:i[k] for k in ('item_id','number','area_m2','perimeter_m','area_annotation_checks') if k in i} for i in review['selected']]
   if action.get('object_ids'):selected=[i for oid in action['object_ids'] for i in selected if i['item_id']==oid]
 annotation_conflicts=[c for i in selected for c in i.get('area_annotation_checks',[]) if c['status']=='unresolved_area_difference']
 gaps=[{'requirement':s['requirement'],'reason':s.get('reason')} for s in task['subtasks'] if s['status']!='completed']
 batches=res.get('room_batches',[]) or list(task['batches'].values())
 batch_shapes=sum(i.get('highlight',{}).get('kind')=='candidate_boundary' for b in batches for i in b['items'])
 if 'items' in action:batch_shapes=sum(i.get('highlight',{}).get('kind')=='candidate_boundary' for i in action['items'])
 results.append(row|dict(measurements=measurements,spatial_classifications=classes,selected_spaces=selected,
   delivered_path_count=len(action.get('paths',[])),delivered_polygon_count=len(action.get('regions',[])),
   delivered_batch_shape_count=batch_shapes,remaining=gaps,provenance=task.get('inherited_from'),annotation_conflicts=annotation_conflicts,
   limitation='已发布、数值复算通过、实际几何交付、语义/全图覆盖分别判断。'))
 c.close()
summary=dict(scope='本轮空间几何修复；历史报告不覆盖',latest_cases=results,
 run_count=len(inventory),model_runs=sum(r['tokens']>0 for r in inventory),zero_token_attempts=sum(r['tokens']==0 for r in inventory),
 reported_tokens=sum(r['tokens'] for r in inventory),inventory=inventory,
 tests=dict(pytest_log='artifacts/spatial-repair-unit-tests-final5.log',frontend_log='artifacts/spatial-repair-ui-tests-final5.log'),
 browser_proof='artifacts/spatial-repair-ui-20261016/verification.json')
(ROOT/'artifacts/spatial-repair-summary-20261016.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
e=html.escape
rows=[]
for r in results:
 actual=[]
 if r['measurements']:actual.append('源门垛间距：'+', '.join(str(m['value'])+' m' for m in r['measurements']))
 if r['selected_spaces']:actual.append('已选空间：'+', '.join(f"#{s['number']} {s['area_m2']} m² / {s['perimeter_m']} m" for s in r['selected_spaces']))
 actual.append(f"实际路径 {r['delivered_path_count']}；空间多边形 {r['delivered_polygon_count']}；名称批次轮廓 {r['delivered_batch_shape_count']}")
 gap='；'.join(g['reason'] or g['requirement'] for g in r['remaining']) or '任务记录没有未完成项；几何候选仍不等于工程验收。'
 if r['annotation_conflicts']:gap+='；原图轮廓内面积文字与计算值不一致，关联及口径尚未解决，不能按任务 completed 字段算净面积准确性通过。'
 link=str(Path(r['directory']).relative_to('artifacts')/(r['run_id']+'.json'))
 rows.append(f'<tr><td>{e(r["case"])}<details><summary>原始问法</summary>{e(r["question"])}</details></td><td>{e(r["status"])} / {e(r["task_status"])}</td><td>{e("；".join(actual))}</td><td>{e(gap)}<p><a href="{e(link)}">原始问答与工具记录</a></p></td></tr>')
body='''<!doctype html><meta charset="utf-8"><title>空间几何修复与实际交付核查</title><style>
body{font:15px/1.7 system-ui,sans-serif;color:#233b30;background:#f6f8f3;margin:32px auto;max-width:1400px;padding:0 24px}h1{font-size:28px}table{border-collapse:collapse;background:white;width:100%}th,td{text-align:left;vertical-align:top;padding:12px;border:1px solid #dbe4d7}th{background:#e7eee2}td:first-child{width:15%}td:nth-child(2){width:12%}td:nth-child(3){width:28%}a{color:#216757}details{margin-top:8px}img{max-width:100%;border:1px solid #dbe4d7}.note{padding:16px;background:#fff3d7;border-left:4px solid #d2a456}code{font-size:13px}</style>
<h1>空间几何修复与实际交付核查 · 2026-10-16</h1>
<p>本轮保留失败原始记录，通过冻结源码执行真实 OpenAI Responses / gpt-6.1-sol 回归。未用脚本答案替代模型。</p>
<div class="note">不能将“已发布”当作通过。门垛间距不等于门框和完成面后的通行净宽；无名称筛选的排名只适用于已检查发现集合。未确认洞口、范围和空间身份仍列为缺口。</div>
<h2>修复的共性根因</h2><ul>
<li>名称定位之后直接逐对核查源墙线、门垛和门符号；排除侧门、窗户和表格名称，输出实际源边线。</li>
<li>无名称发现保留结构线形假设，按结构线条优先检查分块；逐项图像筛选，跨页保留排除结论与排名边界。</li>
<li>从纸空间原生视口映射至模型 WCS，避免在只有标题的纸空间测量建筑；投影和单位分开。</li>
<li>同源已发布父会话继承已审查集合与审查来源；筛选替换实际轮廓，同时替换前端行。</li>
<li>已生成候选的看图决策保留有限预算；概览图与审查联系表分开计数；前端聚焦实际交付几何。</li></ul>
<h2>最新逐项结果</h2><table><thead><tr><th>场景</th><th>发布 / 任务状态</th><th>实际几何交付</th><th>保留缺口与证据</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<h2>自动化检查</h2><p>最终源码：Python 743 passed、4 skipped；前端 Node 33 passed。跨图纸最新记录来自不同修复阶段；并非声称最终版本一次性重跑全部场景。</p><h2>独立核查与浏览器证据</h2><p>门垛：从原始 DXF 对应线段/直线填充边界独立复算最短距离；空间：由所交付顶点独立复算面积、周长与排名。浏览器 SVG 坐标与保存结果逐条比较，允许闭环绕向归一化。</p>
<p><a href="spatial-repair-ui-20261016/verification.json">浏览器坐标核对记录</a> · <a href="spatial-repair-summary-20261016.json">完整运行清单及用量</a></p>
<img src="spatial-repair-ui-20261016/doors-final.png" alt="三组餐厨门垛实测及实际高亮"><p>三组门垛间距 2.1、1.6、1.5 m；浏览器实际绘制九条源边线和测距线。</p>
<img src="spatial-repair-ui-20261016/unnamed-focused.png" alt="无名称空间轮廓高亮"><p>空间高亮与面积、外环周长对应；已核对三个多边形的源坐标。</p>'''
body+='''<img src="spatial-repair-ui-20261016/followup.png" alt="跨轮筛选实际只留一处"><p>跨轮从三处缩为一处，原编号 6、10.3284 m²、12.88 m；浏览器只有一个源坐标匹配的多边形。</p><img src="spatial-repair-ui-20261016/annotation-crosscheck.png" alt="11799原图面积差异和实际空间轮廓"><p>11799：两个多边形实际交付；原图面积文字与几何结果冲突保留，未据此认定净面积。</p>'''
body+='''<img src="spatial-repair-ui-20261016/d5-spaces.png" alt="D5跨图纸无名称空间候选"><p>D5 实际两个屋面围合候选：19.965 / 19.3109 m²；不是确认的室内房间。浏览器顶点已核对。</p>'''
body+=f'<p>本轮保留 {len(inventory)} 次执行记录；其中 {summary["model_runs"]} 次实际有模型用量，{summary["zero_token_attempts"]} 次为 0 Token。接口报告合计 {summary["reported_tokens"]:,} Token，不折算费用或订阅额度。</p>'
(ROOT/'artifacts/spatial-repair-final-report-20261016.html').write_text(body)
print('Wrote report for',len(results),'latest cases; actual model runs',summary['model_runs'])
