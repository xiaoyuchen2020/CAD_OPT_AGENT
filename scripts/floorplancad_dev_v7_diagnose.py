"""Allowlisted 154-family diagnosis; no held-out outputs are read."""
import json
from collections import Counter,defaultdict
from scripts.floorplancad_dev_v7_data import OUT,PREV6,roster,read,install_guard,save
from cad_agent.geometry.vector_recognition_v7 import LocalParts
from cad_agent.geometry.vector_recognition_v6 import PartContext

def run():
    rows=roster('analysis');paths=[PREV6/'predictions/analysis'/v/(r['sample_id'].replace('/','__')+'.json') for r in rows for v in ('baseline','door_reference','v5_full')];install_guard(rows,paths)
    totals=defaultdict(Counter);details=[]
    for i,row in enumerate(rows):
        ps,t,_,_=read(row);ctx=LocalParts(ps);pc=PartContext(ps);name=row['sample_id'].replace('/','__')+'.json'
        preds={v:json.loads((PREV6/'predictions/analysis'/v/name).read_text()) for v in ('baseline','door_reference','v5_full')}
        local=defaultdict(set);candidates=[]
        for c in preds['door_reference']['candidates']:
            ev=c['evidence']
            if c['kind']!='door' or 'added_part_ids' not in ev:continue
            profiles=ctx.door(c)
            seed_true=all(t[e]['category']=='door' for e in ev['seed_ids'])
            seed_any=any(t[e]['category']=='door' for e in ev['seed_ids'])
            for e,p in profiles.items():
                if preds['baseline']['entity_roles'].get(e)=='door' or preds['door_reference']['entity_roles'].get(e)!='door':continue
                cat=t[e]['category']
                local['door_added_raw_semantic',str(t[e]['raw_semantic_id'])].add(e)
                for key in ['door_added','door_seed_connected' if p['seed_connected'] else 'door_anchor_only','door_'+p['orientation'],'door_'+p['location'], 'door_contour' if p['closed_contour'] else 'door_dangling','door_external' if p['external_connection_ids'] else 'door_no_external', 'door_coincident' if p['coincident_ids'] else 'door_unique','door_seed_all_true' if seed_true else 'door_seed_mixed' if seed_any else 'door_seed_false']:
                    local[key,cat].add(e)
                if p['coincident_seed']:local['door_coincident_seed',cat].add(e)
            candidates.append(dict(kind='door',ids=c['entity_ids'],evidence=ev,profiles=profiles))
        for c in preds['v5_full']['candidates']:
            if c['kind']!='window':continue
            profile=pc.parts(c);ext=ctx.cap_extensions(c,profile)
            for e,p in ext.items():
                cat=t[e]['category'];local['window_cap',cat].add(e)
                for key,cond in [('window_cap_one_extension',bool(p['left'] or p['right'])),('window_cap_both_extensions',bool(p['left'] and p['right'])),('window_cap_coincident',bool(p['coincident_ids'])),('window_cap_extension_and_unique',bool(p['left'] or p['right']) and not p['coincident_ids'])]:
                    if cond:local[key,cat].add(e)
            candidates.append(dict(kind='window',ids=c['entity_ids'],profiles=profile,cap_extensions=ext))
        for (key,cat),ids in local.items():totals[key][cat]+=len(ids)
        details.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts={key+':'+cat:len(ids) for (key,cat),ids in local.items()},candidates=candidates))
        if (i+1)%30==0:print(f'analysis diagnosis {i+1}/154',flush=True)
    save(OUT/'analysis_diagnosis.json',dict(analysis_families=154,totals={k:dict(v) for k,v in totals.items()},per_family=details,note='Unique per profile per family; profiles may overlap. Only incremental exact door roles counted; labels used solely after geometry profile computation. No threshold search.'))
    print(json.dumps({k:dict(v) for k,v in totals.items()},indent=2))
if __name__=='__main__':run()
