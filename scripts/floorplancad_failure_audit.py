"""Fixed-analysis failure audit. No recognition changes or check-set command."""
import argparse,hashlib,json,math,sys,time
from collections import Counter,defaultdict
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
PREV=ROOT/'artifacts/floorplancad-vector-v7'
OUT=ROOT/'artifacts/floorplancad-mechanism-audit-v1'
BASE=ROOT/'artifacts/floorplancad-vector-v1'
PROTECTED=['cad_agent/geometry/vector_recognition.py']+[f'cad_agent/geometry/vector_recognition_v{i}.py' for i in (3,4,5,6,7)]+['cad_agent/geometry/vector_adapter.py','cad_agent/geometry/room_boundary.py','cad_agent/geometry/rooms.py','cad_agent/cad/render.py','scripts/floorplancad_svg.py']
PREDICTIONS=('baseline','door_reference','v5_full')
def sha(b):return hashlib.sha256(b).hexdigest()
def save(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2))
def roster():return json.loads((OUT/'analysis_roster.json').read_text())
def pred_path(r,name):return PREV/'predictions/analysis'/name/(r['sample_id'].replace('/','__')+'.json')
def forbidden(path,allowed):
    p=Path(path.decode() if isinstance(path,bytes) else path).resolve();s=str(p)
    if s in allowed:return False
    if p.suffix=='.svg':return True
    if 'validation' in p.parts or 'test' in p.parts or p.name.startswith('test-'):return True
    if p.suffix in ('.xz','.tar','.zip') and 'floorplancad' in s:return True
    if s.startswith(str(ROOT/'artifacts/floorplancad-vector')):return True
    if s.startswith('/Users/vigorchen/Documents/Work/临时数据/floorplancad'):return True
    return False

def guard(rows,extra=()):
    allowed={str(Path(r['path']).resolve()) for r in rows}|{str(Path(p).resolve()) for p in extra};opened=[]
    def hook(event,args):
        if event!='open' or not isinstance(args[0],(str,bytes)):return
        reading=(args[1] is None and (args[2]&3)!=1) or (isinstance(args[1],str) and ('r' in args[1] or '+' in args[1]))
        if not reading:return
        p=Path(args[0].decode() if isinstance(args[0],bytes) else args[0]).resolve();s=str(p)
        if forbidden(s,allowed):raise PermissionError('analysis-only audit blocks '+s)
        if p.suffix=='.svg' or s.startswith(str(ROOT/'artifacts/floorplancad-vector')):opened.append(s)
    sys.addaudithook(hook);return opened

def prepare():
    if (OUT/'protocol.json').exists():raise ValueError('Audit protocol exists')
    rows=json.loads((PREV/'analysis_roster.json').read_text());consumed=json.loads((PREV/'consumed_families_after_run.json').read_text());freeze=json.loads((PREV/'candidate_freeze.json').read_text())
    af={r['group_id'] for r in rows};checks=set(consumed['consumed_check_families'])
    assert len(rows)==len(af)==154 and len(checks)==486 and af.isdisjoint(checks)
    assert af==set(consumed['analysis_families']) and len(consumed['all_used_families'])==640
    for p in PROTECTED:
        if p in freeze['source_hashes']:assert sha((ROOT/p).read_bytes())==freeze['source_hashes'][p]
    for r in rows:
        assert r['split']=='development' and r['archive'] in ('train-00.tar.xz','train-01.tar.xz')
        assert Path(r['path']).resolve().is_relative_to(BASE/'dataset/svg/development')
    save(OUT/'analysis_roster.json',rows);save(OUT/'excluded_check_families.json',sorted(checks))
    allowed=[r['path'] for r in rows]+[str(pred_path(r,n)) for r in rows for n in PREDICTIONS]
    save(OUT/'allowed_data_paths.json',allowed)
    save(OUT/'protocol.json',dict(analysis_families=154,excluded_check_families=486,new_checks=0,metadata_only_sources=['v7 analysis_roster','v7 consumed_families_after_run','v7 candidate_freeze'],recognizer='unchanged v5 complete comparator with independent door completion, no host hard gate',
        taxonomy=['generation_no_target_candidate','filtering_candidate_lost','accepted_role_conflict','partial_part_assignment','half_coverage_or_more'],
        caveat='Stage audit uses overlap with at least one labeled source primitive, a generous candidate reachability measure, not object AP or causal proof. Half coverage is the old reporting threshold, not a new rule.',
        label_policy='Keep original annotations and all denominators. Raw ID 0 is an uncertainty signal, not automatically ignored or relabeled.',
        topology='Audit input/output contracts and exact unsnapped linework only; no room GT, seed, unit, closing edge or wall object is invented. No room accuracy claim.',
        case_selection='Deterministic hash-order two distinct families per mechanism stratum, no performance-based cherry-picking. Inspect source/candidate/annotation separately. Examples are diagnostic, not representative prevalence estimates.',
        protected_source_hashes={p:sha((ROOT/p).read_bytes()) for p in PROTECTED},audit_source_sha256=sha(Path(__file__).read_bytes()),allowed_paths_sha256=sha((OUT/'allowed_data_paths.json').read_bytes()),roster_sha256=sha((OUT/'analysis_roster.json').read_bytes()),created_unix=time.time()))
    print('Prepared analysis-only audit: 154 analysis, 486 excluded, 0 new checks')

