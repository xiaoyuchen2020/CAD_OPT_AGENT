"""Real-DXF holdouts for immutable generated adapters; no model calls or production edits."""
import argparse,copy,hashlib,json,math,shutil,sqlite3,logging
from pathlib import Path
from dataclasses import replace
from collections import defaultdict,Counter
from shapely.geometry import LineString,Polygon,box
from cad_agent.config import ROOT,Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.source import native_source
from cad_agent.extensions.table_recovery.bundle import prepare_region,digest
from cad_agent.extensions.adaptation import service as adaptation
from cad_agent.extensions.adaptation.contracts import Start
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import GeometryMeasure,RoomDiscover

OUT=ROOT/'artifacts/dynamic-nontable-real-20261016';PRIOR=ROOT/'artifacts/dynamic-multimethod-20261016'
METHODS=('text_spans','segment_measurements','segment_distances','closed_contour')
def dump(p,d):p.parent.mkdir(exist_ok=True,parents=True);p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def forbidden(*a,**k):raise AssertionError('MODEL_CALL_FORBIDDEN')
def workspace():
    OUT.mkdir(exist_ok=True);w=Workspace(replace(Settings.load(),data_dir=OUT/'data'))
    for name in ['adaptation-runtime.json','table-recovery-runtime.json']:shutil.copy2(PRIOR/'data'/name,OUT/'data'/name)
    c=sqlite3.connect(f'file:{PRIOR}/data/index.sqlite3?mode=ro',uri=True)
    for k,i,d in c.execute("select kind,id,data from records where kind like 'adaptation_%' or kind='drawing'"):
        v=json.loads(d)
        try:w.store.get(k,i)
        except KeyError:w.store.put(k,i,v)
        if k=='drawing' and not (OUT/'data/drawings'/i).exists():shutil.copytree(PRIOR/'data/drawings'/i,OUT/'data/drawings'/i)
    c.close();adaptation.propose=forbidden;return w

def seed(w,src):
    roots=[ROOT/'artifacts/table-recovery-gap-audit-20261016/search-data',ROOT/'artifacts/dynamic-shadow-near-axis-20261016/data']
    assert sha(Path(src['path']))==src['sha256']
    for root in roots:
        c=sqlite3.connect(f'file:{root}/index.sqlite3?mode=ro',uri=True)
        rows=c.execute("select id,data from records where kind='drawing'").fetchall();c.close()
        for sid,d in rows:
            d=json.loads(d)
            if d['sha256']!=src['sha256']:continue
            if not (OUT/'data/drawings'/sid).exists():shutil.copytree(root/'drawings'/sid,OUT/'data/drawings'/sid)
            w.store.put('drawing',sid,d);return d
    return w.ingest(src['name']+'.dxf',Path(src['path']).read_bytes())

