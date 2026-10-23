"""Compare frozen subscription repetitions without inventing semantic scores."""
import argparse
import json
from pathlib import Path
from statistics import median


def read(path):
    return json.loads(path.read_text())


def audit(folder):
    summaries = read(folder / 'results.json')
    rows = []
    for summary in summaries:
        run = read(folder / f"{summary['attempt']}-run.json")
        result = run.get('result') or {}
        sources = result.get('citations', [])
        used = {eid for c in result.get('claims', []) for eid in c.get('evidence_ids', [])}
        published = {e['evidence_id'] for e in sources}
        manifest = result.get('delivery_manifest')
        regions = {}
        exact, invalid = 0, []
        for evidence in sources:
            sid = evidence['snapshot_id']
            if sid not in regions:
                regions[sid] = {r['region_id']: r for r in read(folder / 'data' / 'drawings' / sid / 'regions.json')}
            region = regions[sid].get(evidence['region_id'])
            span = evidence.get('text_span')
            if region and span and region['text'][slice(*span)] == evidence['quote']:
                exact += 1
            else:
                invalid.append(evidence['evidence_id'])
        usage = run.get('usage', [])
        cached_known = all(type(u.get('prompt_cache_hit_tokens')) is int for u in usage)
        requests = result.get('model_requests', [])
        row = {k: v for k, v in summary.items() if k not in ('answer', 'trace')}
        row.update(total_tokens=summary['input_tokens'] + summary['output_tokens'],
            initial_tool_count=requests[0].get('tool_count') if requests else None,
            initial_schema_chars=requests[0].get('tool_schema_chars') if requests else None,
            initial_prompt_tokens=usage[0].get('prompt_tokens') if usage else None,
            uncached_input_tokens=summary['input_tokens'] - summary['cached_tokens'] if cached_known else None,
            cache_usage_known=cached_known, tool_calls=len(run.get('trace', [])),
            failed_tool_calls=sum(t['status'] == 'failed' for t in run.get('trace', [])),
            citation_count=len(sources), exact_source_spans=exact, invalid_spans=invalid,
            citations_not_used_by_claims=sorted(published - used),
            manifest_matches_claims=(set(manifest['selected_evidence_ids']) == used == published) if manifest else None,
            task_manifest_consistent=(manifest.get('completion_scope') == result.get('task', {}).get('status')) if manifest else None,
            published_frames=sorted({e.get('source_context', {}).get('frame_name') for e in sources if e.get('source_context', {}).get('frame_name')}),
            semantic_review='requires_human_review_of_answer_and_source_records')
        rows.append(row)
        (folder / f"{summary['attempt']}-answer.md").write_text(result.get('answer') or ('运行失败：' + str(run.get('error'))))
    numeric = ('input_tokens', 'output_tokens', 'total_tokens', 'model_requests', 'seconds', 'tool_calls')
    return dict(attempts=rows, median={k: median(r[k] for r in rows) for k in numeric},
                successful_runs=sum(r['status'] == 'completed' for r in rows),
                sample_size=len(rows), includes_failed_attempts=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    old_manifest, new_manifest = read(args.before / 'manifest.json'), read(args.after / 'manifest.json')
    keys = ('source_sha256', 'question', 'model', 'effort', 'runtime', 'allow_fallback', 'source_read_batching')
    assert all(old_manifest[k] == new_manifest[k] for k in keys), 'Non-comparable experiment inputs'
    before, after = audit(args.before), audit(args.after)
    changes = {k: (after['median'][k] / before['median'][k] - 1) * 100
               for k in before['median'] if before['median'][k]}
    output = dict(inputs={k: old_manifest[k] for k in keys}, before=before, after=after,
        median_change_percent=changes,
        limitations=['Three repetitions are not a population estimate.',
                    'Exact source spans and selected IDs do not prove semantic support.',
                    'Cached input remains part of total input; no currency or subscription quota estimate.',
                    'Historical 20-question scores are not current-version results.'])
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(dict(before=before['median'], after=after['median'], changes=changes), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
