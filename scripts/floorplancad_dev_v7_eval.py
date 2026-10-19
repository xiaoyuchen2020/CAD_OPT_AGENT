"""One frozen analysis batch, sealed selection, then fresh-family audit."""
import argparse,json,time
from collections import Counter,defaultdict
from concurrent.futures import ProcessPoolExecutor,as_completed
from dataclasses import asdict
from scripts.floorplancad_dev_v7_data import ROOT,OUT,save,sha,roster,install_guard,read
from scripts.floorplancad_benchmark import counts,summarize
from cad_agent.geometry.vector_recognition_v6 import apply_v6
from cad_agent.geometry.vector_recognition_v7 import build_v7,apply_v7,OwnershipV7
VARIANTS={
    'door_connected':OwnershipV7(door_policy='seed_connected'),
    'door_unique':OwnershipV7(door_policy='unique'),
    'door_connected_unique':OwnershipV7(door_policy='connected_unique'),
    'door_closed_contour':OwnershipV7(door_policy='closed_contour'),
    'cap_both_extensions':OwnershipV7(cap_policy='both_extensions'),
    'cap_any_extension':OwnershipV7(cap_policy='any_extension'),
    'cap_coincident':OwnershipV7(cap_policy='coincident'),
    'cap_both_or_coincident':OwnershipV7(cap_policy='both_or_coincident'),
    'combined':OwnershipV7(door_policy='connected_unique',cap_policy='both_or_coincident'),
}
NAMES=['baseline','door_reference','v5_full','v6_full',*VARIANTS]
SOURCES=['cad_agent/geometry/vector_recognition_v7.py','cad_agent/geometry/vector_recognition_v6.py','cad_agent/geometry/vector_recognition_v5.py','cad_agent/geometry/vector_recognition.py','cad_agent/geometry/vector_recognition_v3.py','cad_agent/geometry/vector_recognition_v4.py',
    'scripts/floorplancad_svg.py','scripts/floorplancad_prepare.py','scripts/floorplancad_benchmark.py','scripts/floorplancad_dev_v7_data.py','scripts/floorplancad_dev_v7_eval.py','scripts/floorplancad_dev_v7_diagnose.py','cad_agent/geometry/vector_adapter.py','tests/test_vector_recognition_v7.py']
SEALED=['protocol.json','analysis_roster.json','fresh_checks_roster.json','excluded_used_families.json','analysis_diagnosis.json']
def hashes():return {p:sha((ROOT/p).read_bytes()) for p in SOURCES}
def freeze():
    dest=OUT/'candidate_freeze.json'
    if dest.exists():raise ValueError('Immutable freeze')
    save(dest,dict(source_hashes=hashes(),data_hashes={p:sha((OUT/p).read_bytes()) for p in SEALED},variants={k:asdict(v) for k,v in VARIANTS.items()},
        baseline='v5 full comparator; v3, v5 independent door and v6 ownership controls',new_check_labels_read=False,parameters='One hypothesis batch without numerical search',created_unix=time.time()))
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
    phase,row=task;accesses=install_guard([row]);start=time.perf_counter();ps,t,di,gh=read(row)
    if row.get('geometry_sha256',gh)!=gh:raise ValueError('Geometry changed from isolation metadata')
    state=build_v7(ps);elapsed=time.perf_counter()-start
    predictions={'baseline':state['bundle']['base'],'door_reference':state['door_reference'],'v5_full':state['full'],'v6_full':apply_v6(state)}|{k:apply_v7(state,f) for k,f in VARIANTS.items()};out={}
    for k,result in predictions.items():
        out[k]=dict(door_roles_unchanged={e for e,v in result['entity_roles'].items() if v=='door'}=={e for e,v in state['door_reference']['entity_roles'].items() if v=='door'},window_roles_unchanged={e for e,v in result['entity_roles'].items() if v=='window'}=={e for e,v in state['full']['entity_roles'].items() if v=='window'},wall_roles_unchanged={e for e,v in result['entity_roles'].items() if v=='wall'}=={e for e,v in state['full']['entity_roles'].items() if v=='wall'},counts=counts(t,result['entity_roles']),instances=instances(t,result['entity_roles']),complete=result['complete'],wall_as_window_fp=sum(role=='window' and t[e]['category']=='wall' for e,role in result['entity_roles'].items()),candidate_counts=dict(Counter(c['kind'] for c in result['candidates'])))
        save(OUT/'predictions'/phase/k/(row['sample_id'].replace('/','__')+'.json'),result)
    result=dict(sample_id=row['sample_id'],group_id=row['group_id'],fold=row['fold'],source_sha256=row['sha256'],geometry_sha256=gh,primitives=len(ps),diagnostics=di,seconds=elapsed,variants=out,
        read_audit=dict(actual_dataset_reads=sorted(set(accesses)),svg_allowlist=[row['path']],labels_used_by_recognizer=False,old_checks_read=False,original_validation_read=False,test_read=False))
    save(OUT/'progress'/phase/(row['sample_id'].replace('/','__')+'.json'),result)
    return result
