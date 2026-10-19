"""Audit omitted metadata and existing candidate rejection paths; no rule changes."""
import argparse,json,re,time
import xml.etree.ElementTree as ET
from collections import Counter,defaultdict
from concurrent.futures import ProcessPoolExecutor,as_completed
from pathlib import Path
from scripts.floorplancad_failure_audit import ROOT,OUT,roster,guard,read,save,sha

def metadata():
    from scripts.floorplancad_svg import SHAPES,transform_matrix,matmul,point,IDENTITY
    rows=roster();opened=guard(rows);texts=[];groups=[];duplicates=[];physical=Counter()
    for row in rows:
        raw,(ps,t,_,_)=read(row);root=ET.fromstring(raw);source={};counter=0
        def visit(node,path,parents,matrix):
            nonlocal counter
            tag=node.tag.rsplit('}',1)[-1];attrs=node.attrib;m=matmul(matrix,transform_matrix(attrs.get('transform','')))
            group=dict(node_path=path,id=attrs.get('id'),label=next((v for k,v in attrs.items() if k.rsplit('}',1)[-1]=='label'),None)) if tag=='g' else None
            parents=parents+[group] if group else parents
            if group:groups.append(dict(sample_id=row['sample_id'],**group))
            if tag in SHAPES:source['p'+str(counter)]=dict(node_path=path,groups=parents);counter+=1
            if tag=='text':
                value=''.join(node.itertext()).strip();xy=None
                try:xy=point(m,(float(attrs['x']),float(attrs['y'])))
                except (KeyError,ValueError):pass
                texts.append(dict(sample_id=row['sample_id'],source_sha256=row['sha256'],node_path=path,text=value,attributes=attrs,groups=parents,matrix=m,baseline_xy=xy,bbox=None,physical_unit_confirmed=False,interpretation='unreviewed_source_text'))
            for i,ch in enumerate(node):visit(ch,path+'/'+str(i),parents,m)
        visit(root,'root',[],IDENTITY)
        sample=json.loads((OUT/'samples'/(row['sample_id'].replace('/','__')+'.json')).read_text())
        for g in sample['coincident_groups']:
            if not g['wall_window']:continue
            paths={tuple(v['node_path'] for v in source[e]['groups']) for e in g['entity_ids']}
            duplicates.append(dict(sample_id=row['sample_id'],entity_ids=g['entity_ids'],distinct_group_paths=len(paths),source_metadata={e:source[e]|dict(annotation=t[e]) for e in g['entity_ids']}))
    values=Counter(t['text'] for t in texts)
    out=dict(text_nodes=len(texts),families_with_text=len({t['sample_id'] for t in texts}),texts_with_baseline=sum(t['baseline_xy'] is not None for t in texts),
        group_nodes=len(groups),nonempty_group_labels=sum(bool(g['label']) for g in groups),top_text_values=values.most_common(30),
        room_name_examples=[dict(text=name,count=values[name],sources=[{k:t[k] for k in ('sample_id','source_sha256','node_path','baseline_xy')} for t in texts if t['text']==name]) for name in ['卫生间','厨房','卧室','强电间','弱电间','走道']],
        wall_window_coincident_groups=len(duplicates),wall_window_groups_with_different_source_groups=sum(g['distinct_group_paths']>1 for g in duplicates),
        texts=texts,groups=groups,coincident_source_groups=duplicates,actual_data_reads=sorted(set(opened)),
        scope='Metadata availability audit only. Text baseline is not room centroid/bbox; SVG layers are not verified building-object membership. No recognition uses these fields and no unit is assumed.')
    save(OUT/'source_metadata_audit.json',out);print(json.dumps({k:v for k,v in out.items() if k not in ('texts','groups','coincident_source_groups','actual_data_reads','room_name_examples')},ensure_ascii=False,indent=2))