def stage(raw_hits,accepted_hits,assigned,total):
    if not raw_hits:return 'generation_no_target_candidate'
    if not accepted_hits:return 'filtering_candidate_lost'
    if not assigned:return 'accepted_role_conflict'
    if assigned*2<total:return 'partial_part_assignment'
    return 'half_coverage_or_more'

def read(row):
    from scripts.floorplancad_svg import parse_svg
    p=Path(row['path'])
    if p.is_symlink() or row['split']!='development':raise ValueError('Non-analysis source')
    raw=p.read_bytes()
    if sha(raw)!=row['sha256']:raise ValueError('Source changed')
    return raw,parse_svg(raw)

def bounds(ids,byid):
    pts=[]
    for e in ids:
        if e not in byid:continue
        p=byid[e];pts.extend(p.points)
        if p.kind=='arc' and p.center and p.radius:pts.extend([(p.center[0]-p.radius,p.center[1]-p.radius),(p.center[0]+p.radius,p.center[1]+p.radius)])
    if not pts:return None
    xs,ys=zip(*pts);return [min(xs),min(ys),max(xs),max(ys)]

def worker(row):
    extras=[pred_path(row,n) for n in PREDICTIONS];opened=guard([row],extras);start=time.time()
    from scripts.floorplancad_svg import parse_svg
    from cad_agent.geometry.vector_recognition_v5 import build_v5,apply_v5
    raw,(ps,t,di,gh)=read(row);byid={p.entity_id:p for p in ps}
    preds={n:json.loads(pred_path(row,n).read_text()) for n in PREDICTIONS};final=preds['v5_full'];bundle=build_v5(ps);rebuilt=apply_v5(bundle)
    assert rebuilt['entity_roles']==final['entity_roles'] and rebuilt['complete']==final['complete']
    key=lambda c:(c['kind'],tuple(sorted(c['entity_ids'])))
    assert {key(c) for c in rebuilt['candidates']}=={key(c) for c in final['candidates']}
    pool={key(c):c for c in bundle['base']['candidates']+bundle['proposals']};pool=list(pool.values())
    raw_index={k:defaultdict(set) for k in ('door','window','wall')};accepted_index={k:defaultdict(set) for k in raw_index}
    for i,c in enumerate(pool):
        for e in c['entity_ids']:raw_index[c['kind']][e].add(i)
    for i,c in enumerate(final['candidates']):
        for e in c['entity_ids']:accepted_index[c['kind']][e].add(i)
    gt=defaultdict(list)
    for e,v in t.items():
        if v['category'] in ('door','window') and v['instance_id']!='-1':gt[(v['category'],v['raw_semantic_id'],v['instance_id'])].append(e)
    instances=[]
    for (kind,rawid,iid),ids in gt.items():
        pi=sorted(set().union(*(raw_index[kind][e] for e in ids)));ai=sorted(set().union(*(accepted_index[kind][e] for e in ids)))
        nhit=sum(final['entity_roles'].get(e)==kind for e in ids)
        arcs=[e for e in ids if e in byid and byid[e].kind=='arc'];eligible=[e for e in arcs if byid[e].radius and byid[e].sweep_degrees and 20<=abs(byid[e].sweep_degrees)<=160]
        instances.append(dict(category=kind,raw_semantic_id=rawid,instance_id=iid,entity_ids=ids,total=len(ids),parsed=sum(e in byid for e in ids),
            raw_candidate_indices=pi,accepted_candidate_indices=ai,assigned=nhit,stage=stage(pi,ai,nhit,len(ids)),
            own_annotation_arc_ids=arcs,own_annotation_eligible_swing_ids=eligible,bbox=bounds(ids,byid)))
    candrows=[];proxies={};growth=[]
    for i,c in enumerate(final['candidates']):
        kind=c['kind'];ids=c['entity_ids'];ev=c['evidence'];hist=Counter(str(t[e]['raw_semantic_id']) for e in ids);categories=Counter(t[e]['category'] for e in ids)
        target_instances=sorted({(t[e]['raw_semantic_id'],t[e]['instance_id']) for e in ids if t[e]['category']==kind and t[e]['instance_id']!='-1'})
        target=sum(t[e]['category']==kind for e in ids)
        candrows.append(dict(index=i,kind=kind,entity_ids=ids,target_parts=target,other_parts=len(ids)-target,raw_label_histogram=dict(hist),category_histogram=dict(categories),target_instances=target_instances,bbox=bounds(ids,byid),reason=c['reason']))
        if kind=='door' and all(k in ev for k in ('hinge','closed_tip')):
            axis=tuple(sorted(tuple(ev[k]) for k in ('hinge','closed_tip')));proxies.setdefault(axis,dict(hinge=ev['hinge'],closed_tip=ev['closed_tip'],candidate_indices=[],host_support_ids=set()))
            proxies[axis]['candidate_indices'].append(i);proxies[axis]['host_support_ids'].update(ev.get('host_support_ids',[]))
        if kind=='door' and 'added_part_ids' in ev:
            seed_gt={(t[e]['raw_semantic_id'],t[e]['instance_id']) for e in ev['seed_ids'] if t[e]['category']=='door'}
            for e in ev['added_part_ids']:
                if preds['baseline']['entity_roles'].get(e)=='door' or preds['door_reference']['entity_roles'].get(e)!='door':continue
                growth.append(dict(entity_id=e,raw_semantic_id=t[e]['raw_semantic_id'],category=t[e]['category'],candidate_index=i,seed_ids=ev['seed_ids'],same_labeled_door_instance=(t[e]['raw_semantic_id'],t[e]['instance_id']) in seed_gt if t[e]['category']=='door' else None))
    exact=defaultdict(list)
    for p in ps:
        if p.kind=='line':exact[tuple(sorted(p.points))].append(p.entity_id)
    coincident=[]
    for pts,ids in exact.items():
        if len(ids)<2:continue
        cats={t[e]['category'] for e in ids}
        coincident.append(dict(points=pts,entity_ids=ids,categories=sorted(cats),wall_window={'wall','window'}<=cats,raw_labels=sorted({t[e]['raw_semantic_id'] for e in ids}),instances=sorted({(t[e]['raw_semantic_id'],t[e]['instance_id']) for e in ids})))
    roles=final['entity_roles'];primitive={};pred_fp={}
    for kind in ('door','window','wall'):
        truth_ids={e for e,v in t.items() if v['category']==kind};union={e for e in raw_index[kind] if raw_index[kind][e]}
        primitive[kind]=dict(total=len(truth_ids),parsed=len(truth_ids&set(byid)),raw_pool_touched=len(truth_ids&union),accepted_touched=sum(bool(accepted_index[kind][e]) for e in truth_ids),assigned=sum(roles.get(e)==kind for e in truth_ids))
        pred_fp[kind]=dict(Counter(str(t[e]['raw_semantic_id']) for e,v in roles.items() if v==kind and t[e]['category']!=kind))
    root=ET.fromstring(raw);tags=Counter();attrs=set();room_attrs=set();links=[]
    for node in root.iter():
        tags[node.tag.rsplit('}',1)[-1]]+=1;attrs.update(node.attrib)
        for attr,val in node.attrib.items():
            if any(k in attr.lower() for k in ('room','unit','area','adjacen')):room_attrs.add(attr)
            if attr.rsplit('}',1)[-1]=='href':links.append(val)
    result=dict(sample_id=row['sample_id'],group_id=row['group_id'],fold=row['fold'],source_sha256=row['sha256'],geometry_sha256=gh,complete=final['complete'],parser_diagnostics=di,
        primitive=primitive,prediction_false_positive_raw_labels=pred_fp,raw_label_counts=dict(Counter(str(v['raw_semantic_id']) for v in t.values())),
        kind_by_label={k:dict(Counter(p.kind for p in ps if str(t[p.entity_id]['raw_semantic_id'])==k)) for k in {str(v['raw_semantic_id']) for v in t.values()}},
        instances=instances,candidates=candrows,coincident_groups=coincident,door_growth=growth,
        opening_axis_proxies=[p|dict(host_support_ids=sorted(p['host_support_ids'])) for p in proxies.values()],
        ambiguous_roles={e:sorted({c['kind'] for c in final['candidates'] if e in c['entity_ids']}) for e,v in roles.items() if v=='ambiguous'},
        window_rejections=final.get('window_rejections',[]),wall_rejections=final.get('wall_rejections',[]),
        input_schema=dict(tags=dict(tags),attribute_names=sorted(attrs),room_unit_adjacency_attribute_names=sorted(room_attrs),root_attributes=root.attrib,external_hrefs=links),
        raw_pool_counts=dict(Counter(c['kind'] for c in pool)),accepted_counts=dict(Counter(c['kind'] for c in final['candidates'])),
        actual_data_reads=sorted(set(opened)),seconds=time.time()-start,recognition_replay_matches_frozen=True)
    save(OUT/'samples'/(row['sample_id'].replace('/','__')+'.json'),result)
    return result

