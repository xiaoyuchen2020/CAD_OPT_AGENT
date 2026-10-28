"""Read-only evaluation summary; does not infer semantic correctness from exit codes."""
from pathlib import Path
import json,sys,collections
root=Path(sys.argv[1])
events=[json.loads(s) for s in (root/'events.jsonl').read_text().splitlines()]
responses=[e for e in events if e['kind']=='model_response']
tools=[json.loads(p.read_text()) for p in sorted((root/'logs').glob('tool-*.json'))]
workset=json.loads((root/'geometry-workset.json').read_text()) if (root/'geometry-workset.json').exists() else {}
measurements=workset.get('measurements',{})
summary=dict(start=events[0]['time'],last=events[-1]['time'],finished=events[-1]['kind']=='model_finished',
    responses=len(responses),usage={k:sum((e.get('usage') or {}).get(k,0) for e in responses) for k in ['prompt_tokens','completion_tokens','total_tokens']},
    tools=dict(collections.Counter(t['name'] for t in tools)),
    errors=[dict(tool=t['name'],error=t['result'].get('error'),message=t['result'].get('message')) for t in tools if t['result'].get('error')],
    generated_code_calls=sum(t['name']=='execute_python' for t in tools),
    generated_code_failures=sum(t['name']=='execute_python' and t['result'].get('exit_code',0)!=0 for t in tools),
    local_measurements=[{k:r[k] for k in ['measurement_id','status','basis','measurements','source_entity_ids','diagnostics'] if k in r} for r in measurements.values()],
    guidance=[dict(file=e['file'],time=e['time'],category=e.get('category','unclassified_direct_assistance'),source=e.get('source','unclassified')) for e in events if e['kind']=='guidance'],
    interpretation='Numerical computation and autonomous semantic selection must be independently reviewed; counts are not acceptance.')
summary['usage']['cached_tokens']=sum((e.get('usage') or {}).get('prompt_tokens_details',{}).get('cached_tokens',0) for e in responses)
summary['usage']['cache_reporting_responses']=sum('cached_tokens' in (e.get('usage') or {}).get('prompt_tokens_details',{}) for e in responses)
summary['usage']['cache_note']='Reported cached_tokens are a subset of prompt_tokens. Missing cache details are not proof of zero cache.'
summary['guidance_counts']=dict(collections.Counter(e.get('category','unclassified_direct_assistance') for e in events if e['kind']=='guidance'))
failures=[dict(request=p.stem,status=json.loads(p.read_text()).get('status')) for p in sorted((root/'logs').glob('error-*.json'))]
summary['infrastructure_failures']=failures
first_failure=min((int(f['request'].split('-')[-1]) for f in failures),default=None)
summary['usage_populations']={name:dict(responses=len(group),prompt_tokens=sum((e.get('usage') or {}).get('prompt_tokens',0) for e in group),completion_tokens=sum((e.get('usage') or {}).get('completion_tokens',0) for e in group)) for name,group in [('before_infrastructure_failure',[e for e in responses if first_failure is None or e['round']<first_failure]),('after_infrastructure_resume',[e for e in responses if first_failure is not None and e['round']>first_failure])]}
(root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k!='local_measurements'},ensure_ascii=False,indent=2))
