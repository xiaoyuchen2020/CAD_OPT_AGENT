"""Paired replay against the delivered four-source semantic baseline.

Join by source SHA, layout name, source entity path / table envelope, never IDs
from a different parser version. Check every semantic table page and trace target.
"""
import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
import logging
from pathlib import Path
import time

from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.semantics.service import SemanticService
from cad_agent.semantics.contracts import SemanticTable, DetailTrace
from cad_agent.geometry.source import native_source
from cad_agent.cad.render import render_svg


def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline',type=Path,default=Path('artifacts/semantic-components-delivery-20261015'))
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--render',action='store_true')
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    baseline=args.baseline.resolve();inputs=json.loads((baseline/'input-manifest.json').read_text());dump(out/'input-manifest.json',inputs)
    root=Path(__file__).resolve().parents[1]
    files=sorted((root/'cad_agent').rglob('*.py'))+[Path(__file__).resolve()]
    hashes={str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest() for p in files};dump(out/'implementation-sha256.json',hashes)
    w=Workspace(Settings(data_dir=out/'data',max_upload_bytes=256*1024*1024))
    oldw=Workspace(Settings(data_dir=baseline/'data',max_upload_bytes=256*1024*1024))
    summaries=[];cards=[]
    try:
        for src in inputs:
            started=time.monotonic();raw=Path(src['path']).read_bytes();assert sha256(raw).hexdigest()==src['sha256']
            old=json.loads((baseline/(src['id']+'.json')).read_text());oldidx=DrawingIndex(oldw,old['snapshot_id'])
            d=w.ingest(src['name']+'.dxf',raw);del raw
            sid=d['id'];s=SemanticService(w,sid);index=s.index
            print('loaded',src['id'],flush=True)
            native=native_source(w,sid);valid_ids=set(native['entities'])|{t['entity_id'] for t in index.scene['texts']}
            old_names={v['layout_id']:v['name'] for v in oldidx.drawing['layouts']}
            names={v['layout_id']:v['name'] for v in d['layouts']}
            def tablekey(t,names):return (names[t['layout_id']],tuple(round(v,2) for v in t['bbox']))
            old_tables={tablekey(t,old_names):t for t in oldidx.tables}
            old_results={t['table_id']:t for t in old['tables']}
            report=dict(source=src,snapshot_id=sid,baseline_snapshot_id=old['snapshot_id'],parser_version=d['parser_version'],tables=[],traces=[],checks=Counter())
            def check(e):
                assert e['snapshot_id']==sid
                r=index.by_id[e['region_id']];a,b=e['text_span'];assert r.text[a:b]==e['quote']
                assert set(e['entity_ids'])<=valid_ids
            crops=[]
            for table in index.tables:
                key=tablekey(table,names);assert key in old_tables,'Unpaired table envelope'
                oldt=old_tables[key];before=old_results[oldt['table_id']]
                entry=dict(table_id=table['table_id'],baseline_table_id=oldt['table_id'],layout_id=table['layout_id'],bbox=table['bbox'],
                           before='unavailable' if before.get('status')=='unavailable' else 'candidate_grid',before_error=before.get('error'),
                           raw_rows=table['row_count'],raw_columns=table['column_count'])
                try:
                    page=s.table(SemanticTable(table_id=table['table_id'],row_limit=5));pages=[page]
                    for offset in range(5,page['row_count'],5):pages.append(s.table(SemanticTable(table_id=table['table_id'],row_offset=offset,row_limit=5)))
                    cells={c['cell_id']:c for p in pages for r in p['items'] for c in r['cells']}
                    actual=[eid for c in cells.values() for eid in c['entity_ids']]
                    a,b,c,d0=table['bbox'];expected={t['entity_id'] for t in index.scene['texts'] if t['layout_id']==table['layout_id'] and a<=t['origin'][0]<c and b<=t['origin'][1]<d0}
                    assert len(actual)==len(set(actual)) and set(actual)==expected,'Grid lost/duplicated source text'
                    for cell in cells.values():
                        for e in cell['evidence']+cell['header_evidence']+cell['unit_evidence']:check(e);report['checks']['table_spans']+=1
                    entry.update(after='candidate_grid',row_count=page['row_count'],column_count=page['column_count'],pages=pages,source_texts=len(expected),diagnostics=page['diagnostics'])
                    if entry['before']=='unavailable':crops.append((f"{src['id']}-recovered-table-{len(crops)}",'recovered_table',table))
                except ValueError as exc:
                    entry.update(after='unavailable',error=str(exc))
                    crops.append((f"{src['id']}-rejected-table-{len(crops)}",'rejected_table',table))
                report['tables'].append(entry)
            assert len(old_tables)==len(report['tables'])
            old_texts={t['entity_id']:t for t in oldidx.scene['texts']};new_texts={t['entity_id']:t for t in index.scene['texts']}
            def refkey(ref,texts,lidnames):
                paths=tuple(sorted(tuple(texts[e]['entity_path']) for e in ref['text_entity_ids']))
                return (lidnames[ref['layout_id']],ref['method'],paths)
            prior={refkey(ref,old_texts,old_names):ref for layout in old['layouts'] for ref in layout['items']['references']}
            trace_crops=set()
            for layout in d['layouts']:
                lid=layout['layout_id']
                for ref in s.annotations(lid)['references']:
                    prev=prior.pop(refkey(ref,new_texts,names))
                    result=s.trace(DetailTrace(layout_id=lid,reference_id=ref['object_id'],limit=12))
                    targets=list(result['items'])
                    for offset in range(12,result['total'],12):targets.extend(s.trace(DetailTrace(layout_id=lid,reference_id=ref['object_id'],offset=offset,limit=12))['items'])
                    result['items']=targets;result['has_more']=False
                    for t in targets:
                        assert t['source_sha256']==src['sha256'] and set(t['support_entity_ids'])<=valid_ids
                        for e in t['source_records']:check(e);report['checks']['trace_spans']+=1
                        if t['state']=='candidate_key_match' and len(trace_crops)<12 and t['target_id'] not in trace_crops:
                            trace_crops.add(t['target_id']);key=f"{src['id']}-trace-{len(trace_crops)}"
                            crops.extend([(key+'-reference','reference',ref),(key+'-target',t['method'],t)])
                    for e in result['evidence']:check(e);report['checks']['reference_spans']+=1
                    report['traces'].append(dict(baseline_reference_id=prev['object_id'],before_label=prev['label'],after_label=ref['label'],before_total=0,after=result))
            assert not prior,'Baseline references disappeared'
            assert sha256(Path(src['path']).read_bytes()).hexdigest()==src['sha256']
            report['source_unchanged']=True
            summary=dict(id=src['id'],name=src['name'],source_sha256=src['sha256'],snapshot_id=sid,baseline_snapshot_id=old['snapshot_id'],
                references=len(report['traces']),before_targets_found=0,
                after_targets_found=sum(t['after']['total']>0 for t in report['traces']),
                after_scoped_match=sum(any(v['state']=='candidate_key_match' for v in t['after']['items']) for t in report['traces']),
                after_ambiguous=sum(t['after']['status']=='ambiguous' for t in report['traces']),
                changed_reference_labels=sum(t['before_label']!=t['after_label'] for t in report['traces']),
                tables=len(report['tables']),before_unavailable=sum(t['before']=='unavailable' for t in report['tables']),
                recovered=sum(t['before']=='unavailable' and t['after']=='candidate_grid' for t in report['tables']),
                still_unavailable=sum(t['before']=='unavailable' and t['after']=='unavailable' for t in report['tables']),
                newly_rejected=sum(t['before']=='candidate_grid' and t['after']=='unavailable' for t in report['tables']),
                after_unavailable=sum(t['after']=='unavailable' for t in report['tables']),checks=dict(report['checks']),source_unchanged=True,
                elapsed_seconds=round(time.monotonic()-started,2))
            dump(out/(src['id']+'.json'),report);summaries.append(summary);dump(out/'summary.json',dict(samples=summaries,evidence_class='paired_offline_real_dxf_replay',semantic_accuracy_measured=False,table_checks='all_rows_all_pages'))
            print('done',summary,flush=True)
            if args.render:
                import resvg_py
                rendering=w.rendering(sid)
                # All recovered/rejected envelopes, plus bounded representative pairs.
                for key,kind,item in crops:
                    a,b,c,d0=item['bbox'];pad=max(c-a,d0-b)*.12;bounds=[a-pad,b-pad,c+pad,d0+pad]
                    if kind=='reference' and 'center' in item:
                        x,y=item['center'];r=item['radius']*3;bounds=[x-r,y-r,x+r,y+r]
                    svg=render_svg(rendering,item['layout_id'],bounds)
                    (out/(key+'.svg')).write_text(svg);(out/(key+'.png')).write_bytes(resvg_py.svg_to_bytes(svg_string=svg))
                    cards.append(f'<article><h3>{escape(key)}</h3><p>{escape(kind)} · {escape(item.get("title",item.get("label","")))}</p><img src="{escape(key)}.png"></article>')
        unchanged=all(sha256(p.read_bytes()).hexdigest()==hashes[str(p.relative_to(root))] for p in files)
        dump(out/'implementation-integrity.json',dict(unchanged_during_replay=unchanged));assert unchanged
        table=''.join(f"<tr><td>{escape(s['name'])}</td><td>0 → {s['after_targets_found']}</td><td>{s['after_scoped_match']}</td><td>{s['before_unavailable']} → 恢复 {s['recovered']}，保留 {s['still_unavailable']}</td><td>{s['newly_rejected']}</td></tr>" for s in summaries)
        (out/'review.html').write_text('<!doctype html><meta charset="utf-8"><title>语义组件修复前后核查</title><style>body{font:16px system-ui;margin:24px;background:#f3f5f7}article{background:white;padding:16px;margin:16px 0}img{max-width:100%;width:1000px}td,th{padding:10px;border:1px solid #bbb}table{border-collapse:collapse}</style><h1>同一批真实图纸：修复前后</h1><p>候选匹配不等于工程语义验收。图号未知、无标题标记和歧义分别保留；全部表格行的引用与原文核对，不测量整图识别准确率。</p><table><tr><th>图纸</th><th>找到目标的索引</th><th>含图号/同图框依据</th><th>原失败表格</th><th>旧可返回项新增拒绝</th></tr>'+table+'</table>'+''.join(cards))
    finally:w.close();oldw.close()


if __name__=='__main__':main()
