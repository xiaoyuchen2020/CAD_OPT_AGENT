"""Audit saved real-model runs against frozen original text; performs no inference."""
import argparse
import json
from pathlib import Path


def audit(root, case='exact', case_name=''):
    saved=root/case_name if case_name else root
    run=json.loads((saved/'run.json').read_text())
    task=json.loads((saved/'task.json').read_text())
    request=json.loads((saved/'request.json').read_text())
    regions=json.loads((root/'data'/'drawings'/request['snapshot_id']/'regions.json').read_text())
    sources={r['region_id']:r for r in regions}
    result=run.get('result') or {}
    events=run.get('run_events', [])
    models=[e for e in events if e['kind']=='model']
    tools=[e for e in events if e['kind']=='tool']
    contract=task.get('query_intent', {}).get('intent', {})
    targets=contract.get('targets', [])
    evidence=task.get('evidence', {})
    cited={eid for claim in result.get('claims', []) for eid in claim.get('evidence_ids', [])}
    invalid=[]
    for eid in cited:
        item=evidence.get(eid, {})
        source=sources.get(item.get('region_id'), {})
        span=item.get('text_span', [])
        if (item.get('snapshot_id')!=request['snapshot_id'] or len(span)!=2 or
                source.get('text', '')[span[0]:span[1]]!=item.get('quote')):
            invalid.append(eid)
    checks=dict(completed=run['status']=='completed', actual_model=bool(models),
        model_before_tools=bool(models and tools and models[0]['sequence']<tools[0]['sequence']),
        subscription_only=request['runtime']=='responses_subscription' and request['allow_fallback'] is False,
        intent_registered=bool(targets), source_citations_valid=bool(cited) and not invalid,
        no_implicit_expansion=all(not s.get('alternatives') for s in task.get('query_searches', [])))
    text=''.join(c.get('text', '') for c in result.get('claims', []))
    normalized=''.join(text.split()).casefold()
    if case=='exact':
        checks['single_wall4_candidate']=sum(s.get('total', 0) for s in task.get('query_searches', []))==1
        checks['body_and_notes_retained']=all(s.casefold() in normalized for s in
            ('1.5','防水涂料','160g','25mm','DPM15','300','0.6','装修设计'))
    elif case=='multiple':
        checks['distinct_targets']=len(targets)>=2
        checks['each_target_addressed']={t['target_id'] for t in targets}<={
            tid for c in result.get('claims', []) for tid in c.get('query_target_ids', [])}
    elif case=='exclude':
        checks['exclusion_preserved']=any(c['operator']=='exclude' and c['value']=='地下室' for t in targets for c in t['conditions'])
        # Raw rejected candidates may be quoted with conflict/unknown; they must
        # not be presented as an accepted result of the excluded scope.
        checks['excluded_scope_not_accepted']=all(c['state'] in ('unknown','conflict') for c in result.get('claims', []))
    report=dict(case=case, run_id=run['id'], run_status=run['status'], task_status=task.get('status'),
        checks=checks, passed=all(checks.values()), invalid_citations=invalid,
        model_requests=len(models), tool_calls=len(tools), citations=len(cited),
        source_candidates=len(task.get('source_candidates', {})),
        requirement_outcomes=result.get('requirement_outcomes', []),
        limitation='原文交付与引用验证；候选归属、工程适用性和全图穷尽不因此获得确认。')
    (saved/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--case',choices=['exact','multiple','exclude','followup'],default='exact')
    parser.add_argument('--case-name',default='')
    args=parser.parse_args()
    report=audit(args.directory,args.case,args.case_name)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['passed'] else 1)
