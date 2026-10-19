"""Offline real-DXF replay: source integrity, candidate outputs and review crops.

Never runs production rules, uploads drawings or calls a language model.
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
from cad_agent.semantics.service import SemanticService
from cad_agent.semantics.contracts import AnnotationQuery, SemanticTable, DetailTrace
from cad_agent.geometry.source import native_source
from cad_agent.cad.render import render_svg


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,default=Path('artifacts/semantic-components-20261015'))
    ap.add_argument('--papers',type=int,nargs='+',default=[11804,11810,11798])
    ap.add_argument('--render',action='store_true')
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    root=Path('artifacts/ar030-real-room-validation-20261015')
    manifest=json.loads((root/'input_manifest.json').read_text())
    sources=[dict(id=str(m['paper_id']),name=m['source']['name'],path=str((root/'input/dxf'/f"{m['paper_id']}.dxf").resolve()),sha256=m['dxf_sha256']) for m in manifest if m['paper_id'] in args.papers]
    if set(args.papers)!={int(s['id']) for s in sources}:raise ValueError('Requested drawing absent from manifest')
    # An existing general-note drawing gives a contrasting table-rich sample.
    source=json.loads(Path('artifacts/real-validation-20261014/samples.json').read_text())[0]
    sources.append(dict(id='sample-59',name='真实设计说明样本 case59',path=source['source_path'],sha256=source['sha256']))
    dump(out/'input-manifest.json',sources)
    code_root=Path(__file__).resolve().parents[1]
    implementation_paths=sorted((code_root/'cad_agent').rglob('*.py'))+[Path(__file__).resolve()]
    implementation={str(p.relative_to(code_root)):sha256(p.read_bytes()).hexdigest() for p in implementation_paths}
    dump(out/'implementation-sha256.json',implementation)
    w=Workspace(Settings(data_dir=out/'data',max_upload_bytes=256*1024*1024))
    summaries=[];cards=[]
    try:
        for source in sources:
            started=time.monotonic();raw=Path(source['path']).read_bytes()
            assert sha256(raw).hexdigest()==source['sha256']
            d=w.ingest(source['name']+'.dxf',raw);del raw
            sid=d['id'];s=SemanticService(w,sid)
            print('loaded',source['id'],round(time.monotonic()-started,1),flush=True)
            report=dict(source=source,snapshot_id=sid,parser_version=d.get('parser_version'),layouts=[],tables=[],checks=0,table_checks=0)
            native=native_source(w,sid)
            valid_ids=set(native['entities'])|{t['entity_id'] for t in s.index.scene['texts']}
            crops=[]
            for layout in d['layouts']:
                lid=layout['layout_id'];data=s.annotations(lid)
                row=dict(layout_id=lid,name=layout['name'],counts={k:len(data[k]) for k in ('axes','references','leaders','elevations','storeys')},items=data)
                for kind in row['counts']:
                    for item in data[kind]:
                        assert set(item['entity_ids'])<=valid_ids
                        for e in s.evidence(item):
                            r=s.index.by_id[e['region_id']];a,b=e['text_span']
                            assert r.text[a:b]==e['quote'] and e['snapshot_id']==sid
                            report['checks']+=1
                    # At most two source crops per class, deterministic spatial order.
                    for j,item in enumerate(data[kind][:2]):crops.append((f"{source['id']}-{layout['name']}-{kind}-{j}",kind,item))
                row['traces']=[s.trace(DetailTrace(layout_id=lid,reference_id=r['object_id'])) for r in data['references']]
                report['layouts'].append(row)
                print(source['id'],layout['name'],row['counts'],flush=True)
            for table in s.index.tables:
                try:
                    page=s.table(SemanticTable(table_id=table['table_id'],row_limit=5))
                except ValueError as exc:
                    report['tables'].append(dict(table_id=table['table_id'],status='unavailable',error=str(exc)));continue
                report['tables'].append(page)
                for row in page['items']:
                    for cell in row['cells']:
                        for e in cell['evidence']+cell['header_evidence']+cell['unit_evidence']:
                            region=s.index.by_id[e['region_id']];a,b=e['text_span']
                            assert region.text[a:b]==e['quote'] and e['snapshot_id']==sid
                            report['table_checks']+=1
            good=[t for t in report['tables'] if t.get('header_rows')]
            good.sort(key=lambda t:-sum(c['numeric_value'] is not None and c['unit'] is not None for r in t['items'] for c in r['cells']))
            for j,t in enumerate(good[:2]):crops.append((f"{source['id']}-table-{j}",'table',t))
            if args.render:
                render=w.rendering(sid)
                for key,kind,item in crops:
                    a,b,c,d0=item['bbox'];pad=max(c-a,d0-b)*.15
                    bounds=[a-pad,b-pad,c+pad,d0+pad]
                    if kind in ('axes','references') and 'center' in item:
                        x,y=item['center'];r=item['radius']*3;bounds=[x-r,y-r,x+r,y+r]
                    svg=render_svg(render,item['layout_id'],bounds)
                    (out/(key+'.svg')).write_text(svg)
                    import resvg_py
                    (out/(key+'.png')).write_bytes(resvg_py.svg_to_bytes(svg_string=svg))
                    cards.append(f'<article><h3>{escape(key)}</h3><p>{escape(item.get("label",kind))}</p><img src="{escape(key)}.png"><pre>{escape(json.dumps({k:item.get(k) for k in ("object_id","state","method","unit_basis","header_rows")},ensure_ascii=False))}</pre></article>')
            report['source_unchanged']=sha256(Path(source['path']).read_bytes()).hexdigest()==source['sha256']
            assert report['source_unchanged']
            report['elapsed_seconds']=round(time.monotonic()-started,2)
            dump(out/(source['id']+'.json'),report)
            totals=Counter()
            for row in report['layouts']:totals.update(row['counts'])
            summary=dict(id=source['id'],name=source['name'],snapshot_id=sid,source_sha256=source['sha256'],
                counts=dict(totals),tables=len(report['tables']),tables_with_header_candidates=len(good),
                source_span_checks=report['checks'],source_unchanged=report['source_unchanged'],elapsed_seconds=report['elapsed_seconds'])
            summary.update(table_source_span_checks=report['table_checks'],tables_unavailable=sum(t.get('status')=='unavailable' for t in report['tables']),
                traced_references=sum(t['total']>0 for l in report['layouts'] for t in l['traces']),
                trace_queries=sum(len(l['traces']) for l in report['layouts']))
            summaries.append(summary);dump(out/'summary.json',dict(samples=summaries,evidence_class='offline_real_dxf_replay',semantic_accuracy_measured=False))
            print('done',summary,flush=True)
        unchanged=all(sha256(p.read_bytes()).hexdigest()==implementation[str(p.relative_to(code_root))] for p in implementation_paths)
        dump(out/'implementation-integrity.json',dict(unchanged_during_replay=unchanged))
        assert unchanged,'Implementation changed during replay; do not combine this run as a single version.'
        (out/'review.html').write_text('<!doctype html><meta charset="utf-8"><title>公共语义组件真实图纸核查</title><style>body{font:16px system-ui;margin:24px;background:#f4f6f8}article{background:white;padding:16px;margin:16px 0}img{max-width:100%;width:1000px;background:white}pre{white-space:pre-wrap}</style><h1>真实图纸候选与原图核查</h1><p>候选统计不代表识别准确率；来源绑定检查不代替语义复核。详见各图纸 JSON 与 visual-review.json。</p>'+''.join(cards))
    finally:w.close()


if __name__=='__main__':main()
