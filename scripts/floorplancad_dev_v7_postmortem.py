"""Analysis-only explanation after freezing. No model or selection changes."""
import json,math
from collections import Counter,defaultdict
from scripts.floorplancad_dev_v7_data import OUT,roster,install_guard,read,save

def prediction(row,name):return json.loads((OUT/'predictions/analysis'/name/(row['sample_id'].replace('/','__')+'.json')).read_text())
def run():
    rows=roster('analysis');install_guard(rows);examples=[];totals=Counter();per_family=[];mixed_groups=[]
    for row in rows:
        ps,t,_,_=read(row);old=prediction(row,'v5_full');local=Counter();byid={p.entity_id:p for p in ps};groups=defaultdict(list)
        for p in ps:
            if p.kind=='line':groups[tuple(sorted(p.points))].append(p.entity_id)
        for kind,variant in [('door','door_unique'),('window','cap_coincident')]:
            new=prediction(row,variant);removed={e for e,v in old['entity_roles'].items() if v==kind and new['entity_roles'].get(e)!=kind}
            for e in removed:local[kind+'_removed_'+t[e]['category']]+=1
            ds=new['door_part_decisions'] if kind=='door' else new['cap_extension_decisions']
            for d in ds:
                ids=set(d['seed_ids'])|set(d['profiles']) if kind=='door' else set(d['observed_frame_entity_ids'])
                loss=ids&removed
                if not loss:continue
                examples.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],kind=kind,before='v5_full',after=variant,ids=sorted(ids),removed_ids=sorted(loss),score_good=sum(t[e]['category']!=kind for e in loss),score_bad=sum(t[e]['category']==kind for e in loss)))
            for points,ids in groups.items():
                if not set(ids)&removed:continue
                cats={t[e]['category'] for e in ids};local[kind+'_removed_in_coincident_groups']+=len(set(ids)&removed)
                if len(cats)>1:
                    local[kind+'_removed_in_mixed_label_groups']+=len(set(ids)&removed)
                    mixed_groups.append(dict(sample_id=row['sample_id'],kind=kind,points=points,sources=[dict(entity_id=e,**t[e]) for e in ids]))
        totals.update(local);per_family.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts=dict(local)))
    chosen=[];used=set()
    for kind in ('door','window'):
        for criterion in ('score_good','score_bad'):
            options=sorted((e for e in examples if e['kind']==kind and e[criterion]>0 and e['group_id'] not in used),key=lambda e:(-e[criterion],e['sample_id'],e['ids']))
            if options:
                e=options[0];chosen.append(e|dict(selection=criterion));used.add(e['group_id'])
    save(OUT/'analysis_postmortem.json',dict(totals=dict(totals),per_family=per_family,examples=chosen,mixed_label_groups=mixed_groups,selection='One best gain and one largest loss example per component, distinct analysis families. Illustrations only; no parameter/selection changes.'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(len(chosen),2,figsize=(13,len(chosen)*3.1),squeeze=False);rowmap={r['sample_id']:r for r in rows}
    for i,ex in enumerate(chosen):
        row=rowmap[ex['sample_id']];ps,t,_,_=read(row);ids=set(ex['ids']);points=[pt for p in ps if p.entity_id in ids for pt in p.points];xs,ys=zip(*points);L=max(max(xs)-min(xs),max(ys)-min(ys));pad=L*.18;swap=max(ys)-min(ys)>max(xs)-min(xs)
        for j,variant in enumerate((ex['before'],ex['after'])):
            pred=prediction(row,variant);roles=pred['entity_roles'];ax=axes[i][j]
            ordered=sorted(ps,key=lambda p:(2 if j and p.entity_id in ex['removed_ids'] else 1 if roles.get(p.entity_id)==ex['kind'] else 0))
            for p in ordered:
                if p.kind not in ('line','arc'):continue
                pts=p.points
                if p.kind=='arc' and p.center and p.radius:
                    a=math.atan2(pts[0][1]-p.center[1],pts[0][0]-p.center[0]);d=math.radians(p.sweep_degrees)
                    pts=[(p.center[0]+p.radius*math.cos(a+d*k/40),p.center[1]+p.radius*math.sin(a+d*k/40)) for k in range(41)]
                if max(x for x,y in pts)<min(xs)-pad or min(x for x,y in pts)>max(xs)+pad or max(y for x,y in pts)<min(ys)-pad or min(y for x,y in pts)>max(ys)+pad:continue
                e=p.entity_id;color='#c6cdd2';width=.8;style='-'
                if t[e]['category']==ex['kind']:color='#ac79b6';width=1.1;style='--'
                if roles.get(e)==ex['kind']:color='#12856b' if t[e]['category']==ex['kind'] else '#da5b2c';width=2;style='-'
                if j and e in ex['removed_ids']:color='#2b83be';width=2.5;style='-'
                ax.plot(*zip(*[(y,x) if swap else (x,y) for x,y in pts]),color=color,linewidth=width,linestyle=style)
            xx,yy=(ys,xs) if swap else (xs,ys);ax.set_xlim(min(xx)-pad,max(xx)+pad);ax.set_ylim(max(yy)+pad,min(yy)-pad);ax.set_aspect('equal');ax.set_xticks([]);ax.set_yticks([])
            tp=sum(roles.get(e)==ex['kind'] and t[e]['category']==ex['kind'] for e in ids);fp=sum(roles.get(e)==ex['kind'] and t[e]['category']!=ex['kind'] for e in ids)
            ax.set_title(f'{ex["sample_id"]} | {variant}\nselected {ex["kind"]} parts TP={tp}, FP={fp}',fontsize=10)
    fig.suptitle('Analysis examples: green = correct, orange = false, blue = abstained part\nPurple dashed = target label; exact coincident sources may visually overlap',fontsize=11)
    fig.tight_layout(rect=(0,0,1,.95));fig.savefig(OUT/'analysis_examples.png',dpi=145);plt.close(fig)
    print(json.dumps(dict(totals),indent=2))
if __name__=='__main__':run()
