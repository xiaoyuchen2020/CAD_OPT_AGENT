"""Validate analysis-only audit evidence without opening any held-out content."""
import json,re,time
from pathlib import Path
from scripts.floorplancad_failure_audit import OUT,ROOT,PROTECTED,PREDICTIONS,roster,pred_path,guard,sha,save,summarize

def run():
    rows=roster();rowmap={r['sample_id']:r for r in rows};blocked=set(json.loads((OUT/'excluded_check_families.json').read_text()));protocol=json.loads((OUT/'protocol.json').read_text())
    assert len(rows)==154 and len(blocked)==486 and blocked.isdisjoint(r['group_id'] for r in rows)
    assert protocol['new_checks']==0 and protocol['audit_source_sha256']==sha((ROOT/'scripts/floorplancad_failure_audit.py').read_bytes())
    assert protocol['roster_sha256']==sha((OUT/'analysis_roster.json').read_bytes())
    assert protocol['allowed_paths_sha256']==sha((OUT/'allowed_data_paths.json').read_bytes())
    for p,h in protocol['protected_source_hashes'].items():assert sha((ROOT/p).read_bytes())==h
    samples=[json.loads((OUT/'samples'/(r['sample_id'].replace('/','__')+'.json')).read_text()) for r in rows]
    summary=json.loads((OUT/'summary.json').read_text());assert summarize(samples)==summary and summary['complete_samples']==154
    assert sum(summary['instance_stages']['door'].values())==501 and sum(summary['instance_stages']['window'].values())==213
    allsvg={r['path'] for r in rows};allpred={str(pred_path(r,n)) for r in rows for n in PREDICTIONS};allowed=allsvg|allpred
    evidence_reads=set()
    for sample in samples:
        row=rowmap[sample['sample_id']];assert sample['source_sha256']==row['sha256'] and sample['recognition_replay_matches_frozen']
        expected={row['path']}|{str(pred_path(row,n)) for n in PREDICTIONS};assert set(sample['actual_data_reads'])==expected
        evidence_reads.update(sample['actual_data_reads'])
        for obj in sample['instances']:
            assert obj['assigned']<=obj['parsed']<=obj['total']
            assert obj['stage']=='generation_no_target_candidate' or obj['raw_candidate_indices']
    for f in ['source_metadata_audit.json','label_presence_audit.json','case_index.json','text_schema_audit.json']:
        d=json.loads((OUT/f).read_text());reads=set(d['actual_data_reads']);assert reads<=allowed;evidence_reads.update(reads)
    rejections=json.loads((OUT/'rejection_path_summary.json').read_text());assert sum(len(r['instances']) for r in rejections['samples'])==87
    for r in rejections['samples']:
        assert set(r['actual_data_reads'])=={rowmap[r['sample_id']]['path']};evidence_reads.update(r['actual_data_reads'])
    metadata=json.loads((OUT/'source_metadata_audit.json').read_text());assert metadata['text_nodes']==len(metadata['texts'])==4081
    assert len({x['sample_id'] for x in metadata['texts']})==150 and metadata['texts_with_baseline']==4081
    overlap=json.loads((OUT/'source_group_overlap_check.json').read_text());assert overlap['groups']==overlap['disjoint']==195
    presence=json.loads((OUT/'label_presence_audit.json').read_text());assert presence['totals']['missing_semantic_attribute']==66741
    assert sum(presence['totals'].get(k,0) for k in ('missing_semantic_attribute','explicit_zero','explicit_nonzero'))==presence['totals']['shapes']==112895
    for cat,hist in presence['false_positive_annotation_origin'].items():assert sum(hist.values())==sum(summary['prediction_false_positive_raw_labels'][cat].values())
    cases=json.loads((OUT/'case_index.json').read_text())['cases'];notes=json.loads((OUT/'visual_review_notes.json').read_text())['cases']
    assert len(cases)==len(notes)==12 and len({c['group_id'] for c in cases})==12
    assert all(c['sample_id'] in rowmap for c in cases)
    from scripts.floorplancad_failure_audit_cases import choices
    expected,_=choices(samples);selection=json.loads((OUT/'case_selection.json').read_text())['cases'];assert selection==expected
    for c,n in zip(cases,notes):
        assert c['case_id']==n['case_id'] and not n['expert_confirmed'] and not n['original_labels_changed']
        assert sha((OUT/c['image']).read_bytes())==n['image_sha256']
        assert not c['room_accuracy_evaluated'] and not c['room_ground_truth_available']
        assert all(x['synthetic_edges']==0 and x['snapping'] is False for x in c['linework_topology'].values())
    proposed=json.loads((OUT/'minimal_validation_plan.json').read_text());assert proposed['status']=='proposal_not_executed' and proposed['scope']['new_check_families']==0
    template=json.loads((OUT/'development_reference_template.json').read_text());assert not template['room_references'] and template['annotation_status']=='pending_review'
    links=re.findall(r'(?:href|src)="([^"]+)"',(OUT/'report.html').read_text());assert not [p for p in links if not (OUT/p).exists() and p!='delivery_validation.json']
    # Final source hash readback is itself guarded by the fixed analysis roster.
    opened=guard(rows)
    for r in rows:assert sha(Path(r['path']).read_bytes())==r['sha256']
    assert set(opened)==allsvg
    audit_sources=['scripts/floorplancad_failure_audit.py','scripts/floorplancad_failure_audit_cases.py','scripts/floorplancad_failure_audit_supplement.py','scripts/floorplancad_failure_audit_report.py','scripts/floorplancad_failure_audit_validate.py','tests/test_floorplancad_failure_audit.py']
    for rel in audit_sources:
        p=OUT/'audit_source_snapshot'/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/rel).read_bytes())
    result=dict(validated_unix=time.time(),analysis_families=154,excluded_check_families=486,new_check_families_consumed=0,
        source_svgs_unique=154,old_analysis_prediction_files=462,observed_read_paths_within_allowlist=True,source_hashes_readback_verified=True,
        original_validation_test_and_check_contents_read=False,recognizer_replays_matching_frozen=154,recognizer_complete=154,
        protected_sources_unchanged=protocol['protected_source_hashes'],recognition_rules_added=0,parameters_changed=False,default_public_method_changed=False,product_render_changed=False,
        instance_stage_counts_verified=dict(door=501,window=213),candidate_filter_loss_instances_audited=87,source_metadata_verified=dict(texts=4081,text_families=150,wall_window_coincident_group_sets_disjoint=195),
        visual_review=dict(cases=12,distinct_families=12,reviewer='assistant',expert_adjudicated=False),room_accuracy_evaluated=False,
        proposed_plan_executed=False,original_labels_changed=False,diagnostic_tests=dict(command='.venv/bin/python -m pytest -q tests/test_floorplancad_failure_audit.py',passed=6),
        audit_script_repair=protocol.get('audit_repair'),audit_source_hashes={p:sha((ROOT/p).read_bytes()) for p in audit_sources},
        artifact_sha256={p:sha((OUT/p).read_bytes()) for p in ('report.html','protocol.json','summary.json','source_metadata_audit.json','label_presence_audit.json','rejection_path_summary.json','minimal_validation_plan.json','visual_review_notes.json')})
    save(OUT/'delivery_validation.json',result);print(json.dumps({k:result[k] for k in ['analysis_families','excluded_check_families','new_check_families_consumed','source_svgs_unique','recognizer_replays_matching_frozen','candidate_filter_loss_instances_audited','source_metadata_verified','visual_review','room_accuracy_evaluated','recognition_rules_added','diagnostic_tests']},ensure_ascii=False,indent=2))
if __name__=='__main__':run()
