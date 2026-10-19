"""Allowed-analysis-only topology diagnostics; annotations never enter core."""
import json
from collections import Counter,defaultdict
from scripts.floorplancad_dev_v5_data import OUT,PREV,roster,install_guard,read,save
from cad_agent.geometry.vector_recognition_v5 import WindowContext

def run():
    rows=roster('analysis');paths=[PREV/'predictions/analysis/combined'/(r['sample_id'].replace('/','__')+'.json') for r in rows];install_guard(rows,paths)
    groups=defaultdict(Counter);per_family=[]
    for row,path in zip(rows,paths):
        ps,truth,_,_=read(row);pred=json.loads(path.read_text());context=WindowContext(ps);local=defaultdict(set)
        for c in pred['candidates']:
            if c['kind']!='window':continue
            p=context.profile(c);typ='through' if p['through_rail_ids'] else 'no_through';cross='crossing' if p['crossing_ids'] else 'no_crossing'
            inner='independent_inner' if p['independent_interior_ids'] else 'no_independent_inner'
            for eid in c['entity_ids']:
                cls=truth[eid]['category'];local[(typ,cls)].add(eid);local[(cross,cls)].add(eid);local[(inner,cls)].add(eid)
            groups['candidates'][typ]+=1;groups['candidates'][cross]+=1;groups['candidates'][inner]+=1
        for (typ,cls),ids in local.items():groups[typ][cls]+=len(ids)
        per_family.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts={f'{typ}:{cls}':len(ids) for (typ,cls),ids in local.items()}))
    result=dict(samples=len(rows),totals={k:dict(v) for k,v in groups.items()},per_family=per_family,
        note='Only 154 analysis families. Categories counted uniquely within each profile per file; profiles may overlap on shared primitives. Fixed geometric continuity/junction probes, no threshold sweep.')
    save(OUT/'analysis_geometry_diagnosis.json',result);print(json.dumps(result['totals'],indent=2))
if __name__=='__main__':run()
