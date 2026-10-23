"""Report source coverage, never equate citations with semantic correctness."""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text())


def audit(folder, gold, evidence):
    summary = load(folder/'summary.json')
    if not (folder/'result.json').exists():
        return dict(summary=summary, publication=False)
    result = load(folder/'result.json')
    citations = {e['evidence_id']:e for e in result['citations']}
    used = {eid for c in result['claims'] for eid in c['evidence_ids']}
    source_root = folder.parent/'data/drawings'/gold['snapshot_id']
    regions = {r['region_id']:r for r in load(source_root/'regions.json')}
    exact = all(e['snapshot_id']==gold['snapshot_id'] and e['region_id'] in regions
                and regions[e['region_id']]['text'][slice(*e['text_span'])]==e['quote']
                for e in citations.values())
    def covered(source, refs):
        spans = sorted(e['text_span'] for e in refs if e['snapshot_id']==source['snapshot_id']
                       and e['region_id']==source['region_id'])
        cursor, stop = source['text_span']
        for lo, hi in spans:
            if lo <= cursor <= hi:
                cursor = max(cursor, hi)
        return cursor >= stop
    rows = []
    for record in gold['records']:
        relevant = [c for c in result['claims'] if covered(evidence[record['title_id']],
                    [citations[eid] for eid in c['evidence_ids'] if eid in citations])]
        bound = [c for c in relevant if c['state'] in ('observed', 'derived')]
        refs = [citations[eid] for c in bound for eid in c['evidence_ids'] if eid in citations]
        supported = [eid for eid in record['source_ids'] if covered(evidence[eid], refs)]
        rows.append(dict(key=record['key'], title=record['title'], frame=record['frame_name'], expected=record['expected'],
                         full_citation_coverage=len(supported)==len(record['source_ids']),
                         supported_sources=len(supported), required_sources=len(record['source_ids']),
                         title_mentioned=bool(relevant), unknown_claim=any(c['state']=='unknown' for c in relevant),
                         claim_indexes=[result['claims'].index(c) for c in relevant]))
    return dict(summary=summary, publication=True, exact_original_spans=exact,
                claims_and_citations_match=used==set(citations),
                delivery_manifest_matches=used==set(result['delivery_manifest']['selected_evidence_ids']),
                source_bound_records_fully_cited=sum(r['expected']=='complete_source' and r['full_citation_coverage'] for r in rows),
                uncertain_records_labeled=sum(r['expected']=='uncertain_source' and r['unknown_claim'] for r in rows),
                records=rows, semantic_review='Citation coverage is not answer completeness; manually inspect layer content and scope.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path('artifacts/source-record-workflow'))
    parser.add_argument('--review',default='live-release-review')
    args=parser.parse_args()
    root=args.root
    gold=load(root/'exterior-gold-before-live.json')
    evidence=load(root/'prepare-2/1/evidence.json')
    arms={name:audit(root/folder/str(n),gold,evidence) for name,folder,n in [
        ('baseline','live-baseline',1),('final_review_1',args.review,1),('final_review_2',args.review,2),
        ('automatic_cold','prepare-2',1),('automatic_hot','prepare-2',2)]}
    for arm in arms.values():
        if not arm.get('publication'):continue
        summary=arm['summary'];summary.pop('source_records',None)
        summary['total_tokens']=summary['input_tokens']+summary['output_tokens']
    before=arms['baseline']['summary'];after=arms['final_review_1']['summary']
    changes={k:round((after[k]/before[k]-1)*100,2) for k in ('input_tokens','output_tokens','total_tokens','elapsed_seconds','rounds') if before[k]}
    report=dict(arms=arms,change_percent=changes,limitations=[
        'One baseline and two final subscription samples; not a statistical stability guarantee.',
        'Pre-indexed actual DXF snapshots; fresh import/conversion is excluded.',
        'Token reduction combines fixed workflow, source rendering and context encoding; not a pure formatting A/B.',
        'Model output is decisions; the program renders the final original-source answer.',
        'Cache tokens are included in input tokens. Subscription usage is not API billing.',
        'Citation span coverage is separate from semantic correctness and whole-drawing recall.'])
    (root/'comparison-final.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(dict(changes=changes,coverage={k:v.get('source_bound_records_fully_cited') for k,v in arms.items()},uncertain={k:v.get('uncertain_records_labeled') for k,v in arms.items()}),ensure_ascii=False))
if __name__=='__main__':main()
