"""Analysis-only annotation diagnostics; never imported by the recognizer."""
from collections import Counter,defaultdict
import json,math
from shapely.geometry import LineString,Point
from shapely.strtree import STRtree
from scripts.floorplancad_dev_v4_data import OUT,OLD,roster,install_guard,read,save

def run():
    rows=roster('analysis');paths=[OLD/'refined/predictions/no_caps'/(r['sample_id'].replace('/','__')+'.json') for r in rows]
    install_guard(rows,paths);totals=defaultdict(Counter);family_rows=[]
    for row,path in zip(rows,paths):
        ps,truth,diag,gh=read(row);byid={p.entity_id:p for p in ps};pred=json.loads(path.read_text());roles=pred['entity_roles']
        wall=[LineString(p.points) for p in ps if p.kind=='line' and truth[p.entity_id]['category']=='wall'];wt=STRtree(wall)
        inst=defaultdict(list)
        for eid,t in truth.items():
            if t['category'] in ('door','window') and t['instance_id']!='-1':inst[t['category'],t['raw_semantic_id'],t['instance_id']].append(eid)
        local=defaultdict(Counter)
        for (cls,_,_),ids in inst.items():
            pieces=[byid[e] for e in ids if e in byid];hit=sum(roles.get(e)==cls for e in ids)
            status='no_hit' if not hit else 'partial' if hit<len(ids) else 'full';c=local[cls];c['instances']+=1;c[status]+=1
            if status=='full':continue
            lines=[p for p in pieces if p.kind=='line' and math.dist(*p.points)>0];arcs=[p for p in pieces if p.kind=='arc']
            c['incomplete_with_arc']+=bool(arcs);c['incomplete_without_arc']+=not arcs
            if not lines:continue
            main=max(lines,key=lambda p:math.dist(*p.points));a,b=main.points;L=math.dist(a,b);u=((b[0]-a[0])/L,(b[1]-a[1])/L)
            parallel=[];perp=[]
            for p in lines:
                x,y=p.points;l=math.dist(x,y);v=((y[0]-x[0])/l,(y[1]-x[1])/l)
                if abs(u[0]*v[1]-u[1]*v[0])<math.sin(math.radians(1)):parallel.append(p)
                if abs(u[0]*v[0]+u[1]*v[1])<math.sin(math.radians(1)):perp.append(p)
            c['incomplete_with_3_parallel_lines']+=len(parallel)>=3
            c['incomplete_with_perpendicular_lines']+=bool(perp)
            attached=[]
            for p in lines:
                for pt in p.points:
                    if len(wt.query(Point(pt).buffer(L*.02),predicate='intersects')):attached.append(pt)
            c['incomplete_wall_endpoint_contact']+=bool(attached)
            if parallel:
                spans=[((pt[0]-a[0])*u[0]+(pt[1]-a[1])*u[1]) for p in parallel for pt in p.points];lo,hi=min(spans),max(spans)
                at=[((pt[0]-a[0])*u[0]+(pt[1]-a[1])*u[1]) for pt in attached]
                c['incomplete_wall_contact_both_ends']+=bool(at) and min(at)<=lo+L*.04 and max(at)>=hi-L*.04
        for cls,c in local.items():totals[cls].update(c)
        fp=Counter(str(truth[e]['raw_semantic_id']) for e,role in roles.items() if role=='wall' and truth[e]['category']!='wall')
        totals['wall_fp_raw_semantic'].update(fp)
        family_rows.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts={k:dict(v) for k,v in local.items()}))
    result=dict(samples=len(rows),families=len(rows),totals={k:dict(v) for k,v in totals.items()},per_family=family_rows,
        note='Ground truth wall contacts are diagnosis only, not prediction inputs; contact radius is 2% of longest instance line. Counts are overlapping structural properties, not attribution of causality.')
    save(OUT/'analysis_diagnosis.json',result);print(json.dumps(result['totals'],indent=2))
if __name__=='__main__':run()
