"""Frozen feature ablations and family checks wholly inside development data."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import time

from cad_agent.geometry.vector_recognition_v3 import recognize_vectors_v3,Refinements
from scripts.floorplancad_benchmark import counts,summarize
from scripts.floorplancad_dev_v2 import ROOT,BASE,OUT,sha,save,load_rows,install_guard,read

OUT=OUT/'refined'

VARIANTS={
    'radial_only':Refinements(True,False,False,False),
    'swing_only':Refinements(False,True,False,False),
    'caps_only':Refinements(False,False,True,False),
    'wall_only':Refinements(False,False,False,True),
    'full':Refinements(),
    'no_radial':Refinements(False,True,True,True),
    'no_swing':Refinements(True,False,True,True),
    'no_caps':Refinements(True,True,False,True),
    'no_wall':Refinements(True,True,True,False),
}
SOURCES=['cad_agent/geometry/vector_recognition.py','cad_agent/geometry/vector_recognition_v3.py',
         'scripts/floorplancad_svg.py','scripts/floorplancad_dev_v2.py','scripts/floorplancad_dev_v3_eval.py',
         'scripts/floorplancad_benchmark.py']


def hashes():return {p:sha((ROOT/p).read_bytes()) for p in SOURCES}


def freeze():
    dest=OUT/'candidate_freeze.json'
    if dest.exists():raise ValueError('Freeze is immutable')
    save(dest,dict(source_hashes=hashes(),variants={k:asdict(v) for k,v in VARIANTS.items()},
        roster_sha256=sha((OUT/'development_roster.json').read_bytes()),
        protocol_sha256=sha((OUT/'protocol.json').read_bytes()),
        selection='Among variants passing all analysis-fold gates, maximize mean class F1. If none pass, no promoted variant.',
        gates=dict(wall_fp_reduction_min=.2,wall_tp_retention_min=.8,
            door_window_recall_increase=True,door_window_precision_drop_max=.05,all_class_f1_nondecreasing=True),
        tuning='One predeclared feature ablation batch; no numerical parameter search; checks 3-4 after selection seal only.',
        original_validation_access=False,test_access=False))
    for rel in SOURCES:
        p=OUT/'frozen_source'/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/rel).read_bytes())


def check():
    c=json.loads((OUT/'candidate_freeze.json').read_text())
    if c['source_hashes']!=hashes():raise ValueError('Frozen implementation changed')
    if c['roster_sha256']!=sha((OUT/'development_roster.json').read_bytes()):raise ValueError('Roster changed')
    if c['protocol_sha256']!=sha((OUT/'protocol.json').read_bytes()):raise ValueError('Protocol changed')


def instance_counts(truth,roles):
    targets=defaultdict(list)
    for eid,t in truth.items():
        if t['category'] in ('door','window') and t['instance_id']!='-1':
            targets[t['category'],t['raw_semantic_id'],t['instance_id']].append(eid)
    out={c:dict(total=0,any_hit=0,half_covered=0) for c in ('door','window')}
    for (cls,_,_),ids in targets.items():
        hit=sum(roles.get(eid)==cls for eid in ids);out[cls]['total']+=1
        out[cls]['any_hit']+=hit>0;out[cls]['half_covered']+=hit/len(ids)>=.5
    return out


def worker(row):
    install_guard([row]);ps,t,diagnostics,gh=read(row)
    old=json.loads((BASE/'baseline/development/predictions'/(row['sample_id'].replace('/','__')+'.json')).read_text())
    variants={'v1':old};times={'v1':0.}
    for name,features in VARIANTS.items():
        start=time.perf_counter();variants[name]=recognize_vectors_v3(ps,features=features);times[name]=time.perf_counter()-start
    out={}
    for name,result in variants.items():
        out[name]=dict(counts=counts(t,result['entity_roles']),instances=instance_counts(t,result['entity_roles']),
            complete=result['complete'],seconds=times[name],candidate_counts=dict(Counter(c['kind'] for c in result['candidates'])),
            rejected_wall_pairs=len(result.get('wall_rejections',[])))
        # Full evidence retained for inspection and reproducing all ablations.
        save(OUT/'predictions'/name/(row['sample_id'].replace('/','__')+'.json'),result)
    return dict(sample_id=row['sample_id'],group_id=row['group_id'],fold=row['fold'],source_sha256=row['sha256'],
                geometry_sha256=gh,primitives=len(t),parser_diagnostics=diagnostics,variants=out)


def aggregate(rows):
    result={}
    for variant in ['v1',*VARIANTS]:
        totals={c:Counter(tp=0,fp=0,fn=0) for c in ('door','window','wall')}
        inst={c:Counter(total=0,any_hit=0,half_covered=0) for c in ('door','window')}
        for r in rows:
            v=r['variants'][variant]
            for cls,c in v['counts'].items():totals[cls].update(c)
            for cls,c in v['instances'].items():inst[cls].update(c)
        result[variant]=dict(micro=summarize(totals),instances={c:dict(v,half_coverage_recall=v['half_covered']/v['total'] if v['total'] else None) for c,v in inst.items()},
            incomplete_samples=sum(not r['variants'][variant]['complete'] for r in rows),
            seconds=sum(r['variants'][variant]['seconds'] for r in rows))
    return result


def gates(current,base):
    a=current['micro'];b=base['micro']
    return dict(wall_fp_reduction=a['wall']['fp']<=b['wall']['fp']*.8,
        wall_tp_retention=a['wall']['tp']>=b['wall']['tp']*.8,
        door_recall=a['door']['recall']>b['door']['recall'],window_recall=a['window']['recall']>b['window']['recall'],
        door_precision=(a['door']['precision'] or 0)>=(b['door']['precision'] or 0)-.05,
        window_precision=(a['window']['precision'] or 0)>=(b['window']['precision'] or 0)-.05,
        all_f1=all((a[c]['f1'] or 0)>=(b[c]['f1'] or 0) for c in b),
        complete=current['incomplete_samples']==0)


def run(phase,workers=3):
    check();all_rows=load_rows()
    folds=(0,1,2) if phase=='analysis' else (3,4)
    if phase=='checks':
        seal=json.loads((OUT/'selection_seal.json').read_text())
        if seal['analysis_sha256']!=sha((OUT/'analysis_results.json').read_bytes()):raise ValueError('Analysis selection changed')
        if seal['freeze_sha256']!=sha((OUT/'candidate_freeze.json').read_bytes()):raise ValueError('Freeze changed')
    dest=OUT/(phase+'_results.json')
    if dest.exists():raise ValueError('Do not overwrite evaluated results')
    selected=[r for r in all_rows if r['fold'] in folds]
    # One task per worker process: read guard is scoped to that exact SVG sample.
    # max_tasks_per_child avoids accumulating guards for different samples.
    rows=[]
    with ProcessPoolExecutor(max_workers=workers,max_tasks_per_child=1) as pool:
        for i,result in enumerate(pool.map(worker,selected)):
            rows.append(result)
            if i%10==0:print(f'{phase}: {i+1}/{len(selected)}',flush=True)
    totals=aggregate(rows)
    report=dict(phase=phase,samples=len(rows),families=len({r['group_id'] for r in rows}),
        folds={str(f):aggregate([r for r in rows if r['fold']==f]) for f in folds},
        totals=totals,sample_results=rows,source_hashes=hashes(),
        test_access=False,original_validation_access=False,
        gates={v:gates(t,totals['v1']) for v,t in totals.items() if v!='v1'})
    save(dest,report)
    if phase=='analysis':
        passed=[v for v,g in report['gates'].items() if all(g.values())]
        selected=max(passed,key=lambda v:sum(totals[v]['micro'][c]['f1'] or 0 for c in ('door','window','wall'))) if passed else None
        save(OUT/'selection_seal.json',dict(selected_variant=selected,passing_variants=passed,
            analysis_sha256=sha(dest.read_bytes()),freeze_sha256=sha((OUT/'candidate_freeze.json').read_bytes()),
            checks_tuning_allowed=False))
    print(json.dumps(dict(totals=totals,gates=report['gates']),indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['freeze','analysis','checks']);p.add_argument('--workers',type=int,default=3)
    a=p.parse_args()
    freeze() if a.command=='freeze' else run(a.command,a.workers)
