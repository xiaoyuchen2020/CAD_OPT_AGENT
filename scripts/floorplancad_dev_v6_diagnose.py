"""Only the fixed 154 development analysis families; labels for diagnosis only."""
import json
from collections import Counter,defaultdict
from scripts.floorplancad_dev_v6_data import OUT,PREV5,roster,install_guard,read,save
from cad_agent.geometry.vector_recognition_v6 import PartContext

def run():
    rows=roster('analysis');paths=[PREV5/'predictions/analysis/full'/(r['sample_id'].replace('/','__')+'.json') for r in rows];basepaths=[PREV5/'predictions/analysis/door_parts_only'/(r['sample_id'].replace('/','__')+'.json') for r in rows];install_guard(rows,paths+basepaths)
    totals=defaultdict(Counter);details=[];candidate_totals=Counter()
    for row,path,basepath in zip(rows,paths,basepaths):
        ps,t,_,_=read(row);pred=json.loads(path.read_text());context=PartContext(ps);prior=json.loads(basepath.read_text());wall_returns={e for c in prior['candidates'] if c['kind']=='wall' for e in c['evidence'].get('return_entity_ids',[])};local=defaultdict(set);has_true=set();no_true=set()
        for c in pred['candidates']:
            if c['kind']!='window':continue
            profile=context.parts(c);profile['wall_return_cap_ids']=sorted(set(profile['cap_ids'])&wall_returns);wide=profile.get('span',0)<8*profile.get('spacing',0);profile['wide_cap_ids']=profile['cap_ids'] if wide else [];profile['wide_shared_cap_ids']=profile['shared_cap_ids'] if wide else [];mixed=any(t[e]['category']=='window' for e in c['entity_ids'])
            candidate_totals['with_true_window' if mixed else 'without_true_window']+=1
            for name in ['interior_ids','outer_ids','cap_ids','other_ids','continued_ids','shared_cap_ids','wall_return_cap_ids','wide_cap_ids','wide_shared_cap_ids']:
                for e in profile[name]:local[name,t[e]['category']].add(e)
            for e in c['entity_ids']:
                local['mixed_window' if mixed else 'no_true_window',t[e]['category']].add(e)
                local['two_inner_levels' if profile['distinct_interior_levels']>=2 else 'one_inner_level',t[e]['category']].add(e)
        for (name,cls),ids in local.items():totals[name][cls]+=len(ids)
        details.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts={f'{name}:{cls}':len(ids) for (name,cls),ids in local.items()}))
    result=dict(analysis_families=154,candidate_counts=dict(candidate_totals),totals={k:dict(v) for k,v in totals.items()},per_family=details,
        note='Classified by geometric part role and whether a candidate contains any true window primitive. Counts are unique within each role per file, roles overlap across candidates. Labels never reach the recognizer.')
    save(OUT/'analysis_cap_scale_diagnosis.json',result);print(json.dumps({k:v for k,v in result.items() if k!='per_family'},indent=2))
if __name__=='__main__':run()
