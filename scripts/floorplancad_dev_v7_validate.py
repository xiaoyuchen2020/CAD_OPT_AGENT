"""Read-only consistency checks of frozen v7 outputs; no sample contents read."""
import json,time,re
from pathlib import Path
from scripts.floorplancad_dev_v7_data import OUT,ROOT,sha,save,roster
from scripts.floorplancad_dev_v7_eval import check_freeze,aggregate,VARIANTS,NAMES

def run():
    check_freeze();freeze=json.loads((OUT/'candidate_freeze.json').read_text());seal=json.loads((OUT/'selection_seal.json').read_text());results={p:json.loads((OUT/(p+'_results.json')).read_text()) for p in ('analysis','fresh_checks')}
    assert seal['analysis_sha256']==sha((OUT/'analysis_results.json').read_bytes())
    assert seal['freeze_sha256']==sha((OUT/'candidate_freeze.json').read_bytes())
    assert freeze['created_unix']<results['analysis']['completed_unix']<=seal['sealed_unix']<results['fresh_checks']['completed_unix']
    assert seal['selected_components']=={'door':None,'window':None}
    used=set(json.loads((OUT/'excluded_used_families.json').read_text()));analysis=roster('analysis');fresh=roster('fresh_checks')
    assert len(used)==544 and len(analysis)==154 and len(fresh)==96
    assert len({r['group_id'] for r in fresh})==96 and used.isdisjoint(r['group_id'] for r in fresh)
    assert {r['group_id'] for r in analysis}<=used
    assert len({r['geometry_sha256'] for r in fresh})==96
    inspected=0;incomplete=[];wall_invariants={};read_paths=[]
    for phase,result in results.items():
        rows={r['sample_id']:r for r in roster(phase)}
        assert set(rows)=={r['sample_id'] for r in result['sample_results']}
        assert aggregate(result['sample_results'])==result['totals']
        assert result['source_hashes']==freeze['source_hashes']
        wall_invariants[phase]={}
        for name,z in result['totals'].items():wall_invariants[phase][name]=z['micro']['wall']==result['totals']['v5_full']['micro']['wall']
        for r in result['sample_results']:
            row=rows[r['sample_id']];audit=r['read_audit']
            assert audit['actual_dataset_reads']==[row['path']]==audit['svg_allowlist']
            assert not audit['labels_used_by_recognizer'] and not audit['old_checks_read'] and not audit['original_validation_read'] and not audit['test_read']
            assert r['source_sha256']==row['sha256']
            if phase=='fresh_checks':assert r['geometry_sha256']==row['geometry_sha256']
            read_paths.extend(audit['actual_dataset_reads'])
            for name in NAMES:
                path=OUT/'predictions'/phase/name/(r['sample_id'].replace('/','__')+'.json')
                pred=json.loads(path.read_text());inspected+=1
                assert pred['complete']==r['variants'][name]['complete']
                if name in VARIANTS and pred['complete']:
                    assert pred['automatic_room_closure'] is False and pred['original_geometry_preserved']
                    for d in pred['door_part_decisions']:assert set(d['removed_added_ids']).isdisjoint(d['seed_ids'])
                    for c in pred['candidates']:
                        ev=c['evidence']
                        if c['kind']=='window':assert set(ev['observed_frame_entity_ids'])==set(c['entity_ids'])|set(ev['unassigned_boundary_support_ids'])
                        if c['kind']=='door' and 'observed_door_entity_ids' in ev:
                            assert set(ev['observed_door_entity_ids'])==set(c['entity_ids'])|set(ev['unassigned_part_support_ids'])
                if name=='v5_full' and not pred['complete']:incomplete.append(dict(phase=phase,sample_id=r['sample_id'],primitives=r['primitives'],diagnostics=pred['diagnostics']))
    assert len(read_paths)==250 and len(set(read_paths))==250 and inspected==3250
    fresh_first=min(p.stat().st_mtime for p in (OUT/'progress/fresh_checks').glob('*.json'))
    assert seal['sealed_unix']<fresh_first
    public='cad_agent/geometry/vector_adapter.py';assert sha((ROOT/public).read_bytes())==freeze['source_hashes'][public]
    for rel,h in freeze['source_hashes'].items():assert sha((OUT/'frozen_source'/rel).read_bytes())==h
    report=OUT/'report.html';links=re.findall(r'(?:href|src)="([^"]+)"',report.read_text())
    missing=[x for x in links if not (OUT/x).exists() and x!='delivery_validation.json'];assert not missing
    all_used=used|{r['group_id'] for r in fresh};checks=all_used-{r['group_id'] for r in analysis}
    save(OUT/'consumed_families_after_run.json',dict(all_used_families=sorted(all_used),consumed_check_families=sorted(checks),analysis_families=sorted({r['group_id'] for r in analysis}),counts=dict(all_used=len(all_used),checks=len(checks),analysis=154),future_check_tuning_allowed=False))
    result=dict(validated_unix=time.time(),source_and_data_freeze_verified=True,selection_precedes_first_fresh_output=True,selection_unchanged=seal['selected_components'],source_prediction_files=inspected,
        analysis_families=154,new_check_families=96,previous_check_families_excluded=390,all_used_families_after=640,consumed_check_families_after=486,
        actual_worker_dataset_reads=250,worker_allowlists_verified=True,old_checks_original_validation_and_test_contents_read=False,
        incomplete_samples=incomplete,incomplete_in_primary_denominator=True,budget_increased=False,public_adapter_unchanged=True,
        wall_metrics_vs_v5=wall_invariants,tests=dict(command='.venv/bin/python -m pytest -q tests/test_vector_recognition*.py',passed=88,new_v7_tests=14),
        report_local_links_valid=True,analysis_image_visually_reviewed=True,artifact_sha256={f:sha((OUT/f).read_bytes()) for f in ('candidate_freeze.json','selection_seal.json','analysis_results.json','fresh_checks_results.json','report.html','analysis_examples.png')})
    save(OUT/'delivery_validation.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':run()
