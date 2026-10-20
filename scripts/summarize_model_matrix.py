"""Summarize explicit six-dimension reviews; never infer scores from tool counts."""
from pathlib import Path
import argparse
import json
import statistics
from itertools import combinations

DIMENSIONS = ['取证', '归属', '范围', '测量', '计算', '交付']


def summarize(root):
    load = lambda name: json.loads((root / name).read_text())
    plan = load('plan.json')
    results = load('evaluated-results.json') if (root / 'evaluated-results.json').exists() else []
    reviews = load('review.json') if (root / 'review.json').exists() else []
    rubric = {r['id']: r for r in load('rubric.json')['cases']}
    scored = {r['id']: r for r in reviews}
    if len(scored) != len(reviews):
        raise ValueError('Duplicate review')
    ids = {r['id'] for r in results}
    if len(ids) != len(results):
        raise ValueError('More than one attempt: choose a disclosed comparison policy first')
    rows = []
    for r in results:
        case = r['case']; review = scored.get(r['id']); applicable = rubric[case['pair_id']]['applicable']
        if review:
            if review['run_id'] != r['run_id']:
                raise ValueError('Review belongs to another run')
            for dim in DIMENSIONS:
                value = review['scores'][dim]
                if r['status'] != 'completed' or not applicable[dim]:
                    if value is not None: raise ValueError('Failure/N-A must retain null content score')
                elif type(value) is not int or value not in (0, 1, 2):
                    raise ValueError('Missing/invalid applicable content score')
                if not review['reasons'].get(dim): raise ValueError('Missing score reason')
        scores = review['scores'] if review else {d: None for d in DIMENSIONS}
        published = r['status'] == 'completed'
        rows.append(dict(id=r['id'], pair_id=case['pair_id'], variant=case['variant'],
            title=case['title'], question=case['question'], status=r['status'], error=r.get('error'),
            reviewed=bool(review), published=published,
            full_completion=bool(review and published and all(scores[d] == 2 for d in DIMENSIONS if applicable[d])),
            task_completed=r.get('task_status')=='completed', scores=scores,
            utility_points=(sum(v or 0 for v in scores.values()) if review else None),
            utility_max=2*sum(applicable.values()), applicable=applicable,
            seconds=r['elapsed_seconds'], tokens=r['tokens'], model_requests=r['model_requests'],
            answer_repair_requests=r['answer_repair_requests'], model_wait_seconds=r['model_wait_seconds'],
            batch_count=r['batch_count'],
            unknown_usage=bool((r.get('result') or {}).get('subscription_budget',{}).get('unknown_usage') or
                any(t.get('unknown_usage') for req in (json.loads((Path(r['evidence_path'])/'run.json').read_text()).get('runtime_diagnostics') or {}).get('model_requests',[]) for t in req.get('transport_attempts',[]))),
            answer=r.get('answer'), reasons=review['reasons'] if review else {},
            blockers=review.get('blockers',[]) if review else [], evidence_path=r['evidence_path']))
    variants = plan['variants']
    by_variant = {v['id']: [r for r in rows if r['variant']==v['id']] for v in variants}
    common = set.intersection(*[{r['pair_id'] for r in rs if r['published'] and r['reviewed']} for rs in by_variant.values()])
    def mean(values): return statistics.mean(values) if values else None
    summary = []
    for v in variants:
        rs = by_variant[v['id']]; reviewed = [r for r in rs if r['reviewed']]
        durations = [r['seconds'] for r in rs if r['seconds'] is not None]
        summary.append(dict(**v, expected=sum(c['variant']==v['id'] for c in plan['cases']),
            finished=len(rs), reviewed=len(reviewed), published=sum(r['published'] for r in rs),
            full_completion=sum(r['full_completion'] for r in reviewed), task_completed=sum(r['task_completed'] for r in rs),
            substantive_delivery=sum(r['published'] and (r['scores'].get('交付') or 0) >= 1 for r in reviewed),
            utility_points=sum(r['utility_points'] for r in reviewed), utility_max=sum(r['utility_max'] for r in reviewed),
            elapsed_sum_seconds=sum(durations), mean_seconds=mean(durations),
            median_seconds=statistics.median(durations) if durations else None,
            common_published_mean_seconds=mean([r['seconds'] for r in reviewed if r['pair_id'] in common]),
            tokens=sum(r['tokens'] for r in rs), unknown_usage_runs=sum(r['unknown_usage'] for r in rs),
            model_requests=sum(r['model_requests'] for r in rs), answer_repairs=sum(r['answer_repair_requests'] for r in rs),
            dimensions={d:dict(all_reviewed_n=sum(r['applicable'][d] for r in reviewed),
                all_case_utility_mean=mean([(r['scores'][d] or 0) for r in reviewed if r['applicable'][d]]),
                common_published_n=sum(r['pair_id'] in common and r['applicable'][d] for r in reviewed),
                common_published_mean=mean([r['scores'][d] for r in reviewed if r['pair_id'] in common and r['applicable'][d]])) for d in DIMENSIONS}))
    pairwise = []
    for a,b in combinations(by_variant,2):
        left={r['pair_id']:r for r in by_variant[a] if r['published'] and r['reviewed']}
        right={r['pair_id']:r for r in by_variant[b] if r['published'] and r['reviewed']}
        paired=sorted(left.keys() & right.keys())
        changes=[right[k]['utility_points']-left[k]['utility_points'] for k in paired]
        pairwise.append(dict(left=a,right=b,n=len(paired),right_better=sum(x>0 for x in changes),
            tied=sum(x==0 for x in changes),right_worse=sum(x<0 for x in changes),
            score_delta=sum(changes),pair_ids=paired))
    output=dict(rows=rows,variants=summary,common_published_pair_ids=sorted(common),pairwise=pairwise,
        complete=len(rows)==len(plan['cases']) and len(reviews)==len(plan['cases']),
        methodology=['同一冻结代码、20道B题、5份图纸、逐题相同预算；批量读取关闭，无模型回退。',
          '三档交错运行，总并发2；每题每档一次。不是多次随机重复实验，不能估计稳定胜率或唯一因果。',
          '0/1/2是六维有序评分，不是准确率。失败内容分null，运行效用按0计；未运行不当作失败。',
          '答案发布、实质部分交付、完整完成与内部task完成分别判断。完整完成要求全部适用维度2分。',
          '同时报告全体耗时和三档共同发布题耗时，避免提前失败造成表面提速。',
          '订阅Token为已报告用量，存在unknown_usage时不是完整费用，也不换算API美元。'])
    (root/'matrix-summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    result=summarize(parser.parse_args().root)
    print(json.dumps(dict(complete=result['complete'],variants=result['variants'],pairwise=result['pairwise']),ensure_ascii=False))
