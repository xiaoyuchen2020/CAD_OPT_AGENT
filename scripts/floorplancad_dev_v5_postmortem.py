"""Post-freeze illustrations restricted to the fixed analysis roster."""
import json,math
from collections import Counter
from scripts.floorplancad_dev_v5_data import OUT,roster,install_guard,read,save

def run():
    rows=roster('analysis');install_guard(rows);examples=[];totals=Counter();details=[]
    def prediction(row,name):return json.loads((OUT/'predictions/analysis'/name/(row['sample_id'].replace('/','__')+'.json')).read_text())
    for row in rows:
        ps,truth,_,_=read(row);base=prediction(row,'baseline');old=prediction(row,'v4_combined');new=prediction(row,'full');door=prediction(row,'door_parts_only');local=Counter()
        for e,t in truth.items():
            if old['entity_roles'].get(e)=='window' and new['entity_roles'].get(e)!='window':local['removed_window_'+t['category']]+=1
            if door['entity_roles'].get(e)=='door' and base['entity_roles'].get(e)!='door':local['added_door_'+t['category']]+=1
            if t['category']=='wall' and base['entity_roles'].get(e)=='wall' and new['entity_roles'].get(e)!='wall':local['lost_baseline_wall_tp']+=1
        for c in new['window_rejections']:
            ids=[e for e in c['entity_ids'] if old['entity_roles'].get(e)=='window' and new['entity_roles'].get(e)!='window']
            if not ids:continue
            wall=sum(truth[e]['category']=='wall' for e in ids);true=sum(truth[e]['category']=='window' for e in ids)
            examples.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],kind='window',before='v4_combined',after='full',ids=c['entity_ids'],score_good=wall,score_bad=true,reasons=c['reasons']))
        for c in door['candidates']:
            if c['kind']!='door' or 'added_part_ids' not in c['evidence']:continue
            ids=[e for e in c['evidence']['added_part_ids'] if base['entity_roles'].get(e)!='door' and door['entity_roles'].get(e)=='door']
            if not ids:continue
            true=sum(truth[e]['category']=='door' for e in ids);false=len(ids)-true
            examples.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],kind='door',before='baseline',after='door_parts_only',ids=c['entity_ids'],score_good=true,score_bad=false,reasons=[c['reason']]))
        totals.update(local);details.append(dict(sample_id=row['sample_id'],group_id=row['group_id'],counts=dict(local)))
    chosen=[];used=set()
    for kind,score in [('window','score_good'),('window','score_bad'),('door','score_good'),('door','score_bad')]:
        choices=sorted((e for e in examples if e['kind']==kind and e[score]>0),key=lambda e:(-e[score],e['sample_id']))
        for e in choices:
            if e['group_id'] not in used:chosen.append(e|{'selection':score});used.add(e['group_id']);break
    save(OUT/'analysis_postmortem.json',dict(totals=dict(totals),per_family=details,examples=chosen,phase='analysis_only_after_freeze',note='Descriptions and illustrations only. No algorithm or selection change.'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(len(chosen),2,figsize=(13,3*len(chosen)),squeeze=False);rowmap={r['sample_id']:r for r in rows}
    for rr,ex in enumerate(chosen):
        row=rowmap[ex['sample_id']];ps,truth,_,_=read(row);byid={p.entity_id:p for p in ps};ids=set(ex['ids']);points=[pt for e in ids for pt in byid[e].points];xs,ys=zip(*points);swap=max(ys)-min(ys)>max(xs)-min(xs);L=max(max(xs)-min(xs),max(ys)-min(ys));pad=L*.18
        for cc,variant in enumerate([ex['before'],ex['after']]):
            roles=prediction(row,variant)['entity_roles'];ax=axes[rr][cc]
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
                ax.plot(*zip(*[(y,x) if swap else (x,y) for x,y in pts]),color=color,linewidth=width,linestyle=style)
            xx,yy=(ys,xs) if swap else (xs,ys);ax.set_xlim(min(xx)-pad,max(xx)+pad);ax.set_ylim(max(yy)+pad,min(yy)-pad);ax.set_aspect('equal');ax.set_xticks([]);ax.set_yticks([])
            tp=sum(roles.get(e)==ex['kind'] and truth[e]['category']==ex['kind'] for e in ids);fp=sum(roles.get(e)==ex['kind'] and truth[e]['category']!=ex['kind'] for e in ids)
            ax.set_title(f'{ex["sample_id"]} | {variant}\nselected {ex["kind"]} parts TP={tp}, FP={fp}',fontsize=10)
    fig.suptitle('Analysis only: green = correct class, orange = false class\npurple dashed = target truth, gray = context; crops rotated for display',fontsize=12);fig.tight_layout(rect=(0,0,1,.95));fig.savefig(OUT/'analysis_examples.png',dpi=140);plt.close(fig)
    print(json.dumps(dict(totals),indent=2))
if __name__=='__main__':run()
