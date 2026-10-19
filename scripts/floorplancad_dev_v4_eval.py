"""One frozen analysis batch, sealed selection, then fresh-family audit."""
import argparse,json,time
from collections import Counter,defaultdict
from concurrent.futures import ProcessPoolExecutor,as_completed
from dataclasses import asdict
from scripts.floorplancad_dev_v4_data import ROOT,OUT,save,sha,roster,install_guard,read
from scripts.floorplancad_benchmark import counts,summarize
from cad_agent.geometry.vector_recognition_v4 import build_relations,apply_relations,Relations
VARIANTS={'host_only':Relations(True,False,False,False),'topology_only':Relations(False,True,True,True),
    'combined':Relations(),'no_door_parts':Relations(True,True,False,True),'no_window_caps':Relations(True,True,True,False)}
SOURCES=['cad_agent/geometry/vector_recognition.py','cad_agent/geometry/vector_recognition_v3.py','cad_agent/geometry/vector_recognition_v4.py',
    'scripts/floorplancad_svg.py','scripts/floorplancad_benchmark.py','scripts/floorplancad_dev_v4_data.py','scripts/floorplancad_dev_v4_eval.py']
SEALED=['protocol.json','analysis_roster.json','fresh_checks_roster.json','excluded_used_families.json']
def hashes():return {p:sha((ROOT/p).read_bytes()) for p in SOURCES}
def freeze():
    dest=OUT/'candidate_freeze.json'
    if dest.exists():raise ValueError('Immutable freeze')
    save(dest,dict(source_hashes=hashes(),data_hashes={p:sha((OUT/p).read_bytes()) for p in SEALED},variants={k:asdict(v) for k,v in VARIANTS.items()},
        baseline='v3 no_caps, unchanged',new_check_labels_read=False,parameters='One hypothesis batch without numerical search',created_unix=time.time()))
    for rel in SOURCES:
        dest=OUT/'frozen_source'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/rel).read_bytes())
def check_freeze():
    f=json.loads((OUT/'candidate_freeze.json').read_text())
    if f['source_hashes']!=hashes():raise ValueError('Source changed since freeze')
    if f['data_hashes']!={p:sha((OUT/p).read_bytes()) for p in SEALED}:raise ValueError('Roster/protocol changed')
def instances(truth,roles):
    targets=defaultdict(list)
    for e,t in truth.items():
        if t['category'] in ('door','window') and t['instance_id']!='-1':targets[t['category'],t['raw_semantic_id'],t['instance_id']].append(e)
    out={c:dict(total=0,any_hit=0,half_covered=0) for c in ('door','window')}
    for (c,_,_),ids in targets.items():
        n=sum(roles.get(e)==c for e in ids);out[c]['total']+=1;out[c]['any_hit']+=n>0;out[c]['half_covered']+=n/len(ids)>=.5
    return out
def worker(task):
    phase,row=task;install_guard([row]);start=time.perf_counter();ps,t,di,gh=read(row)
    bundle=build_relations(ps);elapsed=time.perf_counter()-start
    predictions={'baseline':bundle['base']}|{k:apply_relations(bundle,f) for k,f in VARIANTS.items()};out={}
    for k,result in predictions.items():
        out[k]=dict(counts=counts(t,result['entity_roles']),instances=instances(t,result['entity_roles']),complete=result['complete'],candidate_counts=dict(Counter(c['kind'] for c in result['candidates'])))
        save(OUT/'predictions'/phase/k/(row['sample_id'].replace('/','__')+'.json'),result)
    result=dict(sample_id=row['sample_id'],group_id=row['group_id'],fold=row['fold'],source_sha256=row['sha256'],geometry_sha256=gh,primitives=len(ps),diagnostics=di,seconds=elapsed,variants=out,
        read_audit=dict(svg_allowlist=[row['path']],labels_used_by_recognizer=False,old_checks_read=False,original_validation_read=False,test_read=False))
    save(OUT/'progress'/phase/(row['sample_id'].replace('/','__')+'.json'),result)
    return result
def aggregate(rows):
    out={}
    for name in ['baseline',*VARIANTS]:
        totals={c:Counter(tp=0,fp=0,fn=0) for c in ('door','window','wall')};it={c:Counter(total=0,any_hit=0,half_covered=0) for c in ('door','window')}
        for r in rows:
            v=r['variants'][name]
            for c,z in v['counts'].items():totals[c].update(z)
            for c,z in v['instances'].items():it[c].update(z)
        out[name]=dict(micro=summarize(totals),instances={c:dict(z) for c,z in it.items()},incomplete_samples=sum(not r['variants'][name]['complete'] for r in rows))
    return out
def gates(a,b):
    x=a['micro'];y=b['micro']
    return dict(door_recall=x['door']['recall']>y['door']['recall'],window_recall=x['window']['recall']>y['window']['recall'],
        door_precision=(x['door']['precision'] or 0)>=(y['door']['precision'] or 0)-.02,window_precision=(x['window']['precision'] or 0)>=(y['window']['precision'] or 0)-.02,
        wall_tp_retention=x['wall']['tp']>=y['wall']['tp']*.98,wall_fp_nonincrease=x['wall']['fp']<=y['wall']['fp'],
        all_f1=all((x[c]['f1'] or 0)>=(y[c]['f1'] or 0) for c in y),complete=a['incomplete_samples']==0)
def run(phase,workers):
    check_freeze();dest=OUT/(phase+'_results.json')
    if dest.exists():raise ValueError('Results immutable')
    if phase=='fresh_checks':
        seal=json.loads((OUT/'selection_seal.json').read_text())
        if seal['analysis_sha256']!=sha((OUT/'analysis_results.json').read_bytes()) or seal['freeze_sha256']!=sha((OUT/'candidate_freeze.json').read_bytes()):raise ValueError('Selection seal changed')
    selected=roster(phase);results=[]
    with ProcessPoolExecutor(max_workers=3 if workers is None else workers,max_tasks_per_child=1) as pool:
        futures=[pool.submit(worker,(phase,r)) for r in selected]
        for f in as_completed(futures):
            results.append(f.result())
            if len(results)%10==0 or len(results)==len(selected):print(f'{phase}: {len(results)}/{len(selected)}',flush=True)
    results.sort(key=lambda r:r['sample_id']);totals=aggregate(results)
    result=dict(phase=phase,samples=len(results),families=len({r['group_id'] for r in results}),totals=totals,
        cohorts={str(k):aggregate([r for r in results if r['fold']==k]) for k in sorted({r['fold'] for r in results})},sample_results=results,
        source_hashes=hashes(),gates={k:gates(totals[k],totals['baseline']) for k in VARIANTS},completed_unix=time.time())
    save(dest,result)
    if phase=='analysis':
        passing=[k for k,g in result['gates'].items() if all(g.values())]
        winner=max(passing,key=lambda k:sum(totals[k]['micro'][c]['f1'] or 0 for c in ('door','window','wall'))) if passing else None
        save(OUT/'selection_seal.json',dict(selected_variant=winner,passing_variants=passing,analysis_sha256=sha(dest.read_bytes()),freeze_sha256=sha((OUT/'candidate_freeze.json').read_bytes()),sealed_unix=time.time(),fresh_check_tuning_allowed=False,
            decision='No promotion if no variant passes all gates; all frozen variants checked for diagnosis only.'))
    print(json.dumps(dict(totals=totals,gates=result['gates']),indent=2),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['freeze','analysis','fresh_checks']);p.add_argument('--workers',type=int)
    a=p.parse_args();freeze() if a.command=='freeze' else run(a.command,a.workers)
