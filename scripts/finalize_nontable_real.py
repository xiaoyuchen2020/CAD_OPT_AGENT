"""Freeze reviewed real-case findings; no model, drawing, or production mutations."""
import json,html,hashlib,sqlite3,shutil
from pathlib import Path
from collections import Counter,defaultdict
from scripts.report_nontable_real import records
from cad_agent.extensions.table_recovery.bundle import digest
from scripts.validate_nontable_real import OUT,ROOT,PRIOR,dump,sha

NOTES={
'11816-region-1':('excluded_hidden_layer','四条边均来自冻结的 BEAM 图层，正常原图为空；不能认定为可见对象恢复。几何通过不计收益。'),
'11816-region-2':('excluded_hidden_layer','同为冻结 BEAM 图层，正常原图不可见；排除有效收益。'),
'11816-region-3':('visible_geometry_compatible','可见矩形及“战时饮用水箱”原文吻合，四边和文本对应正确；只确认图形轮廓与原文，不将 6.5 m² 解释为房间净面积。原组件已能计算。'),
'11816-room-3552_01_1':('rejected_room_candidate','橙色原组件候选只占公变房间中部，上下沿跟随贯穿线而非完整围合；40.8792 m² 不能当作完整房间净面积。动态简单闭环代码对 584 条源线段未返回候选，未修复此失败。'),
'11816-text-control':('visible_text_compatible','“3#-8#架空层”与原图完整实体原文相符；现有来源上下文已覆盖。'),
'11816-visible-region-1':('visible_geometry_entry_gap_only','可见的四线狭长闭合轮廓与候选重合，原生几何面积约 0.285794 m²；基础面积接口拒绝多实体，围合识别已有对应轮廓（约 0.285840 m²，毫米网格量化），不计组件整体新增收益，也不根据 WALL 图层认定墙体。'),
'11816-visible-text-control':('duplicate_visible_text_control','与 11816-text-control 是同一快照同一范围的重复回归，不能算新增样本。'),
'11821-region-1':('visible_geometry_unknown_units','四条原生边围成的矩形与候选一致；源单位 unknown，720000 图纸单位平方不能换算或当作物理面积恢复。构件身份未确认。'),
'11821-text-control':('visible_text_compatible','电动自行车车位标注中的“800*2000”原文一致；现有原文已可读取，不新增识别。'),
'11856-blocked-control':('input_incomplete','原图范围是停车及设备空间，含大量相交构件与文字，不是单个闭合对象；源中存在无法定位的 ACAD_PROXY_ENTITY，无法证实完整输入，四类方法拒绝有依据。'),
'sample-60-region-1':('visible_geometry_unknown_units','原生闭合四边形与候选对应；缺少工程对象身份和可靠单位，仅作图纸单位几何核验，不计新增恢复。'),
'sample-60-region-2':('visible_geometry_unknown_units','第二处原生闭合四边形与候选对应；单位 unknown，未赋予房间或构件身份。'),
'sample-60-region-3':('visible_geometry_unknown_units','单一闭合多段线的边界与候选重合；已有几何读取能提供源路径，单位问题仍未解决。'),
'sample-60-region-4':('visible_geometry_unknown_units','另一单一闭合多段线与候选对应，不能由原始数值推断物理面积或构件身份。'),
'sample-60-text-control':('visible_text_compatible','预埋钢板附近“8X80X80@1500”实体原文一致。相邻“-”来自另一文字实体，本方法未拼接为完整工程标注；现有文字框对末尾字符较紧，未将框精度当新增成果。'),
}
LABELS={'text_spans':'原文片段','segment_measurements':'原生线段长度','segment_distances':'线段最短间距','closed_contour':'闭合轮廓与面积'}

