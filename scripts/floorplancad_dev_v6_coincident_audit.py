"""Analysis-only exact coincident source-label audit; no recognition changes."""
import json
from collections import defaultdict,Counter
from scripts.floorplancad_dev_v6_data import OUT,roster,install_guard,read,save

def run():
    rows=roster('analysis');install_guard(rows);totals=Counter();examples=[];families=set()
    for row in rows:
        ps,truth,_,_=read(row);groups=defaultdict(list)
        for p in ps:
            if p.kind=='line':groups[tuple(sorted(p.points))].append(p.entity_id)
        name=row['sample_id'].replace('/','__')+'.json'
        before=json.loads((OUT/'predictions/analysis/v5_full'/name).read_text())['entity_roles'];after=json.loads((OUT/'predictions/analysis/full'/name).read_text())['entity_roles']
        for points,ids in groups.items():
            cats={truth[e]['category'] for e in ids}
            if not {'wall','window'}<=cats:continue
            families.add(row['group_id']);totals['exact_wall_window_coincident_groups']+=1
            totals['wall_primitives_in_groups']+=sum(truth[e]['category']=='wall' for e in ids)
            totals['window_primitives_in_groups']+=sum(truth[e]['category']=='window' for e in ids)
            for phase,roles in [('v5',before),('v6_full',after)]:
                totals[phase+'_wall_as_window_in_coincident_groups']+=sum(truth[e]['category']=='wall' and roles.get(e)=='window' for e in ids)
                totals[phase+'_window_tp_in_coincident_groups']+=sum(truth[e]['category']=='window' and roles.get(e)=='window' for e in ids)
            examples.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],points=points,
                sources=[dict(entity_id=e,raw_semantic_id=truth[e]['raw_semantic_id'],category=truth[e]['category'],instance_id=truth[e]['instance_id'],v5_role=before.get(e),v6_role=after.get(e)) for e in ids]))
    totals['affected_analysis_families']=len(families)
    result=dict(phase='analysis_only_post_freeze',totals=dict(totals),groups=examples,
        method='Exact parsed line endpoints, reversal invariant, no rounding or tolerance. No claim that either label is wrong; overlapping source parts may belong to different semantic objects. Typed geometry without object membership cannot distinguish coincident copies by geometry alone.',
        recognizer_or_selection_changed=False)
    save(OUT/'analysis_coincident_label_audit.json',result);print(json.dumps(result['totals'],indent=2))
if __name__=='__main__':run()
