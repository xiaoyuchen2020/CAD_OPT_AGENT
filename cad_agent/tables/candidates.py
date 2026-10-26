"""Bounded, conservative borderless proposals from aligned native text rows."""
from collections import defaultdict
from statistics import median
from cad_agent.contracts import stable_id
from .topology import box


def borderless(index, documents):
    covered={eid for d in documents for eid in d['source_inventory']}
    groups=defaultdict(list)
    for t in index.scene['texts']:
        if t['entity_id'] not in covered:
            groups[t['layout_id'],tuple(t.get('entity_path',[])[:-1])].append(t)
    candidates=[]
    for (layout,path),texts in groups.items():
        h=median(t.get('height',1) for t in texts)
        rows=[]
        for t in sorted(texts,key=lambda t:-t['origin'][1]):
            if abs(t.get('rotation',0))>.1: continue
            if rows and abs(rows[-1][0]['origin'][1]-t['origin'][1])<h*.4: rows[-1].append(t)
            else: rows.append([t])
        runs=[];run=[]
        for row in rows:
            row.sort(key=lambda t:t['origin'][0])
            aligned=(run and len(row)==len(run[-1]) and all(abs(a['origin'][0]-b['origin'][0])<h for a,b in zip(row,run[-1]))
                     and abs(row[0]['origin'][1]-run[-1][0]['origin'][1])<h*5)
            if len(row)>=2 and (not run or aligned):run.append(row)
            else:
                if len(run)>=3:runs.append(run)
                run=[row] if len(row)>=2 else []
        if len(run)>=3:runs.append(run)
        for run in runs:
            ts=[t for row in run for t in row]
            if len(ts)>1000:continue
            candidates.append(dict(candidate_id=stable_id('tcandidate',index.snapshot,*(t['entity_id'] for t in ts)),
                layout_id=layout,instance_path=list(path),bbox=box([p for t in ts for p in (t['bbox'][:2],t['bbox'][2:])]),
                source_kind='borderless',structure_state='candidate',binding_state='unresolved',
                preview=' | '.join(t['text'] for t in ts)[:1500],text_entity_ids=[t['entity_id'] for t in ts],
                rows=[[t['entity_id'] for t in row] for row in run],diagnostics=['NO_SOURCE_CELL_BOUNDARIES']))
            if len(candidates)>=256:return candidates
    return candidates
