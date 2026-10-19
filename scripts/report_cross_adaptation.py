"""Review frozen-code regression evidence, keeping yield and compatibility distinct."""
import html
import json
import re
from collections import Counter
from dataclasses import replace
import resvg_py
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.review import source_svg
from scripts.regress_adaptation import OUT, PRIOR, CODE_SHA, dump


def raster(svg, target):
    view=list(map(float,re.search(r'viewBox="([^"]+)"',svg)[1].split()))
    ratio=max(view[2]/1100,view[3]/750)
    svg=re.sub(r'stroke-width="([0-9.]+)"',lambda m:f'stroke-width="{float(m[1])*ratio}"',svg).replace('vector-effect="non-scaling-stroke"','')
    target.write_bytes(resvg_py.svg_to_bytes(svg_string=svg,width=1100))


def main():
    w=Workspace(replace(Settings.load(),data_dir=OUT/'data'));rows=[];sections=[]
    visual_path=OUT/'assistant-visual-review.json'
    visual={r['case_id']:r for r in json.loads(visual_path.read_text())['records']} if visual_path.exists() else {}
    try:
        for path in sorted(OUT.glob('*-table_grid-replay.json')):
            d=json.loads(path.read_text());run=w.store.get('adaptation_run',d['run']['run_id'])
            s=AdaptationService.__new__(AdaptationService);s.w=w;s.sid=run['snapshot_id']
            v=s.result(run['run_id'])['validation'] if run.get('accepted_attempt') else {}
            count=len(v.get('cells',[]));fixed=d.get('fixed_comparison',{});fv=fixed.get('result',{}).get('validation',{})
            old=d['baseline'];changed=bool(count and (old['status']=='unresolved' or old.get('cell_count')!=count))
            row=dict(case_id=d['case_id'],source=d['source'],run_id=run['run_id'],
                status=run['status'],review_status=run['review_status'],baseline=old,
                assistant_visual_review=visual.get(d['case_id']),
                review_label='用户签认' if run['review_status']=='accepted' else ('助手视觉确认' if d['case_id'] in visual else '无通过候选'),
                cells=count,fixed_cells=len(fv.get('cells',[])),fixed_status=fixed.get('run',{}).get('status'),
                candidate_sha256=run.get('candidate_sha256'),
                baseline_changed=changed,
                new_resolution_of_unresolved=bool(count and old['status']=='unresolved'),
                dynamic_only_candidate=bool(count and not fv),
                existing_component_unchanged=d['existing_component_unchanged'],
                error=run.get('error'),attempts=run['attempts'],model_calls=run['model_calls'])
            rows.append(row)
            if count:
                src=OUT/(path.stem+'-source.svg');overlay=OUT/(path.stem+'-overlay.svg')
                if not src.exists():src.write_text(source_svg(s,run['run_id'],False))
                if not overlay.exists():
                    shapes=[]
                    for c in v['cells']:
                        x,y,z,t=c['bbox'];shapes.append(f'<rect x="{x}" y="{-t}" width="{z-x}" height="{t-y}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
                    overlay.write_text(src.read_text().replace('</svg>',''.join(shapes)+'</svg>'))
                for f in (src,overlay):
                    if not f.with_suffix('.png').exists():raster(f.read_text(),f.with_suffix('.png'))
                print('RENDERED',d['case_id'],flush=True)
            images=''
            if (OUT/(path.stem+'-source.png')).exists():
                images='<div class="pair">'+''.join(f'<a href="{path.stem}-{k}.svg"><img src="{path.stem}-{k}.png" alt="{k}"></a>' for k in ('source','overlay'))+'</div>'
            if not images:images='<p>未产生可验收候选；详细诊断见运行记录。</p>'
            label=d['source']['name']
            if d['source']['id']=='sample-60':label='地上建筑设计总说明_人工改错后t3.dwg（理工7，case60）'
            sections.append(f'<section id="{path.stem}"><h2>{d["case_id"]} · {html.escape(label)}</h2><p>状态 {run["status"]}，复核证据：{row["review_label"]}；原组件 {old.get("cell_count",old["status"])}，冻结代码 {count} 格，固定方法 {row["fixed_cells"] if fixed else "未执行"}。</p>{images}<details><summary>来源与运行证据</summary><pre>{html.escape(json.dumps(dict(summary=row,run=run),ensure_ascii=False,indent=2))}</pre></details></section>')
        summary=dict(cases=rows,case_count=len(rows),source_count=len({r['source']['sha256'] for r in rows}),
            statuses=dict(Counter(r['status'] for r in rows)),structural_passes=sum(bool(r['cells']) for r in rows),
            human_accepted_cases=sum(r['review_status']=='accepted' for r in rows),
            human_accepted_cells=sum(r['cells'] for r in rows if r['review_status']=='accepted'),
            assistant_visual_accepted_cases=sum(bool(r['assistant_visual_review']) for r in rows),
            assistant_visual_accepted_cells=sum(r['cells'] for r in rows if r['assistant_visual_review']),
            new_resolutions_of_previously_unresolved=sum(r['new_resolution_of_unresolved'] for r in rows),
            dynamic_only_candidates=sum(r['dynamic_only_candidate'] for r in rows),
            model_calls=sum(r['model_calls'] for r in rows),code_sha256=CODE_SHA,
            inherited_human_accepted=dict(case_id='11816-36',cells=26,review=str(PRIOR/'human-review/acceptance.json')),
            production_components_automatically_changed=False,method_registered=False)
        dump(OUT/'summary.json',summary)
        table=''.join(f'<tr><td><a href="#{r["case_id"]}-table_grid-replay">{r["case_id"]}</a></td><td>{r["baseline"].get("cell_count",r["baseline"]["status"])}</td><td>{r["cells"]}</td><td>{r["fixed_cells"] if r["fixed_status"] else "—"}</td><td>{r["status"]}</td><td>{r["review_label"]}</td></tr>' for r in rows)
        (OUT/'review.html').write_text(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>冻结动态方法跨图纸回归</title><style>body{{font:16px system-ui;margin:30px;color:#243247;background:#f6f8fc}}table{{border-collapse:collapse;width:100%}}td,th{{padding:8px;border:1px solid #ccd4de}}section{{background:white;padding:20px;margin:24px 0}}.pair{{display:flex;align-items:start}}.pair a{{width:50%}}img{{width:100%}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><h1>冻结代码跨图纸回归与人工复核</h1><p>本轮 {summary['source_count']} 张来源图纸、{len(rows)} 个局部，结构通过 {summary['structural_passes']} 个；新增人工确认 {summary['human_accepted_cases']} 处、{summary['human_accepted_cells']} 格；助手受托视觉确认 {summary['assistant_visual_accepted_cases']} 处、{summary['assistant_visual_accepted_cells']} 格。另保留孔浦 11816-36 已验收 26 格。模型调用 0 次，现有组件行为保留。</p><p>代码 SHA256：{CODE_SHA}。case60 是理工7人工改错版；未将 case62 当作独立于代码生成样本 case61 的图纸。自动通过不等于人工确认或新增收益。原组件有输出的样本属于兼容性或分格修正对照。</p><p><a href="assessment.html">公共方法评估</a> · <a href="human-review/acceptance.json">本轮人工签认</a> · <a href="source-binding-audit.json">独立原图追溯</a> · <a href="negative-candidate-checks.json">错误候选拒绝</a></p><table><thead><tr><th>区域</th><th>原组件格数/状态</th><th>重放格数</th><th>固定方法格数</th><th>结果</th><th>人工复核</th></tr></thead><tbody>{table}</tbody></table>{''.join(sections)}</html>''')
        print(json.dumps({k:v for k,v in summary.items() if k!='cases'},ensure_ascii=False),flush=True)
    finally:w.close()


if __name__=='__main__':main()
