"""Render and audit shadow candidates; never accept or register a method."""
import copy
import html
import json
import logging
from collections import Counter
from cad_agent.config import ROOT, Settings
from cad_agent.service import Workspace
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.review import source_svg
from cad_agent.extensions.adaptation.validation import validate
from cad_agent.extensions.table_recovery.bundle import digest
from scripts.validate_adaptation_shadow import OUT, CROSS, PRIOR
from scripts.regress_adaptation import dump, sha
from scripts.report_cross_adaptation import raster
from dataclasses import replace


def main():
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    w=Workspace(replace(Settings.load(),data_dir=OUT/'data'));rows=[];sections=[];checks=[]
    visual_file=OUT/'assistant-visual-review.json'
    visual={r['case_id']:r for r in json.loads(visual_file.read_text())['records']} if visual_file.exists() else {}
    try:
        files=sorted(OUT.glob('*-table_grid-replay.json'))
        seed=OUT/'sample-60-33-table_grid-dynamic.json'
        if not any(p.name=='sample-60-33-table_grid-replay.json' for p in files):files.append(seed)
        for path in files:
            d=json.loads(path.read_text());r=d['run'];sid=r['snapshot_id'];cid=d['case_id']
            # Read-only reporter must not initialize/interrupt another process's running jobs.
            s=AdaptationService.__new__(AdaptationService);s.w=w;s.sid=sid
            bundle=s.artifact('input',r['input_artifact_id']);s.source_check(bundle)
            entry=r['attempts'][r['accepted_attempt']-1] if r.get('accepted_attempt') else None
            result=s.result(r['run_id'])['validation'] if entry else {}
            if entry:
                output=s.artifact('execution',entry['execution_id'])['output']
                verify=validate('table_grid',bundle,output)
                assert verify==result
                checks.append(dict(case_id=cid,run_id=r['run_id'],validation_replay_equal=True))
            count=len(result.get('cells',[]));stem=cid
            # Prior source raster is reusable only when bound to the exact same immutable input.
            oldroot=CROSS if cid in {p.name.split('-table_grid')[0] for p in CROSS.glob('*-table_grid-replay.json')} else PRIOR
            oldpath=oldroot/(cid+'-table_grid-replay.json')
            old=json.loads(oldpath.read_text());assert old['run'].get('input_id')==r['input_id']
            src=OUT/(stem+'-source.svg');overlay=OUT/(stem+'-overlay.svg')
            if r.get('input_artifact_id'):
                if not src.exists():src.write_text(source_svg(s,r['run_id'],False))
                if count and not overlay.exists():
                    shapes=[]
                    for c in result['cells']:
                        x,y,z,t=c['bbox'];shapes.append(f'<rect x="{x}" y="{-t}" width="{z-x}" height="{t-y}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
                    overlay.write_text(src.read_text().replace('</svg>',''.join(shapes)+'</svg>'))
                for f in (src,overlay):
                    if f.exists() and not f.with_suffix('.png').exists():raster(f.read_text(),f.with_suffix('.png'))
            oldcount=len(old.get('result',{}).get('validation',{}).get('cells',[]))
            row=dict(case_id=cid,drawing=('地上建筑设计总说明_人工改错后t3.dwg' if d['source']['id']=='sample-60' else d['source']['name']),source_sha256=d['source']['sha256'],
                run_id=r['run_id'],input_id=r['input_id'],status=r['status'],error=r.get('error'),
                baseline=d['baseline'],old_dynamic_cells=oldcount,cells=count,
                newly_passes_old_dynamic=bool(count and not oldcount),
                new_resolution_of_original_unresolved=bool(count and d['baseline']['status']=='unresolved'),
                code_sha256=entry['code_sha256'] if entry else None,candidate_sha256=r.get('candidate_sha256'),
                model_calls=r['model_calls'],source_unchanged=d['source_unchanged'],
                existing_component_unchanged=d.get('existing_component_unchanged'),
                blocking_diagnostics=bundle['coverage']['blocking_diagnostics'],
                execution_reason=(s.artifact('execution',r['attempts'][-1]['execution_id'])['output'].get('reason') if r['attempts'] and r['attempts'][-1].get('execution_id') else None),
                classification='input_problem' if r.get('error')=='INPUT_INCOMPLETE_OR_UNSUPPORTED' else
                    ('non_table' if cid=='11821-19' else ('candidate_pending_visual' if count else 'method_gap')))
            if cid in visual:
                row.update(assistant_visual_review=visual[cid],classification=visual[cid]['classification'])
            rows.append(row)
            images=''.join(f'<a href="{stem}-{k}.svg"><img src="{stem}-{k}.png"></a>' for k in ('source','overlay') if (OUT/f'{stem}-{k}.png').exists())
            if not images:images='<p>输入诊断拒绝，未生成候选。历史原图身份记录保留，不能据此认定本次恢复成功。</p>'
            sections.append(f'<section id="{cid}"><h2>{cid} · {html.escape(row["drawing"])}</h2><p>原组件 {row["baseline"].get("cell_count",row["baseline"]["status"])}；旧动态 {oldcount}；新动态 {count}；状态 {r["status"]}。复核：{html.escape(visual.get(cid,{}).get('note','待复核'))}</p><div class="pair">{images}</div><details><summary>追溯记录</summary><pre>{html.escape(json.dumps(row,ensure_ascii=False,indent=2))}</pre></details></section>')
            print('REPORT',cid,count,flush=True)
        generated=json.loads(seed.read_text());r=generated['run'];a=r['attempts'][r['accepted_attempt']-1]
        s=AdaptationService.__new__(AdaptationService);s.w=w;s.sid=r['snapshot_id']
        b=s.artifact('input',r['input_artifact_id']);raw=s.artifact('execution',a['execution_id'])['output']
        negatives=[]
        for kind in ('foreign_input','fake_text','dropped_text','duplicate_text','fake_segment','missing_edge','overlap','empty','out_of_scope','forged_evidence'):
            bad=copy.deepcopy(raw);cells=bad['cells']
            if kind=='foreign_input':bad['input_id']='foreign'
            elif kind=='fake_text':cells[0]['text_entity_ids']=['foreign']
            elif kind=='dropped_text':
                next(c for c in cells if c['text_entity_ids'])['text_entity_ids']=[]
            elif kind=='duplicate_text':next(c for c in cells if c['text_entity_ids'])['text_entity_ids']*=2
            elif kind=='fake_segment':cells[0]['edge_segment_ids'][0]=['foreign']
            elif kind=='missing_edge':cells[0]['edge_segment_ids'][0]=[]
            elif kind=='overlap':cells.append(copy.deepcopy(cells[0]))
            elif kind=='empty':bad['cells']=[]
            elif kind=='out_of_scope':bad['xs'][0]-=100
            elif kind=='forged_evidence':cells[0]['evidence']=[{'quote':'fake'}]
            v=validate('table_grid',b,bad);assert v['validation_status']=='rejected' and not v.get('cells')
            negatives.append(dict(attack=kind,errors=v['errors'],rejected=True))
        # The original failed candidate must remain rejected by the unchanged validator.
        prior=json.loads((CROSS/'sample-60-33-table_grid-replay.json').read_text())['run'];e=prior['attempts'][-1]
        v=validate('table_grid',b,s.artifact('execution',e['execution_id'])['output'])
        assert 'CELL_ERASES_NATIVE_SEPARATOR' in v['errors']
        negatives.append(dict(attack='old_near_axis_candidate',errors=v['errors'],rejected=True))
        dump(OUT/'negative-candidate-checks.json',negatives);dump(OUT/'validation-replays.json',checks)
        summary=dict(cases=rows,case_count=len(rows),drawing_count=len({r['source_sha256'] for r in rows}),
            status_counts=dict(Counter(r['status'] for r in rows)),structural_passes=sum(bool(r['cells']) for r in rows),
            newly_passes_old_dynamic=sum(r['newly_passes_old_dynamic'] for r in rows),
            new_resolution_of_original_unresolved=sum(r['new_resolution_of_original_unresolved'] for r in rows),
            incremental_new_resolution_of_original_unresolved=sum(r['new_resolution_of_original_unresolved'] and r['newly_passes_old_dynamic'] for r in rows),
            generation_model_calls=generated['run']['model_calls'],generation_usage=generated['run']['usage'],
            replay_model_calls=sum(r['model_calls'] for r in rows if r['run_id']!=generated['run']['run_id']),
            code_sha256=a['code_sha256'],provider='responses_subscription',model='gpt-6.1-sol',
            automatic_adoption=False,method_registered=False,review_status='assistant_visual_complete' if len(visual)==len(rows)==19 else 'assistant_visual_pending',
            assistant_visual_cases=len(visual),human_confirmations_added=0,
            visual_classification_counts=dict(Counter(r['classification'] for r in rows)))
        dump(OUT/'summary.json',summary)
        import sqlite3
        c=sqlite3.connect(OUT/'data/index.sqlite3');folder=OUT/'audit-records';folder.mkdir(exist_ok=True)
        for kind,key,text in c.execute("select kind,id,data from records where kind like 'adaptation_%'"):
            if kind=='adaptation_run' and not json.loads(text)['request']['idempotency_key'].startswith('shadow'):continue
            dump(folder/(kind+'-'+key+'.json'),json.loads(text))
        c.close()
        table=''.join(f'<tr><td><a href="#{r["case_id"]}">{r["case_id"]}</a></td><td>{r["baseline"].get("cell_count",r["baseline"]["status"])}</td><td>{r["old_dynamic_cells"]}</td><td>{r["cells"]}</td><td>{r["classification"]}</td></tr>' for r in rows)
        (OUT/'review.html').write_text(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>近轴线动态方法旁路验证</title><style>body{{font:16px system-ui;margin:30px;color:#243247;background:#f6f8fc}}td,th{{padding:8px;border:1px solid #ccd4de}}table{{border-collapse:collapse}}section{{background:white;padding:20px;margin:24px 0}}.pair{{display:flex;align-items:start}}.pair a{{width:50%}}img{{width:100%}}pre{{white-space:pre-wrap}}</style><h1>近轴线动态方法：第一阶段与 Agent 旁路</h1><p>ChatGPT 直接订阅 GPT-6.1 Sol：生成 {summary['generation_model_calls']} 次，{summary['generation_usage']['total_tokens']} Token；冻结同代码本地重放。{len(rows)} 个局部，结构通过 {summary['structural_passes']} 个，比旧动态多 {summary['newly_passes_old_dynamic']} 个通过；相对原组件未解决而通过 {summary['new_resolution_of_original_unresolved']} 个（孔浦 11816-36，旧动态已实现）；本轮新增从原组件未解决变通过 {summary['incremental_new_resolution_of_original_unresolved']} 个。</p><p>本轮没有自动采用或注册公共方法。视觉确认是助手受托复核，不冒充用户签认，也不验收工程含义。</p><p><a href="assistant-visual-review.json">助手视觉复核</a> · <a href="source-binding-audit.json">独立原图追溯</a> · <a href="negative-candidate-checks.json">错误结果拒绝</a> · <a href="agent-shadow-check.json">Agent 旁路响应对照</a> · <a href="assessment.html">评估及实施说明</a></p><table><tr><th>局部</th><th>原组件</th><th>旧动态通过格数</th><th>新动态通过格数</th><th>分类</th></tr>{table}</table>{''.join(sections)}</html>''')
    finally:w.close()

if __name__=='__main__':main()