def aggregate(rows):
    out={}
    for name in NAMES:
        totals={c:Counter(tp=0,fp=0,fn=0) for c in ('door','window','wall')};it={c:Counter(total=0,any_hit=0,half_covered=0) for c in ('door','window')}
        for r in rows:
            v=r['variants'][name]
            for c,z in v['counts'].items():totals[c].update(z)
            for c,z in v['instances'].items():it[c].update(z)
        out[name]=dict(window_roles_unchanged=all(r['variants'][name]['window_roles_unchanged'] for r in rows),wall_roles_unchanged=all(r['variants'][name]['wall_roles_unchanged'] for r in rows),door_roles_unchanged=all(r['variants'][name]['door_roles_unchanged'] for r in rows),wall_as_window_fp=sum(r['variants'][name]['wall_as_window_fp'] for r in rows),micro=summarize(totals),instances={c:dict(z) for c,z in it.items()},incomplete_samples=sum(not r['variants'][name]['complete'] for r in rows))
    return out
def gates(a,control,baseline,component):
    x=a['micro'];y=control['micro']
    if component=='door':
        return dict(door_fp_reduction=x['door']['fp']<=y['door']['fp']*.8,
            door_tp_retention=x['door']['tp']>=y['door']['tp']*.95,
            door_precision=x['door']['precision']>=y['door']['precision'],
            door_f1=x['door']['f1']>=y['door']['f1'],
            door_precision_vs_v3=x['door']['precision']>=baseline['micro']['door']['precision']-.02,
            window_roles_unchanged=a['window_roles_unchanged'],wall_roles_unchanged=a['wall_roles_unchanged'],complete=a['incomplete_samples']==0)
    return dict(wall_as_window_fp_reduction=a['wall_as_window_fp']<=control['wall_as_window_fp']*.8,
        window_tp_retention=x['window']['tp']>=y['window']['tp']*.95,
        window_precision=x['window']['precision']>=y['window']['precision'],
        window_f1=x['window']['f1']>=y['window']['f1'],
        door_roles_unchanged=a['door_roles_unchanged'],wall_tp_nondecreasing=x['wall']['tp']>=y['wall']['tp'],
        wall_fp_nonincrease=x['wall']['fp']<=y['wall']['fp'],complete=a['incomplete_samples']==0)

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
        source_hashes=hashes(),gates={k:gates(totals[k],totals['v5_full'],totals['baseline'],'door' if k.startswith('door_') else 'window') for k in VARIANTS if k!='combined'},completed_unix=time.time())
    save(dest,result)
    if phase=='analysis':
        passing=[k for k,g in result['gates'].items() if all(g.values())]
        selected={}
        for component,prefix in [('door','door_'),('window','cap_')]:
            choices=[k for k in passing if k.startswith(prefix)]
            selected[component]=max(choices,key=lambda k:totals[k]['micro'][component]['f1']) if choices else None
        save(OUT/'selection_seal.json',dict(selected_components=selected,passing_variants=passing,analysis_sha256=sha(dest.read_bytes()),freeze_sha256=sha((OUT/'candidate_freeze.json').read_bytes()),sealed_unix=time.time(),fresh_check_tuning_allowed=False,
            decision='No component promotion if no variant passes all analysis gates; all frozen variants checked once without reselection. No public adapter changes.'))
    print(json.dumps(dict(totals=totals,gates=result['gates']),indent=2),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['freeze','analysis','fresh_checks']);p.add_argument('--workers',type=int)
    a=p.parse_args();freeze() if a.command=='freeze' else run(a.command,a.workers)
