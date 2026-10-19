"""Descriptive analysis of frozen results on allowed analysis samples only."""
import json,math
from collections import Counter
from scripts.floorplancad_dev_v4_data import OUT,roster,read,install_guard,save

def run():
    rows=roster('analysis');install_guard(rows);totals=Counter();fp=Counter();host_categories=Counter();per_family=[];examples=[]
    for row in rows:
        ps,truth,_,_=read(row);name=row['sample_id'].replace('/','__')+'.json'
        base=json.loads((OUT/'predictions/analysis/baseline'/name).read_text());new=json.loads((OUT/'predictions/analysis/combined'/name).read_text());a=base['entity_roles'];b=new['entity_roles'];local=Counter()
        for eid,t in truth.items():
            if b.get(eid)=='window' and a.get(eid)!='window':
                local['new_window_tp' if t['category']=='window' else 'new_window_fp']+=1
                if t['category']!='window':fp[str(t['raw_semantic_id'])]+=1
            if t['category']=='wall' and a.get(eid)=='wall' and b.get(eid)!='wall':
                local['lost_wall_tp']+=1;local['lost_wall_now_'+b.get(eid,'unassigned')]+=1
            if t['category']=='door' and b.get(eid)=='door' and a.get(eid)!='door':local['new_door_tp']+=1
        supports=set()
        for c in new['candidates']:
            if c['kind']!='window' or not c['evidence'].get('host_supported'):continue
            ids=c['entity_ids'];support=c['evidence']['host_support_ids'];supports.update(support)
            hit=sum(truth[e]['category']=='window' for e in ids);wrong=len(ids)-hit
            if hit or wrong:examples.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],tp=hit,fp=wrong,entity_ids=ids,host_support_ids=support))
            local['hosted_window_candidates']+=1
            local['hosted_window_candidates_no_true_window_parts']+=hit==0
            local['hosted_window_candidates_all_supports_nonwall']+=all(truth[e]['category']!='wall' for e in support)
        host_categories.update(str(truth[e]['raw_semantic_id']) for e in supports)
        totals.update(local);per_family.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts=dict(local)))
    selected=[];used=set()
    for kind in ['true','false']:
        ordered=sorted(examples,key=lambda c:(-(c['tp'] if kind=='true' else c['fp']),c['sample_id']))
        n=0
        for ex in ordered:
            if ex['group_id'] in used or (kind=='true' and ex['fp']>0) or (kind=='false' and ex['tp']>0):continue
            selected.append(ex|{'type':kind});used.add(ex['group_id']);n+=1
            if n==2:break
    result=dict(phase='analysis_only_post_freeze',totals=dict(totals),new_window_fp_raw_classes=dict(fp),host_support_raw_classes=dict(host_categories),per_family=per_family,illustrative_candidates=selected,
        note='Host means geometric witness, not verified semantic wall; candidate counts may overlap. Descriptive diagnosis only, frozen algorithm unchanged.')
    save(OUT/'analysis_postmortem.json',result);print(json.dumps({k:result[k] for k in ['totals','new_window_fp_raw_classes','host_support_raw_classes']},indent=2))
    # Post-freeze analysis illustrations, never used for rule additions.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(len(selected),2,figsize=(13,2.3*len(selected)),squeeze=False)
    rowmap={r['sample_id']:r for r in rows}
    for rr,ex in enumerate(selected):
        ps,t,_,_=read(rowmap[ex['sample_id']]);byid={p.entity_id:p for p in ps};ids=set(ex['entity_ids']);hosts=set(ex['host_support_ids']);points=[pt for e in ids|hosts for pt in byid[e].points]
        xs,ys=zip(*points);swap=max(ys)-min(ys)>max(xs)-min(xs);span=max(max(xs)-min(xs),max(ys)-min(ys));pad=span*.08
        for cc,variant in enumerate(['baseline','combined']):
            name=ex['sample_id'].replace('/','__')+'.json';roles=json.loads((OUT/'predictions/analysis'/variant/name).read_text())['entity_roles'];ax=axes[rr][cc]
            for p in ps:
                if p.kind not in ('line','arc'):continue
                pts=p.points
                if p.kind=='arc' and p.center and p.radius:
                    start=math.atan2(p.points[0][1]-p.center[1],p.points[0][0]-p.center[0]);delta=math.radians(p.sweep_degrees)
                    pts=[(p.center[0]+p.radius*math.cos(start+delta*i/30),p.center[1]+p.radius*math.sin(start+delta*i/30)) for i in range(31)]
                if max(pt[0] for pt in pts)<min(xs)-pad or min(pt[0] for pt in pts)>max(xs)+pad or max(pt[1] for pt in pts)<min(ys)-pad or min(pt[1] for pt in pts)>max(ys)+pad:continue
                eid=p.entity_id;color='#c7cdd1';width=.7;style='-'
                if eid in hosts:color='#3988ba';width=1.3
                if t[eid]['category']=='window':color='#a17ab6';width=1.2;style='--'
                if roles.get(eid)=='window':color='#14966f' if t[eid]['category']=='window' else '#db592c';width=2;style='-'
                ax.plot(*zip(*[(y,x) if swap else (x,y) for x,y in pts]),color=color,linewidth=width,linestyle=style)
            xx,yy=(ys,xs) if swap else (xs,ys);ax.set_xlim(min(xx)-pad,max(xx)+pad);ax.set_ylim(max(yy)+pad,min(yy)-pad);ax.set_aspect('equal');hits=sum(roles.get(e)=='window' and t[e]['category']=='window' for e in ids);false=sum(roles.get(e)=='window' and t[e]['category']!='window' for e in ids);ax.set_title(f'{ex["sample_id"]} | {variant} | selected parts TP={hits}, FP={false}',fontsize=10);ax.set_xticks([]);ax.set_yticks([])
    fig.suptitle('Analysis only: green = correct window, orange = false window\npurple dashed = window truth, blue = geometric host support',fontsize=13)
    fig.tight_layout(rect=(0,0,1,.96));fig.savefig(OUT/'analysis_examples.png',dpi=145);plt.close(fig)
if __name__=='__main__':run()
