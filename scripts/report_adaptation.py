"""Build a local evidence report; never modifies prior audit or approval files."""
import hashlib
import html
import json
import re
from dataclasses import replace
from pathlib import Path
import resvg_py
from cad_agent.config import ROOT, Settings
from cad_agent.service import Workspace
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.review import source_svg

OUT=ROOT/'artifacts/dynamic-adaptation-20261016'


def main():
    # Aggregate replay audits also end in -replay.json; only load case exports.
    files=sorted(p for p in OUT.glob('*.json') if any(
        p.name.endswith(f'-{method}-{mode}.json')
        for method in ('table_grid', 'text_spans', 'segment_measurements', 'closed_contour')
        for mode in ('dynamic', 'fixed', 'replay')))
    rows=[];sections=[];w=Workspace(replace(Settings.load(),data_dir=OUT/'data'))
    try:
        for path in files:
            data=json.loads(path.read_text());run=w.store.get('adaptation_run',data['run']['run_id']);method=run['request']['method']
            result=data.get('result',{}).get('validation',{})
            item=dict(case_id=data['case_id'],source=data['source'],mode=run['request']['mode'],method=method,
                run_id=run['run_id'],status=run['status'],attempts=len(run['attempts']),model_calls=run['model_calls'],
                tokens=run['usage'].get('total_tokens',0),elapsed_seconds=run['elapsed_seconds'],
                result_count=len(result.get('cells',[]))+len(result.get('items',[])),
                candidate_sha256=run.get('candidate_sha256'),review_status=run['review_status'],
                baseline=data['baseline'],source_unchanged=data['source_unchanged'],
                errors=[e.get('errors',[]) or [e.get('error')] for e in run['attempts']])
            rows.append(item)
            if run.get('input_artifact_id'):
                s=AdaptationService.__new__(AdaptationService);s.w=w;s.sid=run['snapshot_id']
                s.source_check(s.artifact('input',run['input_artifact_id']))
                for overlay in (False,True):
                    suffix='overlay' if overlay else 'source'
                    image=OUT/(path.stem+'-'+suffix+'.svg')
                    if not image.exists():image.write_text(source_svg(s,run['run_id'],overlay))
                    png=image.with_suffix('.png')
                    if not png.exists():
                        svg=image.read_text();m=re.search(r'viewBox="([^"]+)"',svg);view=list(map(float,m[1].split()))
                        ratio=max(view[2]/1100,view[3]/750)
                        raster=re.sub(r'stroke-width="([0-9.]+)"',lambda m:f'stroke-width="{float(m[1])*ratio}"',svg).replace('vector-effect="non-scaling-stroke"','')
                        png.write_bytes(resvg_py.svg_to_bytes(svg_string=raster,width=1100))
            images=''.join(f'<a href="{path.stem}-{kind}.svg"><img src="{path.stem}-{kind}.png" alt="{kind}"></a>' for kind in ('source','overlay')) if run.get('input_artifact_id') else ''
            detail=html.escape(json.dumps(dict(run=run,validation={k:v for k,v in result.items() if k!='derived_input'}),ensure_ascii=False,indent=2))
            sections.append(f'<section id="{path.stem}"><h2>{html.escape(data["case_id"])} · {method} · {run["request"]["mode"]}</h2><p>{html.escape(data["source"]["name"])}</p><p>{run["status"]} · 候选数量 {item["result_count"]} · 人工复核 {run["review_status"]}</p><div class="pair">{images}</div><details><summary>运行、来源与验证明细</summary><pre>{detail}</pre></details></section>')
        summary=dict(cases=rows,model_calls=sum(r['model_calls'] for r in rows),
            total_tokens=sum(r['tokens'] for r in rows),monetary_cost=None,
            monetary_cost_note='Provider price not configured; tokens and elapsed time are actual measured values.',
            human_confirmed_new_dynamic_recoveries=sum(r['review_status']=='accepted' and r['method']=='table_grid' and r['mode'] in ('dynamic','replay') for r in rows),
            production_components_automatically_changed=False)
        (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        table=''.join(f'<tr><td><a href="#{Path(f).stem}">{r["case_id"]}</a></td><td>{r["method"]}</td><td>{r["mode"]}</td><td>{r["status"]}</td><td>{r["result_count"]}</td><td>{r["model_calls"]}</td><td>{r["tokens"]}</td></tr>' for f,r in zip(files,rows))
        (OUT/'review.html').write_text(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>受控动态方法验证</title><style>body{{font:16px system-ui;margin:30px;color:#243247;background:#f6f8fc}}table{{border-collapse:collapse;width:100%}}td,th{{padding:8px;border:1px solid #ccd4de}}section{{background:white;padding:20px;margin:24px 0}}.pair{{display:flex;gap:1%;align-items:start}}.pair a{{width:49%}}img{{width:100%}}pre{{white-space:pre-wrap;word-break:break-all}}h1{{color:#153c68}}</style><h1>受控动态方法：真实运行与原图复核</h1><p>所有结果来自已记录的真实样本。自动通过仅代表相应结构和来源约束；尚未签认的新候选保持 pending。原组件结果、固定组合与 LLM 动态方法分别记录。</p><p>实际模型调用 {summary["model_calls"]} 次，{summary["total_tokens"]} tokens。未配置价格，不估算金额。</p><table><thead><tr><th>区域</th><th>方法</th><th>模式</th><th>结果</th><th>候选数</th><th>模型调用</th><th>tokens</th></tr></thead><tbody>{table}</tbody></table>{''.join(sections)}</html>''')
        print(json.dumps({k:v for k,v in summary.items() if k!='cases'},ensure_ascii=False),flush=True)
    finally:w.close()


if __name__=='__main__':main()
