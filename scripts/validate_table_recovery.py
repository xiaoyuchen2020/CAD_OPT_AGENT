"""Real DXF fixed-method replay. No model requests, uploads or production rules.

All candidates, including existing component successes, are mechanism controls;
they are never reported as new recovery wins. Failure injections happen only to
copies of sandbox outputs and are not stored as application results.
"""
import argparse
from collections import Counter
import copy
import hashlib
from html import escape
import json
import logging
from pathlib import Path
import shutil

from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.semantics.tables import grid
from cad_agent.extensions.table_recovery.bundle import digest, verify_bundle
from cad_agent.extensions.table_recovery.contracts import RunFixed
from cad_agent.extensions.table_recovery.service import RecoveryService
from cad_agent.extensions.table_recovery.validator import validate
from cad_agent.extensions.table_recovery.sandbox import execute, SandboxError, docker


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


def fingerprint(index,table):
    try:
        semantic={'value':grid(index,table)}
    except ValueError as exc:
        semantic={'error':str(exc)}
    return dict(raw=digest(index.read_table(table['table_id'],0,10000)),semantic=digest(semantic))


def attack_outputs(bundle,output):
    # Only attack a genuinely returned, valid grid. Otherwise no positive control.
    cases={}
    def case(name):
        value=copy.deepcopy(output);cases[name]=value;return value
    case('foreign_input')['input_id']='tri_foreign'
    case('empty_grid')['cells']=[]
    case('outside_scope')['xs'][0]-=1
    cell_number=next(i for i,c in enumerate(output['cells']) if c['text_entity_ids'])
    case('invented_entity')['cells'][cell_number]['text_entity_ids']=['invented']
    case('lost_text')['cells'][cell_number]['text_entity_ids']=[]
    case('duplicate_text')['cells'][cell_number]['text_entity_ids']*=2
    case('invented_edge')['cells'][0]['edge_segment_ids'][0]=['invented']
    case('missing_edge')['cells'][0]['edge_segment_ids'][0]=[]
    case('overlapping_cell')['cells'].append(copy.deepcopy(output['cells'][0]))
    case('forged_quote')['cells'][0]['text']='forged text'
    if len(output['cells'])>1:
        value=case('wrong_cell');other=(cell_number+1)%len(value['cells'])
        value['cells'][other]['text_entity_ids']+=value['cells'][cell_number]['text_entity_ids']
        value['cells'][cell_number]['text_entity_ids']=[]
        value=case('one_giant_cell');cell=value['cells'][0]
        cell.update(row_index=0,column_index=0,row_span=len(value['ys'])-1,column_span=len(value['xs'])-1,
                    text_entity_ids=[t['entity_id'] for t in bundle['texts']])
        value['cells']=[cell]
    reports={}
    for name,value in cases.items():
        r=validate(bundle,value)
        assert r['validation_status']=='rejected' and not r['cells'],name
        reports[name]=r['errors']
    return reports


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,default=Path('artifacts/semantic-repair-20261015/final/input-manifest.json'))
    parser.add_argument('--render',action='store_true')
    parser.add_argument('--probes',action='store_true')
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[1]
    files=sorted((root/'cad_agent').rglob('*.py'))+[Path(__file__).resolve()]
    hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    dump(out/'implementation-sha256.json',hashes)
    sources=json.loads(args.manifest.read_text());dump(out/'input-manifest.json',sources)
    (out/'inputs').mkdir(exist_ok=True);(out/'executions').mkdir(exist_ok=True)
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    w=Workspace(Settings(data_dir=out/'data',max_upload_bytes=256*1024*1024))
    summaries=[];cards=[];probe_results={};total_attacks=0
    try:
        if args.probes:
            probe_results['isolation']=execute(out/'data',{'operation':'probe'})
            checks=probe_results['isolation']['output']
            assert checks['uid']==65534 and all(checks[k] for k in ('root_write_denied','host_file_unavailable','network_denied','sensitive_env_absent'))
            for op,code in (('timeout_probe','SANDBOX_TIMEOUT'),('output_probe','SANDBOX_OUTPUT_LIMIT')):
                try:
                    execute(out/'data',{'operation':op},timeout=2 if op=='timeout_probe' else 30)
                except SandboxError as exc:
                    assert exc.code==code,(op,exc.code)
                    probe_results[op]={'error':exc.code,'expected_rejection':True}
                else:
                    raise AssertionError('Sandbox probe not rejected: '+op)
            assert not docker(['ps','-aq','--filter','name=cad-table-']).strip()
            probe_results['containers_cleaned_up']=True
            dump(out/'sandbox-probes.json',probe_results)
        for src in sources:
            raw=Path(src['path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==src['sha256']
            d=w.ingest(src['name']+'.dxf',raw);idx=DrawingIndex(w,d['id']);service=RecoveryService(w,d['id'],idx)
            reports=[];counts=Counter();review_candidates=[]
            for number,table in enumerate(idx.tables):
                before=fingerprint(idx,table)
                diag=service.diagnose(table['table_id'])
                if not diag['input_available']:
                    counts['tables']+=1;counts['input_refused']+=1
                    try:
                        grid(idx,table);counts['baseline_candidate']+=1
                    except ValueError:
                        counts['baseline_unresolved']+=1
                    assert fingerprint(idx,table)==before
                    reports.append(dict(table_index=number,table_id=table['table_id'],bbox=table['bbox'],
                                        diagnostic=diag,component_before_after_equal=True))
                    print(src['id'],number,diag['code'],flush=True)
                    continue
                bundle=service.record('table_recovery_input',diag['input_id']);verify_bundle(bundle)
                dump(out/'inputs'/(bundle['input_id']+'.json'),bundle)
                run=service.run(RunFixed(diagnostic_id=diag['diagnostic_id'],input_id=diag['input_id'],
                                        idempotency_key='replay-'+diag['input_id']))
                assert run['execution_status']=='completed',run
                result=service.record('table_recovery_result',run['result_id'])
                execution=service.record('table_recovery_execution',run['execution_id'])
                dump(out/'executions'/(run['run_id']+'.json'),execution)
                counts['tables']+=1;counts['baseline_'+bundle['baseline']['status']]+=1
                counts[result['validation_status']]+=1
                counts['candidate_grids']+=bool(result['cells'])
                counts['cells']+=len(result['cells'])
                span_count=0
                for cell in result['cells']:
                    for e in cell['evidence']:
                        assert e['snapshot_id']==d['id']
                        region=idx.by_id[e['region_id']];a,b=e['text_span']
                        assert region.text[a:b]==e['quote']
                        assert set(e['entity_ids'])<={t['entity_id'] for t in bundle['texts']}
                        span_count+=1
                counts['source_spans_checked']+=span_count
                assert fingerprint(idx,table)==before,'Existing component changed'
                entry=dict(table_index=number,table_id=table['table_id'],bbox=table['bbox'],
                           diagnostic=diag,run=run,result=result,component_before_after_equal=True)
                if result['cells'] and not any(r.get('failure_injections') for r in reports):
                    entry['failure_injections']=attack_outputs(bundle,execution['output'])
                    total_attacks+=len(entry['failure_injections'])
                reports.append(entry)
                if result['cells']:
                    score=sum(any(ch.isdigit() for ch in c['text']) for c in result['cells'])
                    review_candidates.append((score,number,result))
                elif bundle['baseline']['status']=='unresolved':
                    review_candidates.append((-1,number,result))
                print(src['id'],number,bundle['baseline']['status'],result['validation_status'],len(result['cells']),flush=True)
            if args.render:
                positives=sorted((v for v in review_candidates if v[0]>=0),reverse=True)[:1]
                negatives=[v for v in review_candidates if v[0]<0]
                for score,number,result in positives+negatives:
                    key=f"{src['id']}-table-{number}"
                    svg=service.overlay(result['result_id']);(out/(key+'.svg')).write_text(svg)
                    import resvg_py
                    (out/(key+'.png')).write_bytes(resvg_py.svg_to_bytes(svg_string=svg))
                    cards.append(f'<article><h2>{escape(src["name"])} · 表格候选 {number}</h2><p>{escape(result["validation_status"])} · 候选格数 {len(result["cells"])} · 人工复核待确认</p><img src="{key}.svg"><p><a href="{src["id"]}.json">完整输入身份、逐格来源和验证结果</a></p></article>')
            assert hashlib.sha256(Path(src['path']).read_bytes()).hexdigest()==src['sha256']
            dump(out/(src['id']+'.json'),dict(source=src,snapshot_id=d['id'],tables=reports))
            summaries.append(dict(id=src['id'],name=src['name'],source_sha256=src['sha256'],source_unchanged=True,**counts))
            dump(out/'summary.json',dict(samples=summaries,failure_injections_rejected=total_attacks,
                                        evidence_class='offline_real_dxf_fixed_method_docker_replay',
                                        semantic_accuracy_measured=False,new_recovery_benefit_measured=False))
        unchanged=all(hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p.relative_to(root))] for p in files)
        dump(out/'implementation-integrity.json',dict(unchanged_during_replay=unchanged))
        assert unchanged,'Implementation changed during replay'
        (out/'review.html').write_text('<!doctype html><meta charset="utf-8"><title>表格恢复固定方法验证</title><style>body{font:16px system-ui;margin:24px;background:#f4f6f8}article{background:white;padding:18px;margin:18px 0}img{max-width:100%;width:1000px}p{line-height:1.6}</style><h1>表格恢复固定方法 · 原图复核</h1><p>橙色为候选网格；空白文字不填充。自动验证不等于表格语义正确。现有成功表格用于机制对照，不计为新增恢复收益。没有橙色格线的项目保留原图供核查。</p><p><a href="summary.json">汇总</a> · <a href="sandbox-probes.json">隔离探针</a></p>'+''.join(cards))
    finally:
        w.close()


if __name__=='__main__':main()
