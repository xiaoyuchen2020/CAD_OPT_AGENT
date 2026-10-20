"""Local cross-drawing regression of a frozen adapter; never calls a model."""
import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

from cad_agent.artifact_storage import copy_artifact, copy_artifact_tree
from cad_agent.config import ROOT, Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.semantics.tables import grid
from cad_agent.extensions.adaptation import service as adaptation
from cad_agent.extensions.adaptation.contracts import Start
from cad_agent.extensions.table_recovery.bundle import digest

OUT = ROOT/'artifacts/dynamic-cross-drawing-20261016'
PRIOR = ROOT/'artifacts/dynamic-adaptation-20261016'
AUDIT = ROOT/'artifacts/table-recovery-gap-audit-20261016'
ORIGIN = 'adrun_8420bf46e8e797b2bbc6e864'
CODE_SHA = 'bc04bf47a7cdc8b6ceec1d1ae6fd2e4df0ce56fd86c2d449a215f9d0da931dd4'


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def no_model(*args, **kwargs):
    raise AssertionError('MODEL_CALL_FORBIDDEN_IN_FROZEN_REGRESSION')


def seed(workspace, sources):
    # Copy only drawing snapshots and audit records; never open the old DB writable.
    for root in (PRIOR/'data', AUDIT/'search-data'):
        c = sqlite3.connect(f'file:{root / "index.sqlite3"}?mode=ro', uri=True)
        try:
            records = [(k, i, json.loads(d)) for k, i, d in c.execute('select kind,id,data from records')]
        finally:
            c.close()
        for kind, key, value in records:
            wanted = kind == 'drawing' and value.get('sha256') in sources
            if wanted:
                dest = workspace.settings.data_dir/'drawings'/key
                if not dest.exists():
                    copy_artifact_tree(root/'drawings'/key, dest)
            if wanted or (root == PRIOR/'data' and kind.startswith('adaptation_')):
                try:
                    workspace.store.get(kind, key)
                except KeyError:
                    workspace.store.put(kind, key, value)
    for name in ('adaptation-runtime.json', 'table-recovery-runtime.json'):
        copy_artifact(PRIOR/'data'/name, workspace.settings.data_dir/name)


def baseline(index, table):
    try:
        result = grid(index, table)
        return dict(status='candidate', rows=result[1], cols=result[2],
                    cell_count=len(result[0]), output_sha256=digest(result))
    except ValueError as exc:
        return dict(status='unresolved', reason=str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--indices', default='')
    parser.add_argument('--scan-only', action='store_true')
    args = parser.parse_args()
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    adaptation.propose = no_model
    manifest = json.loads((AUDIT/'search-manifest.json').read_text())
    source = next(s for s in manifest if s['id'] == args.source)
    assert sha(Path(source['path'])) == source['sha256']
    w = Workspace(replace(Settings.load(), data_dir=OUT/'data', max_upload_bytes=256*1024*1024))
    try:
        seed(w, {source['sha256'], *(s['sha256'] for s in manifest if s['id'] in ('11816', 'sample-61'))})
        print('IMPORT', args.source, flush=True)
        drawing = w.ingest(source['name']+'.dxf', Path(source['path']).read_bytes())
        idx = DrawingIndex(w, drawing['id'])
        scan = []
        for i, table in enumerate(idx.tables):
            scan.append(dict(index=i, table_id=table['table_id'], bbox=table['bbox'],
                             **baseline(idx, table)))
        dump(OUT/(args.source+'-scan.json'), dict(source=source, snapshot_id=drawing['id'], tables=scan))
        print('SCAN', args.source, len(scan), 'unresolved', sum(t['status']=='unresolved' for t in scan), flush=True)
        if args.scan_only:
            return
        for i in map(int, args.indices.split(',')):
            target = OUT/f'{args.source}-{i}-table_grid-replay.json'
            if target.exists():
                print('EXISTS', target.name, flush=True)
                continue
            table = idx.tables[i]
            service = adaptation.AdaptationService(w, drawing['id'])
            run = service.start(Start(table_id=table['table_id'], method='table_grid',
                instruction='冻结代码跨图纸回归，保留原生分隔线及原组件输出。', mode='replay',
                reuse_run_id=ORIGIN, idempotency_key=f'cross-{args.source}-{i}-v1'), background=False)
            assert run['model_calls'] == 0
            assert all(e['code_sha256'] == CODE_SHA for e in run['attempts'])
            after = baseline(idx, table)
            assert after == {k:v for k,v in scan[i].items() if k not in ('index','table_id','bbox')}
            result = dict(source=source, case_id=f'{args.source}-{i}', baseline=scan[i], run=run,
                          baseline_after=after, existing_component_unchanged=True,
                          source_unchanged=sha(Path(source['path']))==source['sha256'])
            if run.get('accepted_attempt'):
                result['result'] = service.result(run['run_id'])
                fixed = service.start(Start(table_id=table['table_id'], method='table_grid',
                    instruction='固定全宽行方法对照。', mode='fixed',
                    idempotency_key=f'cross-{args.source}-{i}-fixed-v1'), background=False)
                result['fixed_comparison'] = dict(run=fixed)
                if fixed.get('accepted_attempt'):
                    result['fixed_comparison']['result'] = service.result(fixed['run_id'])
            dump(target, result)
            print('RESULT', result['case_id'], run['status'], run.get('error'),
                  len(result.get('result',{}).get('validation',{}).get('cells',[])), flush=True)
    finally:
        w.close()


if __name__ == '__main__':
    main()