def rejection_worker(row):
    opened=guard([row]);raw,(ps,t,_,_)=read(row)
    from cad_agent.geometry.vector_recognition_v5 import build_v5
    b=build_v5(ps);key=lambda c:(c['kind'],tuple(sorted(c['entity_ids'])))
    pool={key(c):c for c in b['base']['candidates']+b['proposals']};pool=list(pool.values());basekeys={key(c) for c in b['base']['candidates']}
    sample=json.loads((OUT/'samples'/(row['sample_id'].replace('/','__')+'.json')).read_text());results=[]
    for obj in sample['instances']:
        if obj['stage']!='filtering_candidate_lost':continue
        reasons=set();details=[]
        for i in obj['raw_candidate_indices']:
            c=pool[i];assert set(c['entity_ids'])&set(obj['entity_ids']) and c['kind']==obj['category'];why=[];isbase=key(c) in basekeys
            if c['kind']=='window':
                if not isbase:
                    if not c['evidence']['host_supported']:why.append('external_host_condition_not_met')
                    if not c['evidence']['topology_supported']:why.append('two_cap_topology_condition_not_met')
                if not b['window_profiles'][tuple(sorted(c['entity_ids']))]['independent_interior_ids']:why.append('independent_interior_rail_condition_not_met')
            elif not c['evidence'].get('topology_supported',True):why.append('no_observed_door_leaf_seed')
            assert why
            reasons.update(why);details.append(dict(raw_candidate_index=i,origin='v3_base' if isbase else 'relation_proposal',candidate=c,blocking_conditions=why))
        results.append(dict(category=obj['category'],instance_id=obj['instance_id'],raw_semantic_id=obj['raw_semantic_id'],entity_ids=obj['entity_ids'],blocking_conditions=sorted(reasons),raw_candidates=details))
    result=dict(sample_id=row['sample_id'],group_id=row['group_id'],instances=results,actual_data_reads=opened)
    save(OUT/'rejection_paths'/(row['sample_id'].replace('/','__')+'.json'),result);return result

def rejections():
    rows=[]
    for row in roster():
        r=json.loads((OUT/'samples'/(row['sample_id'].replace('/','__')+'.json')).read_text())
        if any(i['stage']=='filtering_candidate_lost' for i in r['instances']):rows.append(row)
    results=[]
    with ProcessPoolExecutor(max_workers=3,max_tasks_per_child=1) as pool:
        for i,f in enumerate(as_completed([pool.submit(rejection_worker,r) for r in rows])):
            results.append(f.result())
            if (i+1)%10==0:print('rejection-path audit',i+1,'/',len(rows),flush=True)
    counts=defaultdict(Counter);combinations=defaultdict(Counter)
    for r in results:
        for i in r['instances']:counts[i['category']].update(i['blocking_conditions']);combinations[i['category']][' + '.join(i['blocking_conditions'])]+=1
    save(OUT/'rejection_path_summary.json',dict(families=len(rows),instance_blocking_conditions={k:dict(v) for k,v in counts.items()},instance_reason_combinations={k:dict(v) for k,v in combinations.items()},note='Per-instance blocking-condition unions across all touching candidates. Reasons may overlap, not independent causal attribution. Only existing frozen logic replayed.',samples=results));print(json.dumps({k:dict(v) for k,v in counts.items()},indent=2))
def label_presence():
    from scripts.floorplancad_svg import SHAPES
    from scripts.floorplancad_failure_audit import pred_path
    rows=roster();opened=guard(rows,[pred_path(r,'v5_full') for r in rows]);totals=Counter();fps=defaultdict(Counter);per=[]
    for row in rows:
        raw,(ps,t,_,_)=read(row);root=ET.fromstring(raw);i=0;local=Counter();zero=[];pred=json.loads(pred_path(row,'v5_full').read_text())
        for node in root.iter():
            if node.tag.rsplit('}',1)[-1] not in SHAPES:continue
            eid='p'+str(i);i+=1;local['shapes']+=1
            source='explicit_zero' if node.get('semantic-id')=='0' else 'missing_semantic_attribute' if 'semantic-id' not in node.attrib else 'explicit_nonzero'
            local[source]+=1
            role=pred['entity_roles'].get(eid)
            if role in ('door','window','wall') and t[eid]['category']!=role:
                fps[role][source]+=1
                if t[eid]['raw_semantic_id']==0:zero.append(dict(entity_id=eid,predicted_role=role,annotation_origin=source,raw_attributes=node.attrib))
        totals.update(local);per.append(dict(sample_id=row['sample_id'],counts=dict(local),zero_label_false_positives=zero))
    save(OUT/'label_presence_audit.json',dict(totals=dict(totals),false_positive_annotation_origin={k:dict(v) for k,v in fps.items()},samples=per,actual_data_reads=sorted(set(opened)),note='Missing semantic-id is mapped to 0 by the unchanged parser. This audit distinguishes omission from explicit 0 without changing any scoring label.'))
    print(json.dumps(dict(totals=dict(totals),false_positive_annotation_origin={k:dict(v) for k,v in fps.items()}),indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['metadata','rejections','label_presence']);a=p.parse_args();{'metadata':metadata,'rejections':rejections,'label_presence':label_presence}[a.command]()
