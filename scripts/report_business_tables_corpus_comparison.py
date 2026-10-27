"""Reviewable local before/after report; links to unmodified source evidence."""
from pathlib import Path
import html,json,re
from compare_business_tables_corpus import ROOT,BEFORE,AFTER,AUDIT,read,save,fields_and_rows
H=lambda x:html.escape(str(x))
def link(path,label):return f'<a href="{H(path)}">{H(label)}</a>'
def main():
 s=read(AFTER/'summary.json');cmp=read(AFTER/'reference-comparison.json');manifest=read(AFTER/'manifest.json')
 issues=read(BEFORE/'reviewed-issues.json')
 fixes=[
  ('已修复异常传播','按单表隔离网格异常，保留安全上限和具体诊断，其他候选继续发现。坏网格本身仍可能不可恢复。'),
  ('已修复重点样本','楼号、子项、主要功能等按组合表头映射；同时要求建筑属性列，排除仅有楼号/编号的材料及设备表。'),
  ('已修复重点样本','依据同一单元格中 m 与上标 2 的位置和字高恢复 m²；原文、源实体和归一记录保留。'),
  ('已修复重点样本','保留内嵌独立表格，允许父实例边框和子实例行线组合；相同范围优先使用更具体的子实例，避免整页吞并小表和祖先实例引入底图。'),
  ('部分修复，保留身份限制','住宅概况表的字段表头、单位行和5条数据行恢复；a楼与2#楼的跨行身份关系没有填补。'),
  ('已修复重点样本','组合识别位置、车辆类型和停车标签，5个遗漏停车分项恢复；不合计分项、不把地上地下解释成计容不计容。'),
  ('已修复重点样本','在重复表格边线和主文字图层证据充分时排除外来小号高程；绿地率限定中的2.88与标题区1.96不再混入。'),
  ('扩展覆盖，仍有边界','新增单源实体内直接指标声明读取，保留标签前的范围文字、单位和原句；屋顶可绿化/设计绿化、标准/微型车位不会因字段名相同被误判为冲突。跨实体断开的标签、数值、单位与复杂控制句仍未全覆盖。'),
  ('仍阻塞','两张DWG再次调用同一内网服务仍失败，未进入两个方法。服务只给出笼统ODA解析失败，不能据此判定文件损坏。')]
 reviewed=[]
 for issue,(state,action) in zip(issues,fixes):reviewed.append(dict(issue,repair_status=state,repair=action))
 save(AFTER/'issue-comparison.json',reviewed)
 remaining=[
  dict(id='R1',kind='conversion_blocker',cases=['17-0f4bab48','23-dabcd356'],description='两张DWG持续ODA解析失败。补充本机LibreDWG诊断：17号可生成DXF并返回7条指标，但报告CRC异常及未知对象跳过，不能确认原DWG完整性，未纳入正式通过；23号解码失败。需要转换服务原生日志/支持或从CAD重新保存的可靠版本。'),
  dict(id='R2',kind='source_structure_boundary',cases=['22-c8a9d664'],description='多层表头已恢复，但原图2#楼与a楼/b-e楼的跨行身份关系不在可靠矩形合并格内。保留原始身份单元格，未向空白格填充2#楼。'),
  dict(id='R3',kind='coverage_boundary',cases=['02-d6318601','12-37b0faef','21-0942faf4','22-c8a9d664','32-a0023dfe'],description='标签、值、单位跨不同文字实体断行，以及复杂控制句/停车总量句仍有遗漏；无候选不能认定无数据。例如汇能B段落地上建筑面积143373.7的㎡在下一实体。单写占地面积仍保留为未映射列，不等同基底面积。'),
  dict(id='R4',kind='source_disagreement',cases=['05-5aab4a78','21-0942faf4','24-4e1dc15b'],description='原图存在不同声明/精度：用地5870与5880；总建筑面积188875.7与188875.69；地下室13654.73与13667.13。按各来源、口径独立保留，未选择真值或自动跨表合并。'),
  dict(id='R5',kind='unresolved_candidates',cases=[],description='16个候选仍未解析，涉及标题块、说明框、空白模板或破损网格，返回诊断或unresolved_rows；3张原生遍历不完整。49条标题范围提醒只是待审线索，不能等同漏提。全图语义召回率没有独立穷尽标准答案，不能声称全部字段100%完整。')]
 save(AFTER/'remaining-issues.json',remaining)
 rows=[];prose_review=[]
 for c in manifest:
  cid=c['case_id'];a=read(AFTER/'cases'/cid/'result.json');b=read(BEFORE/'cases'/cid/'result.json')
  checks=[x for x in cmp['after']['checks'] if x['case_id']==cid]
  cells=[H(cid),H(c['name']),H('成功导入' if a.get('snapshot_id') else '转换失败')]
  for kind,key in [('planning_metrics','field_count'),('building_schedule','record_count')]:
   cells.append(H(f"{b['methods'].get(kind,{}).get(key,0)} → {a['methods'].get(kind,{}).get(key,0)}"))
  cells.append(H(f"{sum(x['matched'] for x in checks)}/{len(checks)}" if checks else '未建立逐格基准'))
  cells.append(link('cases/'+cid+'/result.json','本次结果')+' · '+link('../'+BEFORE.name+'/cases/'+cid+'/result.json','修复前'))
  rows.append('<tr>'+''.join('<td>'+x+'</td>' for x in cells)+'</tr>')
  _,_,pages=fields_and_rows(AFTER,cid,'planning_metrics')
  for page in pages:
   if page.get('source_kind')!='explicit_metric_paragraph':continue
   for f in page['fields']:
    prose_review.append(dict(case_id=cid,field=f['field'],value=f['value'],unit=f['unit'],state=f['state'],
      label=f['source_labels'],clause=f['qualifiers'],source_character_span=f['source_character_span'],evidence=f['evidence'],
      verification='Native quote binding verified by batch runner; clause/context inventory reviewed. This is output agreement review, not exhaustive recall gold.'))
 save(AFTER/'paragraph-output-review.json',prose_review)
 cards=[]
 for item in reviewed:
  refs=' · '.join(link('../'+BEFORE.name+'/'+p,'原始证据'+str(i+1)) for i,p in enumerate(item['evidence']))
  images=''.join('<details><summary>查看原生文字/线框回看</summary><img loading="lazy" src="../'+BEFORE.name+'/'+H(p)+'"></details>' for p in item['images'][:1])
  cards.append(f'<article><h3>{H(item["id"])} · {H(item["title"])}</h3><p><b>{H(item["repair_status"])}</b> — {H(item["repair"])}</p><p class="muted">修复前：{H(item["observed"])}</p><p>{refs}</p>{images}</article>')
 metrics=[]
 for kind,label in [('planning_metrics','指标：有返回的图纸'),('building_schedule','一览表：有返回的图纸')]:
  metrics.append(f'<div><span>{label}</span><strong>{s["before"]["methods"][kind]["nonempty_files"]} → {s["after"]["methods"][kind]["nonempty_files"]}</strong></div>')
 metrics.append(f'<div><span>已复核检查点匹配</span><strong>{cmp["before"]["matched"]} → {cmp["after"]["matched"]} / {cmp["after"]["total"]}</strong></div>')
 metrics.append(f'<div><span>完成导入 / 全部图纸</span><strong>{s["after"]["completed"]} / 39</strong></div>')
 check_errors=[x for x in cmp['after']['checks'] if not x['matched']]
 code_files=['cad_agent/analysis/business_tables.py','cad_agent/analysis/business_metric_prose.py','cad_agent/cad/ruled_tables.py','cad_agent/semantics/tables.py','tests/test_business_table_corpus_repairs.py','docs/business-table-extraction.md']
 validation=[]
 for name in ['pytest-full.log','pytest-current-recheck.log','pytest-final-targeted.log']:
  p=AUDIT/name
  if p.exists():
   text=p.read_text();last=text.splitlines()[-1] if text.splitlines() else '运行中'
   validation.append('<li>'+H(name)+'：'+H(last)+' · '+link('../'+AUDIT.name+'/'+name,'日志')+'</li>')
 doc='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>39张建筑设计说明 · 两个提取方法修复前后对比</title><style>