def scan(w,src,visible_only=False):
    d=seed(w,src);idx=DrawingIndex(w,d['id']);native=native_source(w,d['id']);adj=defaultdict(list);edges=[]
    for eid,(e,path,lid,bounds) in native['entities'].items():
        points=None
        if visible_only:
            root=native['doc'].entitydb[path[0]]
            if any(native['doc'].layers.get(v.dxf.layer).is_off() or native['doc'].layers.get(v.dxf.layer).is_frozen() or v.dxf.get('invisible',0) for v in [root,e]):continue
        if e.dxftype()=='LINE':points=[e.dxf.start,e.dxf.end]
        elif e.dxftype()=='LWPOLYLINE' and not e.has_arc:
            points=list(e.vertices_in_wcs())
            if e.closed:points+=points[:1]
        if not points:continue
        for a,b in zip(points,points[1:]):
            if a.z or b.z or a==b:continue
            a=(lid,float(a.x),float(a.y));b=(lid,float(b.x),float(b.y));n=len(edges);edges.append((a,b,eid));adj[a].append(n);adj[b].append(n)
    seen=set();rings=[]
    for i in range(len(edges)):
        if i in seen:continue
        todo=[i];component=[];vertices=set()
        while todo:
            j=todo.pop()
            if j in seen:continue
            seen.add(j);component.append(j)
            for p in edges[j][:2]:vertices.add(p);todo.extend(k for k in adj[p] if k not in seen)
        if not 3<=len(component)<=16 or any(len(adj[p])!=2 for p in vertices):continue
        x=[p[1] for p in vertices];y=[p[2] for p in vertices];bounds=[min(x),min(y),max(x),max(y)]
        if min(bounds[2]-bounds[0],bounds[3]-bounds[1])<20:continue
        entity_ids=sorted({edges[j][2] for j in component});lid=edges[i][0][0]
        if any(t['layout_id']==lid and box(*bounds).intersection(box(*t['bbox'])).area>box(*bounds).area*.2 for t in idx.tables):continue
        rings.append(dict(layout_id=lid,bbox=bounds,entity_ids=entity_ids,segment_count=len(component)))
    rings.sort(key=lambda r:(len(r['entity_ids'])==1,-box(*r['bbox']).area,r['bbox']))
    accepted=[];screened=[];categories=Counter()
    for ring in rings:
        category='multi_entity_ring' if len(ring['entity_ids'])>1 else 'single_entity_control'
        if categories[category]>=2:continue
        a,b,c,e=ring['bbox'];pad=max(.1,min(c-a,e-b)*.001);bounds=[a-pad,b-pad,c+pad,e+pad]
        try:
            bundle=prepare_region(idx,ring['layout_id'],bounds)
            reason='eligible' if not bundle['coverage']['blocking_diagnostics'] and not bundle['excluded_geometry'] and len(bundle['segments'])==ring['segment_count'] else 'input_or_nearby_geometry'
            row=dict(**ring,region_bbox=bounds,classification=category,reason=reason,native_segments=len(bundle['segments']),texts=len(bundle['texts']),excluded=len(bundle['excluded_geometry']),blocking=bundle['coverage']['blocking_diagnostics'])
            screened.append(row)
            if reason!='eligible':continue
            row['case_id']=src['id']+'-region-'+str(len(accepted)+1);row['source']=src;row['snapshot_id']=d['id'];accepted.append(row);categories[category]+=1
        except ValueError as exc:screened.append(dict(**ring,reason=str(exc)))
        if len(screened)>=80:break
    # A real annotation-only local region supplements geometry locations without text.
    for t in idx.scene['texts']:
        if len(t['text'])<8 or not idx.entity_evidence(t['entity_id']):continue
        if any(tb['layout_id']==t['layout_id'] and box(*t['bbox']).intersects(box(*tb['bbox'])) for tb in idx.tables):continue
        a,b,c,e=t['bbox'];pad=max(.1,t['height']*.1);bounds=[a-pad,b-pad,c+pad,e+pad]
        try:bun=prepare_region(idx,t['layout_id'],bounds)
        except ValueError:continue
        if bun['coverage']['blocking_diagnostics'] or not bun['texts'] or len(bun['segments'])>20:continue
        accepted.append(dict(case_id=src['id']+'-text-control',source=src,snapshot_id=d['id'],layout_id=t['layout_id'],bbox=t['bbox'],region_bbox=bounds,classification='text_control',texts=len(bun['texts']),native_segments=len(bun['segments'])));break
    result=dict(source=src,snapshot_id=d['id'],unit=d['unit'],ring_candidates=len(rings),screened=screened,selected=accepted)
    dump(OUT/(src['id']+'-scan.json'),result);print('SCAN',src['id'],len(rings),dict(categories),'selected',len(accepted),flush=True)

def baseline(w,idx,bun,method):
    from cad_agent.analysis.session import source_context
    gs=GeometryService(w,idx.snapshot);ids=sorted({s['entity_id'] for s in bun['segments']})
    if bun['coverage']['blocking_diagnostics']:
        from cad_agent.geometry.contracts import SpatialQuery
        return dict(kind='blocked_input_preflight',blocking=bun['coverage']['blocking_diagnostics'],native_query=gs.query(SpatialQuery(layout_id=bun['layout_id'],bbox=bun['bbox'],limit=12)))
    def call(operation,selected):
        try:return dict(status='returned',result=gs.measure(GeometryMeasure(operation=operation,entity_ids=selected)))
        except ValueError as exc:return dict(status='refused',reason=str(exc).split('\n')[0])
    if method=='text_spans':
        records=[]
        for t in bun['texts']:
            evidence=idx.entity_evidence(t['entity_id']);contexts=[]
            for ev in evidence:
                contexts.append(source_context(w,idx.snapshot,ev['region_id'],neighbors=0,start=ev['text_span'][0],bounded=False))
            records.append(dict(entity_id=t['entity_id'],source_text=t['text'],evidence=evidence,contexts=contexts))
        return dict(kind='source_context',items=records)
    if method=='closed_contour':
        direct=call('area',ids)
        try:discovery=gs.discover(RoomDiscover(layout_id=bun['layout_id'],bbox=bun['bbox'],close_openings=False))
        except ValueError as exc:discovery=dict(status='refused',diagnostics=[str(exc)])
        return dict(kind='area',entity_ids=ids,direct=direct,discovery=discovery)
    if method=='segment_measurements':return dict(kind='length',items=[dict(entity_id=eid,**call('length',[eid])) for eid in ids])
    return dict(kind='distance',items=[dict(entity_ids=[a,b],**call('distance',[a,b])) for i,a in enumerate(ids) for b in ids[i+1:]],same_entity_segment_pairs=sum(1 for i,s in enumerate(bun['segments']) for t in bun['segments'][i+1:] if s['entity_id']==t['entity_id']))

