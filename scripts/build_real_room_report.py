"""Build auditable, local-only review bundle after measurement and visual review."""
import html,json,sqlite3
from collections import Counter
from hashlib import sha256
from pathlib import Path

ROOT=Path('artifacts/ar030-real-room-validation-20261015').resolve()
def dump(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
def hashfile(path):return sha256(path.read_bytes()).hexdigest()
def main():
 rows=json.loads((ROOT/'review_points.json').read_text());manifest=json.loads((ROOT/'input_manifest.json').read_text())
 visual=json.loads((ROOT/'visual-review.json').read_text());audits={a['key']:a for a in json.loads((ROOT/'after/audit.json').read_text())}
 db=sqlite3.connect(ROOT/'data/index.sqlite3');drawings={json.loads(r[0])['id']:json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind='drawing'")};db.close()
 output=[]
 for row in rows:
  k=row['key'];r=json.loads((ROOT/'after'/f'{k}.json').read_text())['record'];a=audits[k];v=visual.get(k)
  assert v, f'Missing visual review {k}'
  assert hashfile(ROOT/'after'/a['image'])==a['image_sha256']
  output.append(dict(a,title=row['title'],room_type=row['measurement']['roomType'],review=v,
      header_unit=drawings[r['snapshot_id']]['unit'],applied_unit='mm',unit_assumption=True,
      historical_boundary_is_not_ground_truth=True,measurement_id=r.get('measurement_id'),measurement_path=f'after/{k}.json',
      source_sha256=r['source_sha256'],image='after/'+a['image']))
 source_checks=[]
 for m in manifest:
  p=ROOT/'input/dxf'/f"{m['paper_id']}.dxf";actual=hashfile(p)
  assert actual==m['dxf_sha256']
  source_checks.append(dict(paper_id=m['paper_id'],name=m['source']['name'],sha256=actual,match=True,points=sum(r['paper_id']==m['paper_id'] for r in rows)))
 reference=json.loads((ROOT/'reference-hashes.json').read_text());reference_checks={p:hashfile(Path(p))==h for p,h in reference.items()}
 assert all(reference_checks.values());dump(ROOT/'source-verification.json',source_checks);dump(ROOT/'reference-verification.json',reference_checks)
 before=json.loads((ROOT/'before/summary.json').read_text());after=json.loads((ROOT/'after/summary.json').read_text())
 summary=dict(archived_drawings=len(manifest),tested_drawings=after['drawings'],points=len(output),before=before['statuses'],after=after['statuses'],
   visual_verdicts=dict(Counter(r['review']['verdict'] for r in output)),automatically_accepted_points=0,comparisons=dict(Counter(r['comparison'] for r in output)),
   unit_headers=dict(Counter(d['unit'] for d in drawings.values())),
   maximum_area_rounding_error_m2=max(a.get('area_error_m2',0) for a in output),
   source_hashes_verified=len(source_checks),reference_files_unchanged=len(reference_checks),
   evidence_class='local_real_dxf_geometry_replay_and_visual_triage_not_formal_acceptance',
   important='Candidate area arithmetic and matching historical contours do not prove net boundary semantics. All mm overrides are explicit replay assumptions; 2 drawing headers say inches.')
 dump(ROOT/'validation-summary.json',summary);dump(ROOT/'room-audit.json',output)
 code=[*Path('cad_agent/geometry').glob('*.py'),Path('cad_agent/cad/reader.py'),*Path('scripts').glob('*real_room*.py'),Path('tests/test_room_geometry.py')]
 dump(ROOT/'implementation-hashes.json',{str(p):hashfile(p) for p in code})
 lines=['# AR030 真实图纸：当前 agent 房间几何验证','',f"覆盖 {len(manifest)} 张归档 DXF 中有定位记录的 {after['drawings']} 张，共 {len(output)} 个房间点；其余 3 张没有历史房间定位点，不代表零房间。",'',
 '证据级别：本地真实 DXF 重放、原图叠加与逐点目视排查。没有运行生产规则、模型推理、平台 E2E 或正式验收。历史 AR030 轮廓只作对照。','',
 f"修复前：{before['statuses']}；修复后：{after['statuses']}。目视结论：{summary['visual_verdicts']}。",'',
 '面积均按显式毫米假设复算。22 张输入中，7 张头部为 mm、13 张未声明、2 张为 in；后两张与历史毫米口径冲突，不能静默认为单位已经验证。','',
 '所有候选均独立以平移原点后的鞋带公式复算，洞环单独扣除。轮廓一致、面积一致不等于净边界正确。JSON 中保留每条边、源图元 ID、来源图层、平行面距离、推断闭口和诊断。','',
 '本次修复：局部源几何提取独立于预览截断；显式单位参与曲线细分；裁剪并合并局部重复线；自适应搜索范围；竞争平行墙面降级；混合解析诊断不再崩溃；计算图元支持直线 HATCH/SOLID/TRACE/3DFACE 路径。','',
 '剩余限制：默认全图线条仍可能把门扇、设备或标注小面选为候选。此类候选在逐点表中驳回，不能作为房间净面积。物理构件身份、完整门洞和扣除对象尚未自动解决。','',
 '本批次自动确认净面积为 0 个；36 个“轮廓接近，保留意见”也不是验收通过。目视排查采用每页 8 点的联系表，部分疑难点放大原尺寸检查；它不替代逐边工程确认。','',
 '补充扣柱验证：3554_03_1 消防水泵房显式选择原图唯一 COLU 柱轮廓后，外壳 63.145 − 柱 0.25 = 62.895 m²，仍为 needs_review。修复了有效扣除对象落在较小竞争候选之外时整个请求报错的问题，现在排除该候选并继续检查。主批次无扣柱使用 room-net-v1.4；此补充使用 room-net-void-v1.5。','',
 '[扣柱原图叠加](explicit-column/figures/3554_03_1.png) · [扣柱测量 JSON](explicit-column/measurement.json) · [完整回归日志](pytest-final.txt)','',
 '[打开逐点交互报告](review.html) · [完整机器可读核查](room-audit.json) · [源文件校验](source-verification.json) · [生产参考文件校验](reference-verification.json)','',
 '| 原 DWG 文件 | 点数 | 候选驳回 | 待确认 / 保留意见 | 无轮廓 |','|---|---:|---:|---:|---:|']
 for m in manifest:
  group=[r for r in output if r['paper_id']==m['paper_id']]
  if not group:continue
  counts=Counter(r['review']['verdict'] for r in group)
  lines.append(f"| {m['source']['name']} | {len(group)} | {counts['candidate_rejected']} | {counts['ambiguous']+counts['supported_with_reservations']} | {counts['unresolved']} |")
 (ROOT/'report.md').write_text('\n'.join(lines)+'\n')
 data=json.dumps(output,ensure_ascii=False).replace('</','<\\/')
 page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>真实图纸房间测量核查</title><style>
 body{font:15px system-ui;margin:0;color:#203142;background:#f4f7f8}header{padding:28px 5%;background:#183a45;color:white}h1{font-size:25px}main{max-width:1500px;margin:auto;padding:20px}input,select,button{font:inherit;padding:9px;border:1px solid #aab8be;border-radius:5px}nav{position:sticky;top:0;background:#f4f7f8;padding:12px;z-index:3;display:flex;gap:10px;flex-wrap:wrap}.card{background:white;margin:18px 0;padding:18px;border:1px solid #d8e0e4;border-radius:9px}.card h2{font-size:18px;margin:0 0 8px}.meta{color:#526574;font-size:13px;overflow-wrap:anywhere}.finding{padding:12px;background:#fff3de;border-left:4px solid #c98d30}img{width:100%;height:auto}a{color:#08715c}.numbers{display:flex;gap:22px;margin:12px 0;flex-wrap:wrap}summary{cursor:pointer}code{font-size:12px}header a{color:#bdebdc}</style>
 <header><h1>AR030 真实图纸 · 房间净边界核查</h1><p>22 张有定位记录的真实图纸 · 174 个房间点 · 仅本地几何重放与目视排查</p><p><b>99 处候选驳回 · 36 处接近但有保留 · 36 处边界歧义 · 3 处未解 · 0 处自动确认净面积</b></p><p>候选数值不等于可信净面积。紫线为历史对照，绿线为当前候选，橙线为推断闭口。单位统一显式假设为毫米，2 张 DXF 头部为英寸。</p><a href="report.md">报告</a> · <a href="room-audit.json">逐点 JSON</a> · <a href="validation-summary.json">统计与范围</a> · <a href="explicit-column/figures/3554_03_1.png">补充扣柱叠图</a></header>
 <main><nav><input id="q" placeholder="搜索 DWG、房间、点号或问题" size="40"><select id="v"><option value="">全部目视结论</option value="candidate_rejected">候选驳回</option><option value="supported_with_reservations">轮廓接近，保留意见</option><option value="ambiguous">边界歧义</option><option value="unresolved">无可靠候选</option></select><span id="count"></span></nav><div id="cards"></div></main><script>
 const data=__DATA__;const names={candidate_rejected:'候选驳回',supported_with_reservations:'轮廓接近，保留意见',ambiguous:'边界歧义',unresolved:'无可靠候选'};
 const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 function render(){let q=document.querySelector('#q').value.toLowerCase(),v=document.querySelector('#v').value;let rows=data.filter(r=>(!v||r.review.verdict===v)&&JSON.stringify(r).toLowerCase().includes(q));document.querySelector('#count').textContent=rows.length+' / '+data.length+' 点';document.querySelector('#cards').innerHTML=rows.map(r=>`<article class="card" id="${r.key}"><h2>${esc(r.key)} · ${esc(r.dwg_name)}</h2><div>${esc(r.title)} · ${esc(r.room_type)} · <b>${names[r.review.verdict]}</b></div><div class="numbers"><span>当前候选：${esc(r.candidate_area_m2)} m²</span><span>独立复算：${r.independent_area_m2?.toFixed(6)??'—'} m²</span><span>历史对照：${r.historical_area_m2.toFixed(6)} m²</span><span>工具状态：${r.status}</span></div><p class="finding">${esc(r.review.finding)}</p><div class="meta">源单位：${r.header_unit} → 显式毫米假设；DXF SHA256：${r.source_sha256}</div><a href="${r.image}" target="_blank"><img loading="lazy" src="${r.image}" alt="${r.key} 原图与候选叠加"></a><p><a href="${r.measurement_path}">完整测量证据 JSON</a> · <a href="${r.image}">打开原尺寸叠图</a></p><details><summary>诊断与几何比较</summary><pre>${esc(JSON.stringify({diagnostics:r.diagnostics,comparison:r.comparison,symmetric_difference_m2:r.symmetric_difference_m2,area_error_m2:r.area_error_m2},null,2))}</pre></details></article>`).join('')}
 document.querySelector('#q').addEventListener('input',render);document.querySelector('#v').addEventListener('change',render);render();</script></html>'''
 (ROOT/'review.html').write_text(page.replace('__DATA__',data));print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
