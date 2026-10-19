"""Real model/source comparison in a separate workspace. No production data mutation."""
import argparse
import hashlib
import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from cad_agent.config import Settings, ROOT
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.semantics.tables import grid
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.contracts import Start
from cad_agent.extensions.table_recovery.bundle import prepare, digest
from cad_agent.extensions.table_recovery.worker import recover
from cad_agent.extensions.table_recovery.validator import validate

OUT = ROOT/'artifacts/dynamic-adaptation-20261016'
AUDIT = ROOT/'artifacts/table-recovery-gap-audit-20261016'


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='11816')
    parser.add_argument('--indices',default='27,28,36')
    parser.add_argument('--scan-only',action='store_true')
    parser.add_argument('--selected-only',action='store_true')
    parser.add_argument('--families',action='store_true')
    parser.add_argument('--method',choices=['text_spans','segment_measurements','closed_contour'])
    parser.add_argument('--mode',choices=['auto','fixed','dynamic','replay'],default='dynamic')
    parser.add_argument('--reuse-run')
    args=parser.parse_args();logging.getLogger('ezdxf').setLevel(logging.ERROR)
    OUT.mkdir(exist_ok=True);data=OUT/'data';data.mkdir(exist_ok=True)
    for name in ['adaptation-runtime.json','table-recovery-runtime.json']:
        shutil.copy2(ROOT/'.data'/name,data/name)
    source=next(s for s in json.loads((AUDIT/'search-manifest.json').read_text()) if s['id']==args.source)
    raw=Path(source['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==source['sha256']
    w=Workspace(replace(Settings.load(),data_dir=data,max_upload_bytes=256*1024*1024))
    try:
        print('IMPORT',args.source,source['name'],flush=True)
        drawing=w.ingest(source['name']+'.dxf',raw);idx=DrawingIndex(w,drawing['id'])
        selected={int(i) for i in args.indices.split(',')}
        report=dict(source=source,snapshot_id=drawing['id'],tables=[],baseline_scope='selected' if args.selected_only else 'all')
        cached_path=OUT/(args.source+'-scan.json')
        cached=json.loads(cached_path.read_text()) if cached_path.exists() else None
        for i,t in enumerate(idx.tables):
            item=dict(index=i,table_id=t['table_id'],bbox=t['bbox'])
            if i not in selected and (args.selected_only or cached):
                item.update(status='not_evaluated')
                if cached and i<len(cached['tables']):item=cached['tables'][i]
                report['tables'].append(item);continue
            try:
                value=grid(idx,t);item.update(status='candidate',rows=value[1],cols=value[2],sha256=digest(value))
            except ValueError as exc:item.update(status='unresolved',reason=str(exc))
            report['tables'].append(item)
        dump(OUT/(args.source+'-scan.json'),report)
        print('TABLES',len(idx.tables),'FAILED',sum(t['status']=='unresolved' for t in report['tables']),flush=True)
        if args.scan_only:return
        indices=[int(i) for i in args.indices.split(',')]
        for i in indices:
            table=idx.tables[i];service=AdaptationService(w,drawing['id'])
            tasks=[('table_grid','恢复该区域的真实表格网格；保留所有原生分隔线，不预设行列数。')]
            if args.families or args.method:
                tasks=[('text_spans','提取包含“临战”或“战前”的原文片段，完整保留所在源文本。'),
                       ('segment_measurements','计算区域内所有原生线段长度，以图纸单位输出。'),
                       ('closed_contour','找一个由原生线段完整组成的简单闭合多边形，输出面积；不裁切原线段、不认定房间。')]
                if args.method:tasks=[t for t in tasks if t[0]==args.method]
            for method,instruction in tasks:
                target=OUT/f'{args.source}-{i}-{method}-{args.mode}.json'
                if target.exists():
                    print('EXISTS',target.name,flush=True);continue
                request=Start(table_id=table['table_id'],method=method,instruction=instruction,mode=args.mode,
                              reuse_run_id=args.reuse_run,
                              idempotency_key=f'{args.source}-{i}-{method}-{args.mode}-v1')
                print('RUN',target.name,flush=True)
                run=service.start(request,background=False)
                result=dict(source=source,case_id=f'{args.source}-{i}',baseline=report['tables'][i],run=run)
                if run.get('input_artifact_id'):
                    bundle=w.store.get('adaptation_input',run['input_artifact_id'])
                    result['fixed_original']=validate(bundle,recover(bundle))
                if run.get('accepted_attempt'):
                    result['result']=service.result(run['run_id'])
                result['source_unchanged']=hashlib.sha256(Path(source['path']).read_bytes()).hexdigest()==source['sha256']
                dump(target,result)
                print('RESULT',run['status'],run.get('error'),run.get('usage'),flush=True)
    finally:w.close()


if __name__=='__main__':main()
