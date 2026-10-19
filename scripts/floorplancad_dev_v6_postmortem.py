"""Post-freeze illustrations restricted to the fixed analysis roster."""
import json,math
from collections import Counter
from scripts.floorplancad_dev_v6_data import OUT,roster,install_guard,read,save

def run():
    rows=roster('analysis');install_guard(rows);examples=[];totals=Counter();details=[]
    def prediction(row,name):return json.loads((OUT/'predictions/analysis'/name/(row['sample_id'].replace('/','__')+'.json')).read_text())
    for row in rows:
        ps,truth,_,_=read(row);old=prediction(row,'v5_full');new=prediction(row,'full');local=Counter()
        for e,t in truth.items():
            if old['entity_roles'].get(e)=='window' and new['entity_roles'].get(e)!='window':local['removed_window_'+t['category']]+=1
        for c in new['candidates']:
            if c['kind']!='window':continue
            ev=c['evidence'];ids=ev.get('shared_boundary_support_ids',[])
            removed=[e for e in ids if old['entity_roles'].get(e)=='window' and new['entity_roles'].get(e)!='window']
            if not removed:continue
            examples.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],kind='window',before='v5_full',after='full',ids=ev['observed_frame_entity_ids'],support_only_ids=ids,
                score_good=sum(truth[e]['category']=='wall' for e in removed),score_bad=sum(truth[e]['category']=='window' for e in removed)))
        local['restored_wall_pairs']=len(new['restored_wall_pairs'])
        totals.update(local);details.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts=dict(local)))
    chosen=[];used=set()
    for score in ['score_good','score_bad']:
        choices=sorted((e for e in examples if e[score]>0),key=lambda e:(-e[score],e['sample_id']))
        n=0
        for e in choices:
            if e['group_id'] not in used:chosen.append(e|{'selection':score});used.add(e['group_id']);n+=1
            if n==2:break
    save(OUT/'analysis_postmortem.json',dict(totals=dict(totals),per_family=details,examples=chosen,phase='analysis_only_after_freeze',note='Descriptions and illustrations only. No algorithm or selection change.'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(len(chosen),2,figsize=(13,3*len(chosen)),squeeze=False);rowmap={r['sample_id']:r for r in rows}
    for rr,ex in enumerate(chosen):
        row=rowmap[ex['sample_id']];ps,truth,_,_=read(row);byid={p.entity_id:p for p in ps};ids=set(ex['ids']);points=[pt for e in ids for pt in byid[e].points];xs,ys=zip(*points);swap=max(ys)-min(ys)>max(xs)-min(xs);L=max(max(xs)-min(xs),max(ys)-min(ys));pad=L*.18
        for cc,variant in enumerate([ex['before'],ex['after']]):
            pred=prediction(row,variant);roles=pred['entity_roles'];supports=set(pred.get('support_only_ids',[]));ax=axes[rr][cc]
            for p in ps:
                if p.kind not in ('line','arc'):continue
                pts=p.points
                if p.kind=='arc' and p.center and p.radius:
                    a=math.atan2(pts[0][1]-p.center[1],pts[0][0]-p.center[0]);delta=math.radians(p.sweep_degrees)
                    pts=[(p.center[0]+p.radius*math.cos(a+delta*i/30),p.center[1]+p.radius*math.sin(a+delta*i/30)) for i in range(31)]
                if max(pt[0] for pt in pts)<min(xs)-pad or min(pt[0] for pt in pts)>max(xs)+pad or max(pt[1] for pt in pts)<min(ys)-pad or min(pt[1] for pt in pts)>max(ys)+pad:continue
                color='#c6cdd2';width=.8;style='-';eid=p.entity_id
                if truth[eid]['category']==ex['kind']:color='#aa7db7';width=1;style='--'
                if roles.get(eid)==ex['kind']:color='#12856b' if truth[eid]['category']==ex['kind'] else '#da5b2c';width=1.8;style='-'
                if eid in supports and roles.get(eid)!='window':color='#2b83be';width=2;style='-'
                ax.plot(*zip(*[(y,x) if swap else (x,y) for x,y in pts]),color=color,linewidth=width,linestyle=style)
            xx,yy=(ys,xs) if swap else (xs,ys);ax.set_xlim(min(xx)-pad,max(xx)+pad);ax.set_ylim(max(yy)+pad,min(yy)-pad);ax.set_aspect('equal');ax.set_xticks([]);ax.set_yticks([])
            tp=sum(roles.get(e)==ex['kind'] and truth[e]['category']==ex['kind'] for e in ids);fp=sum(roles.get(e)==ex['kind'] and truth[e]['category']!=ex['kind'] for e in ids)
            ax.set_title(f'{ex["sample_id"]} | {variant}\nselected {ex["kind"]} parts TP={tp}, FP={fp}',fontsize=10)
    fig.suptitle('Analysis only: green = correct window, orange = false window\nblue = support-only source, purple dashed = window truth; geometry preserved',fontsize=12);fig.tight_layout(rect=(0,0,1,.95));fig.savefig(OUT/'analysis_examples.png',dpi=140);plt.close(fig)
    print(json.dumps(dict(totals),indent=2))
if __name__=='__main__':run()
