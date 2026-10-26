"""Source-instance-aware ruled grids, independent of business vocabulary."""
from collections import defaultdict
from statistics import median
from cad_agent.contracts import stable_id

VERSION = 'ruled-source-v2'
TOL = .11


def joined(edges):
    groups = defaultdict(list)
    for at, lo, hi in edges:
        groups[round(at, 2)].append((lo, hi))
    result = []
    for at, spans in groups.items():
        merged = []
        for lo, hi in sorted(spans):
            if merged and lo <= merged[-1][1] + TOL:
                merged[-1][1] = max(hi, merged[-1][1])
            else:
                merged.append([lo, hi])
        result.extend((at, lo, hi) for lo, hi in merged)
    return result


def recover(index, legacy):
    """Recover complete envelopes without joining disconnected tables.

    Join collinear overlapping/touching segments. A dangling column endpoint
    may meet a repeated row rail within 0.2 text heights; record the adjustment.
    Text selection uses source instance ancestry, never business values.
    """
    from cad_agent.semantics.tables import grid
    groups = defaultdict(list)
    text_groups = defaultdict(list)
    for line in index.scene['lines']:
        groups[(line['layout_id'], tuple(line.get('entity_path', [])[:-1]))].append(line)
    for t in index.scene['texts']:
        text_groups[(t['layout_id'], tuple(t.get('entity_path', [])[:-1]))].append(t)
    recovered = []
    for (lid, path), lines in groups.items():
        texts = [t for (text_lid,text_path),items in text_groups.items()
                 if text_lid==lid and text_path[:len(path)]==path for t in items]
        if len(texts) < 4:
            continue
        height = median(t['height'] for t in texts)
        if height <= 0:
            continue
        hs, vs = [], []
        for line in lines:
            for p, q in zip(line['points'], line['points'][1:]):
                if abs(p[1]-q[1]) < .01 and abs(p[0]-q[0]) > height:
                    hs.append((round(p[1],2), round(min(p[0],q[0]),2), round(max(p[0],q[0]),2)))
                elif abs(p[0]-q[0]) < .01 and abs(p[1]-q[1]) > height:
                    vs.append((round(p[0],2), round(min(p[1],q[1]),2), round(max(p[1],q[1]),2)))
        hs, vs = joined(hs), joined(vs)
        if len(hs) < 3 or len(vs) < 3:
            continue
        adjustments, snapped = [], []
        for x, lo, hi in vs:
            endpoints = []
            for end, direction in ((lo,-1),(hi,1)):
                matches = [y for y,a,b in hs if a-TOL <= x <= b+TOL and
                           TOL < (y-end)*direction <= height*.2 and
                           sum(abs(y-vend)<TOL for vx,vlo,vhi in vs for vend in (vlo,vhi)) >= 2]
                target = min(matches,key=lambda y:abs(y-end)) if matches else end
                endpoints.append(target)
                if target != end:
                    adjustments.append(dict(axis='vertical',coordinate=x,original_endpoint=end,
                                            endpoint=target,tolerance=height*.2))
            snapped.append((x,*endpoints))
        vs = joined(snapped)
        spans = defaultdict(list)
        for y,a,b in hs:
            spans[(a,b)].append(y)
        for (a,c), levels in spans.items():
            if c-a < height*8 or len(levels) < 3:
                continue
            bands = []
            for b,d in zip(sorted(levels),sorted(levels)[1:]):
                if all(any(abs(x-side)<=TOL and lo<=b+TOL and hi>=d-TOL for x,lo,hi in vs) for side in (a,c)):
                    if bands and abs(bands[-1][1]-b)<=TOL:
                        bands[-1][1]=d
                    else:
                        bands.append([b,d])
            for b,d in bands:
                box=[a,b,c,d]
                # Existing ruled-table candidates guard against arbitrary plan
                # line networks; this pass repairs their source envelopes.
                if not any(t['layout_id']==lid and a-TOL<=t['bbox'][0] and b-TOL<=t['bbox'][1]
                           and t['bbox'][2]<=c+TOL and t['bbox'][3]<=d+TOL for t in legacy):
                    continue
                # A through-line with neither endpoint on this table is not a
                # column separator (e.g. an axis crossing the complete grid).
                # Perimeter rails and columns continuing to a footer still work.
                crossing=[(x,lo,hi) for x,lo,hi in vs
                          if a+TOL<x<c-TOL and lo<b-TOL and hi>d+TOL]
                vv=[(x,max(b,lo),min(d,hi)) for x,lo,hi in vs
                    if a-TOL<=x<=c+TOL and hi>b and lo<d and (x,lo,hi) not in crossing]
                xs=sorted({a,c}|{x for x,lo,hi in vv})
                hh=[v for v in hs if b-TOL<=v[0]<=d+TOL and a-TOL<=v[1] and v[2]<=c+TOL and
                    all(any(abs(x-end)<=TOL for x in xs) for end in v[1:])]
                ys=sorted({b,d}|{y for y,lo,hi in hh},reverse=True)
                if len(xs)<3 or len(ys)<3 or (len(xs)-1)*(len(ys)-1)>4000:
                    continue
                tid=stable_id('table',index.snapshot,lid,VERSION,str(path),str(box))
                seed=dict(table_id=tid,layout_id=lid,bbox=box,column_count=len(xs)-1,
                          row_count=len(ys)-1,rows=[dict(bbox=[a,lo,c,hi],cells=[dict(bbox=[x,lo,z,hi])
                              for x,z in zip(xs,xs[1:])]) for hi,lo in zip(ys,ys[1:])],
                          ruled_source=dict(version=VERSION,instance_path=list(path),horizontal=hh,vertical=vv,
                                            excluded_through_lines=crossing,
                                            adjustments=[v for v in adjustments if a<=v['coordinate']<=c and b<=v['endpoint']<=d]))
                try:
                    cells,n,m,diagnostics=grid(index,seed)
                except ValueError:
                    continue
                if diagnostics or sum(bool(cell['text']) for cell in cells)<4:
                    continue
                seed.update(rows=[dict(row_index=r,bbox=[a,ys[r+1],c,ys[r]],cells=[cell for cell in cells
                    if cell['row_index']==r]) for r in range(n)],row_count=n,column_count=m,state='candidate')
                recovered.append(seed)
    recovered.sort(key=lambda t:-(t['bbox'][2]-t['bbox'][0])*(t['bbox'][3]-t['bbox'][1]))
    kept=[]
    def contains(t,u):
        a,b,c,d=t['bbox'];x,y,z,w=u['bbox']
        return t['layout_id']==u['layout_id'] and a-TOL<=x and b-TOL<=y and z<=c+TOL and w<=d+TOL
    for t in recovered:
        if not any(k['ruled_source']['instance_path']==t['ruled_source']['instance_path'] and contains(k,t) for k in kept):
            kept.append(t)
    return [t for t in legacy if not any(contains(k,t) for k in kept)] + kept