def main():
    rows=records();grouped=defaultdict(list)
    for f,d in rows:grouped[d['case']['case_id']].append((f,d))
    assert set(grouped)==set(NOTES)
    visual=[];sections=[];summary_rows=[]
    for key,group in grouped.items():
        verdict,note=NOTES[key];case=group[0][1]['case'];meta=json.loads((OUT/(key+'-render-provenance.json')).read_text())
        inspected=[key+'-context.png']
        if (OUT/(key+'-context-contrast.png')).exists():inspected.append(key+'-context-contrast.png')
        for f,d in group:
            if (OUT/(f.stem+'-overlay.png')).exists():inspected.append(f.stem+'-overlay.png')
        if key=='11816-room-3552_01_1':inspected.append(key+'-baseline-overlay.png')
        visual.append(dict(case_id=key,reviewer='assistant',user_requested_visual_review=True,verdict=verdict,note=note,
            inspection_scope='context and representative source drawings; all available overlays reviewed in three contact sheets; arithmetic independently checked',
            images={n:sha(OUT/n) for n in inspected},source_sha256=case['source']['sha256'],human_signoff=False,engineering_acceptance=False,new_overall_recovery=False))
        name=case['source']['name'] if not key.startswith('sample-60') else '地上建筑设计总说明_人工改错后t3.dwg（case60）'
        images=f'<figure><img loading="lazy" src="{key}-context-contrast.png"><figcaption>源 DXF 周边上下文；单色高对比辅助视图，粉色仅为试验范围。<a href="{key}-context.svg">原色 SVG</a></figcaption></figure>'
        if key=='11816-room-3552_01_1':images+=f'<figure><img loading="lazy" src="{key}-baseline-overlay.png"><figcaption>橙色为现有房间组件候选；动态方法未返回候选。</figcaption></figure>'
        trs=[]
        for f,d in group:
            passed=bool(d.get('result'));origin='冻结生成代码重放';r=d['run'];v=d.get('result',{}).get('validation',{});items=v.get('items',[])
            item=dict(case=key,method=d['method'],status=r['status'],raw_structural_pass=passed,
                visual_verdict=verdict,overall_incremental_recovery=False,baseline_unchanged=d['baseline_unchanged'],
                baseline_sha256=d.get('baseline_before_sha256'),source_sha256=case['source']['sha256'],run_id=r['run_id'],
                code_sha256=r['attempts'][0]['code_sha256'] if r['attempts'] else None,model_calls=0,item_count=len(items))
            summary_rows.append(item)
            val='、'.join(i['quote'] for i in items) if d['method']=='text_spans' else (f"{items[0]['area']:.8g} 图纸单位²" if d['method']=='closed_contour' and items else f'{len(items)} 项')
            links=f'<a href="{f.name}">原始记录</a>'
            if (OUT/(f.stem+'-overlay.png')).exists():
                links+=f' · <a href="{f.stem}-source-contrast.png">源图高对比</a> · <a href="{f.stem}-overlay.png">叠加</a>'
            else:links+=' · 无可复核候选叠加'
            trs.append(f'<tr><td>{LABELS[d["method"]]}</td><td>{r["status"]}</td><td>{html.escape(val)}</td><td>{links}</td></tr>')
        rep=next((f for f,d in group if d['method']=='closed_contour' and (OUT/(f.stem+'-overlay.png')).exists()),None)
        if rep:images+=f'<div class="pair"><figure><img loading="lazy" src="{rep.stem}-source-contrast.png"><figcaption>原生图元，单色辅助显示</figcaption></figure><figure><img loading="lazy" src="{rep.stem}-overlay.png"><figcaption>候选轮廓叠加</figcaption></figure></div>'
        sections.append(f'<section id="{key}"><h2>{key} · {html.escape(name)}</h2><p><b>{note}</b></p><p>范围：<code>{case["region_bbox"]}</code>；图层不可见源实体 {sum(not v["visible"] for v in meta["source_subset"]["visibility"])} 个。</p>{images}<table><tr><th>方法</th><th>结构状态</th><th>输出</th><th>证据</th></tr>{"".join(trs)}</table></section>')
    dump(OUT/'assistant-visual-review.json',dict(records=visual,reviewed_case_labels=len(visual),automatic_adoption=False,automatic_registration=False))
    sources=defaultdict(list)
    for f,d in rows:sources[d['case']['source']['sha256']].append(d)
    source_rows=[]
    for source_sha,ds in sources.items():
        name=ds[0]['case']['source']['name']
        if ds[0]['case']['case_id'].startswith('sample-60'):name='地上建筑设计总说明_人工改错后t3.dwg（case60）'
        source_rows.append(dict(name=name,sha256=source_sha,method_trials=len(ds),statuses=dict(Counter(d['run']['status'] for d in ds)),unique_regions=len({digest([d['case']['snapshot_id'],d['case']['layout_id'],d['case']['region_bbox']]) for d in ds})))
    counts=Counter(d['run']['status'] for _,d in rows);audit=json.loads((OUT/'source-binding-audit.json').read_text());negative=json.loads((OUT/'negative-candidate-checks.json').read_text())
    summary=dict(source_drawings=len(sources),unique_regions=sum(s['unique_regions'] for s in source_rows),case_labels=len(grouped),method_trials=len(rows),duplicate_scope_method_trials=1,
        statuses=dict(counts),structural_passes=counts['awaiting_review'],hidden_layer_passes=6,visible_structure_passes=counts['awaiting_review']-6,
        confirmed_incremental_recoveries_over_existing_components=0,visible_basic_area_entry_gap_cases=1,
        model_calls=0,new_private_data_transmission=False,public_registration=False,automatic_adoption=False,
        native_segments_verified=sum(r['segments_verified'] for r in audit),native_texts_verified=sum(r['texts_verified'] for r in audit),negative_candidates_rejected=len(negative),
        baseline_comparisons_unchanged=len(rows),sources=source_rows,cases=summary_rows,
        caveats=['Targeted closed-ring and real failure sampling, not whole-drawing recall or an unbiased accuracy estimate.',
        'Raw structural passes include six results on frozen BEAM geometry; these are excluded from visually useful candidates.',
        'Unknown drawing units remain unknown; no mm override used to inflate gains.',
        'One repeated text region is reported as a repeated trial, not an independent sample.',
        'One new visible area-interface success is already covered by existing enclosure discovery; not an overall component recovery.',
        'Two initial Docker-access refusals and an interrupted excessive pair baseline are preserved separately; not counted as algorithm outcomes.'])
    dump(OUT/'summary.json',summary)
    table=''.join(f'<tr><td>{html.escape(s["name"])}</td><td>{s["unique_regions"]}</td><td>{s["method_trials"]}</td><td>{s["statuses"].get("awaiting_review",0)}</td><td>{s["statuses"].get("refused",0)+s["statuses"].get("unresolved",0)}</td><td>0</td></tr>' for s in source_rows)
    nav=' '.join(f'<a href="#{k}">{k}</a>' for k in grouped)
    page='''<!doctype html><html lang="zh"><meta charset="utf-8"><title>真实图纸非表格动态方法验证</title><style>body{font:16px/1.7 system-ui;color:#20322c;background:#f3f6f3;max-width:1280px;margin:28px auto;padding:0 24px}section{background:white;border-radius:14px;padding:24px;margin:24px 0}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd6ce;padding:8px;vertical-align:top}a{color:#17613d}img{width:100%;background:white}figure{margin:12px 0}figcaption{font-size:14px;color:#53675c}.pair{display:flex;gap:12px}.pair figure{width:50%}code{overflow-wrap:anywhere}nav a{display:inline-block;margin:4px 10px 4px 0}.callout{background:#fff1ce;padding:16px;border-radius:10px}@media(max-width:700px){.pair{display:block}.pair figure{width:100%}}</style>
<h1>真实图纸验证：组件整体新增恢复 0 例</h1>
<p>4 张真实图纸、14 个独立局部、37 项方法试验（含 1 项同区域重复文本回归）。32 项结构通过、4 项输入拒绝、1 项复杂轮廓未解决。已有 GPT‑6.1 Sol 生成代码冻结重放，模型调用 0 次，无新增图纸外发。</p>
<div class="callout">结构通过不等于新增收益：其中 6 项来自两处冻结 BEAM 图层，已排除；剩余结果是现有能力兼容、未知单位的原始几何，或已有围合识别能覆盖的基础面积入口缺口。候选不自动采用，不注册公共方法。</div>
<section><h2>重点结论</h2><ol><li>原文、长度和距离未找到超出现有能力的真实恢复；按实体分页、聚合或两两调用即可取得相应结果。多段线内部各段的距离是更细的返回粒度，本轮不作为组件整体新增识别。</li><li>可见局部 <a href="#11816-visible-region-1">11816-visible-region-1</a>：基础面积接口拒绝四实体闭合环，动态返回约 0.285794 m²；已有围合识别得到对应轮廓，数值小差异来自毫米网格量化。属于接口覆盖差异，非整体新增。</li><li><a href="#11816-room-3552_01_1">孔浦公变房间</a>：原组件仍选中 40.8792 m² 局部面，上下沿跟随贯穿线，不能当作完整净边界；动态代码也未解决 584 段复杂线网。真正缺口是目标边界选择与贯穿线排除，不能仅靠简单闭环面积计算。</li><li>输入问题：冻结图层可被算法读取却在正常源图中不可见；地下室顶板存在无法定位的 ACAD_PROXY_ENTITY，拒绝不完整输入；两份图纸单位 unknown，原始图纸单位数值不证明物理测量恢复。</li></ol><p>下一步优先补齐宿主提供的可见性和单位证据，再围绕公变失败样本设计“有原生边证据的目标轮廓选择”适配及反例。此轮只验证并记录，未修改生产组件或放宽验收。</p></section>
<section><h2>逐图纸统计</h2><table><tr><th>图纸</th><th>独立局部</th><th>方法试验</th><th>结构通过</th><th>拒绝／未解决</th><th>整体新增恢复</th></tr>'''+table+'''</table><p>本轮为定向筛选，不代表整图召回率。首先筛选精确端点组成的非分支闭环、排除现有表格范围，再加入可见性补筛、单多段线对照、原文、输入阻断及历史房间失败样本。已有表格识别的包络排除不是语义真值，视觉复核仍只确认局部几何与文字。</p></section>
<section><h2>证据与复核口径</h2><p>全部 37 项前后原组件输出经规范化 JSON 哈希对比保持一致；独立重读源 DXF 核验 1741 条线段和 64 条原文，64 个错误／伪造候选被拒绝。输入阻断样本的已识别源片段可追溯，不表示代理图元已完整解析。</p><p>本次助手已检查周边原图、重点源图及全部 26 份可渲染候选叠加。高对比视图仅转换展示颜色，保留图层冻结状态、几何和文字；原色 SVG 同时保留。数值通过独立计算对照，不用视觉猜面积；没有将助手复核写成用户签认或工程验收。</p><p><a href="summary.json">汇总 JSON</a> · <a href="assistant-visual-review.json">助手视觉复核</a> · <a href="source-binding-audit.json">源文件追溯</a> · <a href="numeric-and-text-comparison.json">现有组件对照</a> · <a href="negative-candidate-checks.json">错误结果拒绝</a> · <a href="evidence-sha256.json">证据哈希</a></p><p><a href="overlay-sheet-1.png">叠加总览 1</a> · <a href="overlay-sheet-2.png">叠加总览 2</a> · <a href="overlay-sheet-3.png">叠加总览 3</a></p><nav>'''+nav+'</nav></section>'+''.join(sections)+'</html>'
    (OUT/'review.html').write_text(page)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('cases','caveats','sources')},ensure_ascii=False))
if __name__=='__main__':main()
