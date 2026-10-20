"""Frozen paired offline replay of the remaining detail references.

Keep the previous v3.3 source snapshots, compare all 76 traces, and recompute all
semantic table pages. This is source/provenance regression, not accuracy scoring.
"""
import argparse
from collections import Counter
from hashlib import sha256
import json
import logging
from pathlib import Path
import shutil

from cad_agent.artifact_storage import copy_artifact_tree
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.semantics.service import SemanticService
from cad_agent.semantics.contracts import DetailTrace, SemanticTable
from cad_agent.geometry.source import native_source


def dump(p,value):
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2))


def target_key(t):
    return (t['snapshot_id'],t['entity_id'],t['detail_number'],t['state'],tuple(t['support_entity_ids']))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline',type=Path,default=Path('artifacts/semantic-repair-20261015/final'))
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();base=args.baseline.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'data').exists():raise ValueError('Use a fresh output directory; preserve earlier evidence.')
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    root=Path(__file__).resolve().parents[1]
    paths=sorted((root/'cad_agent').rglob('*.py'))+[Path(__file__).resolve()]
    hashes={str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest() for p in paths}
    dump(out/'implementation-sha256.json',hashes)
    inputs=json.loads((base/'input-manifest.json').read_text());dump(out/'input-manifest.json',inputs)
    copy_artifact_tree(base/'data',out/'data')
    w=Workspace(Settings(data_dir=out/'data',max_upload_bytes=256*1024*1024))
    summaries=[];triage=[];inventory=[]
    try:
        for src in inputs:
            assert sha256(Path(src['path']).read_bytes()).hexdigest()==src['sha256']
            old=json.loads((base/(src['id']+'.json')).read_text());sid=old['snapshot_id']
            assert sha256(w.store.artifact(sid,'source.dxf').read_bytes()).hexdigest()==src['sha256']
            s=SemanticService(w,sid);idx=s.index;native=native_source(w,sid)
            valid=set(native['entities'])|{t['entity_id'] for t in idx.scene['texts']};checks=Counter()
            def check(e,kind):
                assert e['snapshot_id']==sid
                a,b=e['text_span'];assert idx.by_id[e['region_id']].text[a:b]==e['quote']
                assert set(e['entity_ids'])<=valid
                checks[kind]+=1
            report=dict(source=src,snapshot_id=sid,baseline=str(base),traces=[],tables=[])
            for tr in old['traces']:
                before=tr['after'];ref=before['reference']
                after=s.trace(DetailTrace(layout_id=ref['layout_id'],reference_id=ref['object_id'],limit=12))
                assert not after['has_more'],'Unexpected targets; add full target pagination before evaluating.'
                assert after['reference']==ref
                # New diagnostics must not change previously supported matches.
                assert sorted(map(target_key,before['items']))==sorted(map(target_key,after['items']))
                for e in after['evidence']:check(e,'reference_spans')
                for e in after['reference_scope']['evidence']:check(e,'scope_note_spans')
                for t in after['items']+after['excluded_targets']:
                    assert t['source_sha256']==src['sha256'] and set(t['support_entity_ids'])<=valid
                    for e in t['source_records']:check(e,'target_and_excluded_spans')
                for sheet in after['sheet_inventory']:
                    for e in sheet['source_records']:check(e,'sheet_field_spans')
                pair=dict(reference_id=ref['object_id'],before=before,after=after);report['traces'].append(pair)
                if before['total']==0 or before['status']=='ambiguous':
                    triage.append(dict(drawing_id=src['id'],drawing=src['name'],label=ref['label'],reference_id=ref['object_id'],
                        bbox=ref['bbox'],before_total=before['total'],after_total=after['total'],
                        diagnosis=after['diagnosis'],scope=after['reference_scope'],
                        excluded_targets=after['excluded_targets'],targets=after['items']))
                if not inventory or src['id'] not in {x['drawing_id'] for x in inventory}:
                    inventory.extend(dict(drawing_id=src['id'],drawing=src['name'],**v) for v in after['sheet_inventory'])
            before_tables={v['table_id']:v for v in old['tables']}
            assert len(before_tables)==len(idx.tables)
            for table in idx.tables:
                before=before_tables[table['table_id']]
                try:
                    first=s.table(SemanticTable(table_id=table['table_id'],row_limit=5))
                    pages=[first]+[s.table(SemanticTable(table_id=table['table_id'],row_offset=i,row_limit=5)) for i in range(5,first['row_count'],5)]
                except ValueError as exc:
                    assert before['after']=='unavailable' and before['error']==str(exc)
                    report['tables'].append(dict(table_id=table['table_id'],state='unavailable',error=str(exc),unchanged=True));continue
                assert before['after']=='candidate_grid' and pages==before['pages']
                for page in pages:
                    for row in page['items']:
                        for c in row['cells']:
                            for e in c['evidence']+c['header_evidence']+c['unit_evidence']:check(e,'table_spans')
                report['tables'].append(dict(table_id=table['table_id'],state='candidate_grid',rows=first['row_count'],unchanged=True))
            assert sha256(Path(src['path']).read_bytes()).hexdigest()==src['sha256']
            summary=dict(id=src['id'],name=src['name'],source_sha256=src['sha256'],references=len(report['traces']),
                before_matched=sum(t['before']['total']>0 for t in report['traces']),after_matched=sum(t['after']['total']>0 for t in report['traces']),
                before_ambiguous=sum(t['before']['status']=='ambiguous' for t in report['traces']),after_ambiguous=sum(t['after']['status']=='ambiguous' for t in report['traces']),
                scope_notes=sum(bool(t['after']['reference_scope']['notes']) for t in report['traces']),
                diagnoses=dict(Counter(t['after']['diagnosis']['code'] for t in report['traces'])),tables=len(report['tables']),
                table_grids=sum(t['state']=='candidate_grid' for t in report['tables']),table_rows=sum(t.get('rows',0) for t in report['tables']),
                all_prior_targets_preserved=True,all_table_pages_unchanged=True,source_unchanged=True,checks=dict(checks))
            report['summary']=summary;dump(out/(src['id']+'.json'),report);summaries.append(summary);print(summary,flush=True)
        assert len(triage)==20
        dump(out/'remaining20.json',triage);dump(out/'batch-sheet-inventory.json',inventory)
        dump(out/'summary.json',dict(evidence_class='paired_offline_real_dxf_replay',semantic_accuracy_measured=False,
            samples=summaries,triage_diagnoses=dict(Counter(t['diagnosis']['code'] for t in triage)),
            baseline=str(base),checks='all_76_references_all_79_table_candidates_all_table_pages'))
        unchanged=all(sha256(p.read_bytes()).hexdigest()==hashes[str(p.relative_to(root))] for p in paths)
        dump(out/'implementation-integrity.json',dict(unchanged_during_replay=unchanged));assert unchanged
    finally:w.close()


if __name__=='__main__':main()