def summarize(rows):
    totals=defaultdict(Counter);raw=Counter();fp={k:Counter() for k in ('door','window','wall')};stages={k:Counter() for k in ('door','window')};byraw=defaultdict(Counter);families=defaultdict(set)
    for r in rows:
        raw.update(r['raw_label_counts'])
        for k,v in r['primitive'].items():totals['primitive_'+k].update(v)
        for k,v in r['prediction_false_positive_raw_labels'].items():fp[k].update(v)
        for obj in r['instances']:
            k=obj['category'];stages[k][obj['stage']]+=1;byraw[str(obj['raw_semantic_id'])][obj['stage']]+=1
            if obj['stage']=='generation_no_target_candidate':
                totals['generation_'+k]['no_own_labeled_arc' if not obj['own_annotation_arc_ids'] else 'arc_outside_existing_swing_range' if not obj['own_annotation_eligible_swing_ids'] else 'eligible_arc_but_no_candidate']+=1
            families[k+':'+obj['stage']].add(r['group_id'])
        for c in r['candidates']:
            k=c['kind'];totals['candidate_'+k]['total']+=1
            totals['candidate_'+k]['no_labeled_target' if not c['target_parts'] else 'mixed_parts' if c['other_parts'] else 'all_target_parts']+=1
            if len(c['target_instances'])>1:totals['candidate_'+k]['multiple_target_instances']+=1
        for g in r['coincident_groups']:
            totals['coincident']['groups']+=1
            if len(g['categories'])>1:totals['coincident']['mixed_category_groups']+=1
            if g['wall_window']:totals['coincident']['wall_window_groups']+=1;families['wall_window_coincident'].add(r['group_id'])
        grown={g['entity_id']:g for g in r['door_growth']}
        for g in grown.values():
            totals['door_growth'][g['category']]+=1
            if g['same_labeled_door_instance'] is False:totals['door_growth']['different_door_instance']+=1
        totals['schema_tags'].update(r['input_schema']['tags'])
        for k in r['input_schema']['room_unit_adjacency_attribute_names']:families['schema_attribute:'+k].add(r['group_id'])
        totals['topology_contract']['door_candidates']+=r['accepted_counts'].get('door',0)
        totals['topology_contract']['exact_unique_axis_proxies']+=len(r['opening_axis_proxies'])
        totals['topology_contract']['axis_with_host_witnesses']+=sum(bool(p['host_support_ids']) for p in r['opening_axis_proxies'])
        totals['topology_contract']['wall_candidates']+=r['accepted_counts'].get('wall',0)
        totals['topology_contract']['ambiguous_source_roles']+=len(r['ambiguous_roles'])
    return dict(samples=len(rows),complete_samples=sum(r['complete'] for r in rows),totals={k:dict(v) for k,v in totals.items()},instance_stages={k:dict(v) for k,v in stages.items()},raw_semantic_instance_stages={k:dict(v) for k,v in byraw.items()},raw_label_counts=dict(raw),prediction_false_positive_raw_labels={k:dict(v) for k,v in fp.items()},affected_families={k:len(v) for k,v in families.items()})

def run(workers):
    protocol=json.loads((OUT/'protocol.json').read_text());assert protocol['audit_source_sha256']==sha(Path(__file__).read_bytes())
    for p,h in protocol['protected_source_hashes'].items():assert sha((ROOT/p).read_bytes())==h
    if (OUT/'summary.json').exists():raise ValueError('Audit exists')
    rows=[]
    with ProcessPoolExecutor(max_workers=workers,max_tasks_per_child=1) as pool:
        jobs=[pool.submit(worker,r) for r in roster()]
        for f in as_completed(jobs):
            rows.append(f.result())
            if len(rows)%10==0 or len(rows)==154:print(f'analysis audit {len(rows)}/154',flush=True)
    rows.sort(key=lambda r:r['sample_id']);save(OUT/'summary.json',summarize(rows));save(OUT/'cohorts.json',{str(f):summarize([r for r in rows if r['fold']==f]) for f in (0,1,2)})
    print(json.dumps(summarize(rows),ensure_ascii=False,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','run']);p.add_argument('--workers',type=int,default=3);a=p.parse_args();prepare() if a.command=='prepare' else run(a.workers)