body{font:15px/1.7 system-ui,-apple-system,"PingFang SC",sans-serif;background:#f4f6f8;color:#17242e;margin:0}main{max-width:1240px;margin:auto;padding:32px}h1{font-size:30px;line-height:1.3}h2{margin-top:32px}h3{margin:0 0 8px;font-size:18px}a{color:#17678b}article,.panel{background:white;border:1px solid #d9e1e7;border-radius:12px;padding:20px;margin:16px 0}.muted{color:#586977}.warning{background:#fff6df;border-left:5px solid #b57a16;padding:16px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stats div{background:#173c4d;color:white;padding:18px;border-radius:12px}.stats span{display:block;font-size:13px}.stats strong{display:block;font-size:28px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;background:white;min-width:960px}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #dce3e8}th{background:#dce9ee}td:nth-child(2){max-width:350px;overflow-wrap:anywhere}input{padding:10px;width:95%;max-width:600px;border:1px solid #aebec8;border-radius:6px}img{max-width:100%;background:white}summary{cursor:pointer}code{overflow-wrap:anywhere}@media(max-width:750px){main{padding:16px}.stats{grid-template-columns:repeat(2,1fr)}h1{font-size:25px}}
</style><main><p class="muted">CAD Opt Agent · 2026-10-27 · 本地原生方法回归</p><h1>39张建筑设计说明：修复前后对比</h1>'''
 doc+='<p>通用修复覆盖候选异常隔离、嵌套线框、父子实例、多层表头、分离单位、字段别名、停车分类、底图干扰和明确段落声明。代码不包含图纸文件名、案例坐标或预期数值分支。</p><section class="stats">'+''.join(metrics)+'</section>'
 doc+='<p class="warning"><b>结论范围：</b>39张均尝试测试；转换成功的图纸重新原生导入并遍历两个方法的全部候选和分页，逐条验证来源。37张复用哈希核验的转换文件，未复用旧提取结果；2张重试内网转换。没有模型调用。匹配率只针对人工建立的复核子集，不是全图召回率；无候选、未知和转换失败均不计为准确性通过。</p>'
 doc+=f'<section class="panel"><h2>核对证据</h2><p>建筑基准覆盖8张表、46条原表记录、268个目标单元格；另有24项重点指标检查。不同表中的同名建筑不合并为唯一建筑，空白与“--”分别核对。住宅概况表额外保留的2条父级身份单元格（2#楼及空白格）不计入这268项，跨行归属列为剩余问题。发现{len(check_errors)}项未匹配检查点、{len(s["unreviewed_building_rows"])}条未纳入此基准的建筑输出。</p><p>'+link('reference-comparison.json','逐字段前后对比')+' · '+link('../'+AUDIT.name+'/reviewed-reference.json','人工来源基准')+' · '+link('paragraph-output-review.json',f'段落返回核对明细（{len(prose_review)}项）')+' · '+link('summary.json','汇总与版本核验')+' · '+link('../'+AUDIT.name+'/source-text-review.txt','全批次原生文字检查线索')+'</p><p>原生回看图由源文字与线段重绘，字体排版近似，源DWG/DXF及实体证据优先。</p></section>'
 counts=[]
 for kind,label in [('planning_metrics','指标字段记录'),('building_schedule','建筑行内字段')]:
  a=s['after']['methods'][kind];b=s['before']['methods'][kind]
  counts.append('<li>'+H(label)+f'：{b["fields"]} → {a["fields"]}；状态 '+H(a['field_states'])+'</li>')
 counts.append('<li>建筑原表行：'+H(str(s['before']['methods']['building_schedule']['records'])+' → '+str(s['after']['methods']['building_schedule']['records']))+'；这些是不同原表的记录，不能视为去重后的建筑数量。</li>')
 doc+='<section class="panel"><h2>输出数量与状态</h2><ul>'+''.join(counts)+'</ul><p>指标记录包含不同范围的原文声明、屋顶绿化、人防面积、停车分项及重复声明，不直接合计。observed 表示原文标签、数值及单位可追溯；missing 为空白原格，ambiguous 包含原图“--”。这些状态不确认工程事实真值。</p></section>'
 controls=[]
 for kind,label in [('planning_metrics','尚桥头经济技术指标'),('building_schedule','汇能B原6行一览表')]:
  control=read(AFTER/'controls'/kind/'result.json')
  assert control['passed']
  controls.append('<li>'+H(label)+'：通过，'+H(control['counts'])+' · '+link('controls/'+kind+'/result.json','结果与来源')+'</li>')
 doc+='<section class="panel"><h2>原有两张对照图回归</h2><ul>'+''.join(controls)+'</ul><p>使用原验证样本的DXF哈希与人工答案，在最终冻结代码下重新导入执行。原6行对照与本批次新增发现的7行四期表分别核对。</p><p>'+link('../'+BEFORE.name+'/cases/21-0942faf4/review/additional-schedule.png','新增7行四期表原生回看')+'</p></section>'
 doc+='<h2>逐图结果</h2><p>指标列为字段记录数；一览表列为原表数据行数。同一数据可能在不同来源重复声明，不能直接相加。</p><input id="filter" placeholder="筛选文件名、编号或结果"><div class="scroll"><table><thead><tr>'+''.join('<th>'+x+'</th>' for x in ['编号','文件','导入','指标字段 前→后','建筑行 前→后','基准检查','证据'])+'</tr></thead><tbody id="results">'+''.join(rows)+'</tbody></table></div><p>'+link('file-comparison.csv','下载逐图对比CSV')+'</p>'
 doc+='<h2>原9类问题的修复情况</h2>'+''.join(cards)
 doc+='<h2>剩余问题</h2><p>下列编号是已发现的示例；完整逐图自动诊断保留在各图结果中。未建立穷尽答案的图纸不推定为完整通过。</p>'+''.join('<article><h3>'+H(r['id'])+' · '+H(r['kind'])+'</h3><p>'+H(r['description'])+'</p><p class="muted">'+H('；'.join(cid+' '+next(c['name'] for c in manifest if c['case_id']==cid) for cid in r['cases']))+'</p></article>' for r in remaining)
 doc+='<section class="panel"><h2>转换失败的补充本地诊断</h2><p>使用已安装LibreDWG对两张失败原图做只读转换，源文件哈希不变。17号输出DXF后执行两个方法得到7条指标、0条建筑记录；转换日志有CRC异常、162个未知类对象及162条对象跳过警告，不能据此确认全图完整。23号返回解码失败。未启用有损转换的自动回退，补充结果不混入正式39张对比统计。</p><p>'+link('../'+AUDIT.name+'/local-conversion-diagnostics/17-0f4bab48/method-probe.json','17号方法诊断')+' · '+link('../'+AUDIT.name+'/local-conversion-diagnostics/17-0f4bab48/libredwg.log','17号转换日志')+' · '+link('../'+AUDIT.name+'/local-conversion-diagnostics/23-dabcd356/libredwg.log','23号转换日志')+'</p></section>'
 doc+='<h2>代码与回归</h2><p>'+ ' · '.join(link('../../'+p,Path(p).name) for p in code_files)+'</p><ul>'+''.join(validation)+'</ul><p>全库首轮2046项通过、28项失败、11项跳过；28项失败受沙箱回环端口/分词资源限制，允许所需环境访问后当前代码28项全部通过。最终变更另跑59项针对性回归。该记录是分层复核，不表述为一次全库运行全绿。修改前隔离基线同样复核，未将基线静态资源缺失计作产品缺陷。</p><p>'+link('../'+AUDIT.name+'/conversion-diagnostics-authorized.log','转换失败任务诊断')+' · '+link('issue-comparison.json','问题前后对照JSON')+' · '+link('remaining-issues.json','剩余问题JSON')+' · '+link('code-manifest.json','冻结代码哈希')+' · '+link('manifest.json','39张原图哈希')+' · '+link('validation-provenance.json','复现命令与测试用量')+' · '+link('remaining-diagnostic-files.json','剩余诊断逐图清单')+' · '+link('report-validation.json','报告文件校验')+'</p>'
 doc+='<p class="muted">原图哈希复核未改变；测试代码快照哈希校验通过。工作区相对冻结代码差异：'+H(s['current_workspace_code_drift'])+'</p></main><script>document.getElementById("filter").addEventListener("input",e=>{const q=e.target.value.toLowerCase();document.querySelectorAll("#results tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script></html>'
 (AFTER/'report.html').write_text(doc)
 print(AFTER/'report.html')
if __name__=='__main__':main()