def run(w,source_ids):
    from cad_agent.geometry.source import UNIT_TO_M
    origins={m:json.loads((PRIOR/(m+'-generated.json')).read_text())['run']['run_id'] for m in METHODS}
    results=[]
    for source_id in source_ids:
        scan_data=json.loads((OUT/(source_id+'-scan.json')).read_text());idx=DrawingIndex(w,scan_data['snapshot_id'])
        selected=list(scan_data['selected'])
        if not selected and scan_data['screened']:
            first=scan_data['screened'][0]
            selected=[dict(first,case_id=source_id+'-blocked-control',source=scan_data['source'],snapshot_id=scan_data['snapshot_id'],classification='blocked_input_control')]
        for case in selected:
            bun=prepare_region(idx,case['layout_id'],case['region_bbox']);service=adaptation.AdaptationService(w,idx.snapshot)
            for method in METHODS:
                path=OUT/(case['case_id']+'-'+method+'.json')
                if path.exists():results.append(json.loads(path.read_text()));continue
                if case['classification']=='text_control' and method!='text_spans':continue
                if method=='text_spans' and not bun['texts']:continue
                before=baseline(w,idx,bun,method)
                args=Start(method=method,layout_id=case['layout_id'],bbox=case['region_bbox'],mode='replay',reuse_run_id=origins[method],model_profile='siwc_sol',idempotency_key=case['case_id']+'-'+method+'-sandbox-v2')
                r=service.start(args,background=False);assert r['model_calls']==0
                after=baseline(w,idx,bun,method);assert digest(before)==digest(after)
                row=dict(case=case,method=method,baseline=before,baseline_unchanged=True,baseline_before_sha256=digest(before),baseline_after_sha256=digest(after),source_unchanged=sha(w.store.artifact(idx.snapshot,'source.dxf'))==case['source']['sha256'],run=r,model_calls=0)
                label='dynamic_failed'
                if r.get('accepted_attempt'):
                    row['result']=service.result(r['run_id']);a=r['attempts'][r['accepted_attempt']-1];row['execution']=service.artifact('execution',a['execution_id']);row['code']=service.artifact('code',a['code_artifact_id'])
                    factor=UNIT_TO_M.get(idx.drawing['unit']);label='existing_capability_or_batch_only'
                    if method=='closed_contour':
                        v=row['result']['validation']['items'][0];shape=Polygon(v['vertices']);matches=[]
                        for c in before['discovery'].get('candidates',[]):
                            other=Polygon(c['boundary'])
                            if shape.hausdorff_distance(other)<=max(bun['tolerance']*2,2e-3/(factor or 1)):matches.append(c['candidate_id'])
                        row['matching_existing_enclosures']=matches
                        if before['direct']['status']=='refused' and matches:label='basic_area_entry_gap_existing_discovery_covers'
                        elif before['direct']['status']=='refused' and not matches:label='candidate_new_contour_capability' if factor else 'unknown_units_not_physical_recovery'
                        elif not factor:label='unknown_units_not_physical_recovery'
                    elif not factor and method!='text_spans':label='unknown_units_not_physical_recovery'
                row['assessment']=label;dump(path,row);results.append(row)
                print('RESULT',case['case_id'],method,r['status'],label,flush=True)
        dump(OUT/'run-summary.json',dict(cases=[dict(case=r['case']['case_id'],source=r['case']['source']['name'],method=r['method'],status=r['run']['status'],assessment=r['assessment']) for r in results],model_calls=0))

def main():
    p=argparse.ArgumentParser();p.add_argument('--scan',action='store_true');p.add_argument('--sources',default='11816,11821,11856,sample-60');args=p.parse_args()
    logging.getLogger('ezdxf').setLevel(logging.ERROR);w=workspace()
    try:
        manifest=json.loads((ROOT/'artifacts/table-recovery-gap-audit-20261016/search-manifest.json').read_text())
        if args.scan:
            for sid in args.sources.split(','):scan(w,next(s for s in manifest if s['id']==sid))
        else:run(w,args.sources.split(','))
    finally:w.close()
if __name__=='__main__':main()
