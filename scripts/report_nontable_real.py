"""Independent comparisons and source-context review for real non-table adapters."""
import json,math,copy,html,hashlib,sqlite3,tempfile,logging
from collections import defaultdict,Counter
from pathlib import Path
from dataclasses import replace
import ezdxf
from ezdxf.addons import Importer
from ezdxf import transform
from ezdxf.math import Matrix44
from shapely.geometry import box
from cad_agent.config import ROOT,Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.source import native_source,UNIT_TO_M
from cad_agent.contracts import stable_id
from cad_agent.cad.reader import entity_batches,decode_text
from cad_agent.cad.render import render_drawing,render_svg
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.review import source_svg
from cad_agent.extensions.adaptation.validation import validate
from cad_agent.extensions.table_recovery.bundle import verify_bundle,digest
from scripts.report_cross_adaptation import raster
from scripts.validate_nontable_real import OUT,PRIOR,dump,sha

def records():
    rows=[]
    for f in sorted(OUT.glob('*.json')):
        d=json.loads(f.read_text())
        if isinstance(d,dict) and 'case' in d and 'run' in d:rows.append((f,d))
    return rows

def context(w,case,run):
    idx=DrawingIndex(w,case['snapshot_id']);native=native_source(w,idx.snapshot);a,b,c,d=case['region_bbox'];pad=max(c-a,d-b)*.45
    bounds=[a-pad,b-pad,c+pad,d+pad];lid=case['layout_id'];ids=[native['ids'][int(i)] for i in native['tree'].query(box(*bounds),predicate='intersects')];handles=set()
    for eid in ids:
        e,path,layout,_=native['entities'][eid]
        if layout==lid:handles.add(path[0])
    for t in idx.scene['texts']:
        if t['layout_id']==lid and bounds[0]<=t['origin'][0]<=bounds[2] and bounds[1]<=t['origin'][1]<=bounds[3]:handles.add(t['entity_path'][0])
    limit=6000;truncated=len(handles)>limit;handles=sorted(handles)[:limit]
    doc=ezdxf.new(native['doc'].dxfversion);im=Importer(native['doc'],doc);im.import_entities([native['doc'].entitydb[h] for h in handles]);im.finalize()
    ox,oy=(a+c)/2,(b+d)/2;errors=transform.inplace(doc.modelspace(),Matrix44.translate(-ox,-oy,0));assert not list(errors)
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'context.dxf';doc.saveas(path);render=render_drawing(path,'real-review-context')
    svg=render_svg(render,stable_id('layout','real-review-context','Model'),[bounds[0]-ox,bounds[1]-oy,bounds[2]-ox,bounds[3]-oy])
    svg=svg.replace('</svg>',f'<rect x="{a-ox}" y="{oy-d}" width="{c-a}" height="{d-b}" fill="none" stroke="#e73b91" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>')
    target=OUT/(case['case_id']+'-context.svg');target.write_text(svg);raster(svg,target.with_suffix('.png'))
    return dict(source_sha256=case['source']['sha256'],layout_id=lid,scope_bbox=case['region_bbox'],context_bbox=bounds,root_handles=handles,truncated=truncated,render_diagnostics=render['diagnostics'],display_origin=[ox,oy],rendered_items=len(render['items']),scope='context_only_not_algorithm_input')

def audit(w,rows):
    groups=defaultdict(dict);neg=[];comparisons=[]
    for f,d in rows:
        service=AdaptationService(w,d['case']['snapshot_id']);r=d['run'];b=service.artifact('input',r['input_artifact_id']);verify_bundle(b);groups[b['snapshot_id']][b['input_id']]=b
        if not d.get('result'):continue
        method=d['method'];v=d['result']['validation'];factor=UNIT_TO_M.get(b['unit']);before=d['baseline'];checks=[]
        if method=='text_spans':
            texts={t['entity_id']:t for t in b['texts']};old={i['entity_id']:i for i in before['items']}
            for i in v['items']:
                t=texts[i['entity_id']];assert i['quote']==t['text'];assert any(t['text'] in e['quote'] for e in old[i['entity_id']]['evidence']);checks.append('same_full_original_text')
        elif method=='segment_measurements' and factor:
            sums=defaultdict(float);segs={s['segment_id']:s for s in b['segments']}
            for i in v['items']:sums[segs[i['segment_id']]['entity_id']]+=i['length']*factor
            for i in before['items']:
                if i['status']=='returned' and i['result']['status']=='measured':assert abs(sums[i['entity_id']]-i['result']['value'])<=1e-6;checks.append('same_entity_total_length')
        elif method=='segment_distances' and factor:
            segs={s['segment_id']:s for s in b['segments']};mins={}
            for i in v['items']:
                key=tuple(sorted(segs[s]['entity_id'] for s in i['segment_ids']))
                mins[key]=min(mins.get(key,math.inf),i['distance']*factor)
            for i in before['items']:
                if i['status']=='returned' and i['result']['status']=='measured':assert abs(mins[tuple(i['entity_ids'])]-i['result']['value'])<=1e-6;checks.append('same_entity_pair_minimum')
        comparisons.append(dict(case=d['case']['case_id'],method=method,checks=checks,unit=b['unit'],baseline_unchanged=d['baseline_unchanged']))
        raw=d['execution']['output'];bad=copy.deepcopy(raw);bad['input_id']='foreign-input';vbad=validate(method,b,bad);assert vbad['validation_status']=='rejected';neg.append(dict(case=d['case']['case_id'],method=method,mutation='foreign_input',errors=vbad['errors']))
        bad=copy.deepcopy(raw)
        if method=='closed_contour':bad['area']+=10
        elif method=='text_spans':bad['items'][0]['end']=999999
        else:bad['items'][0]['length' if method=='segment_measurements' else 'distance']+=10
        vbad=validate(method,b,bad);assert vbad['validation_status']=='rejected';neg.append(dict(case=d['case']['case_id'],method=method,mutation='invalid_value',errors=vbad['errors']))
    reports=[]
    for sid,bundles in groups.items():
        path=w.store.artifact(sid,'source.dxf');h=sha(path);assert all(b['source_sha256']==h for b in bundles.values());segments={s['segment_id']:s for b in bundles.values() for s in b['segments']};texts={t['entity_id']:t for b in bundles.values() for t in b['texts']};needed={s['entity_id'] for s in segments.values()}|texts.keys();found={};doc=ezdxf.readfile(path)
        for layout in doc.layouts:
            lid=stable_id('layout',sid,layout.name)
            for batch in entity_batches(layout,[],layout.name):
                for e,p in batch:
                    eid=stable_id('ent',sid,lid,'/'.join(p))
                    if eid in needed:found[eid]=(e,p)
        assert needed<=found.keys()
        for s in segments.values():
            e,p=found[s['entity_id']];assert p==s['entity_path']
            if e.dxftype()=='LINE':pts=[e.dxf.start,e.dxf.end]
            elif e.dxftype()=='LWPOLYLINE':
                pts=list(e.vertices_in_wcs())
                if e.closed:pts+=pts[:1]
            else:
                pts=list(e.points_in_wcs())
                if e.is_closed:pts+=pts[:1]
            for pt,native in [(s['a'],pts[s['segment_index']]),(s['b'],pts[s['segment_index']+1])]:assert math.isclose(pt[0],native.x,abs_tol=1e-7,rel_tol=0) and math.isclose(pt[1],native.y,abs_tol=1e-7,rel_tol=0)
        for eid,t in texts.items():e,p=found[eid];assert p==t['entity_path'] and decode_text(e)==t['text']
        for b in bundles.values():
            for name,h2 in b['parsed_artifact_sha256'].items():assert sha(path.parent/name)==h2
        reports.append(dict(snapshot_id=sid,source_sha256=h,bundle_count=len(bundles),segments_verified=len(segments),texts_verified=len(texts),passed=True));print('AUDIT',sid,len(segments),len(texts),flush=True)
    dump(OUT/'source-binding-audit.json',reports);dump(OUT/'negative-candidate-checks.json',neg);dump(OUT/'numeric-and-text-comparison.json',comparisons)

def subset(w,case,bun):
    idx=DrawingIndex(w,case['snapshot_id']);native=native_source(w,idx.snapshot)
    handles=sorted({v['entity_path'][0] for k in ('texts','segments','excluded_geometry') for v in bun[k]})
    doc=ezdxf.new(native['doc'].dxfversion);im=Importer(native['doc'],doc);im.import_entities([native['doc'].entitydb[h] for h in handles]);im.finalize()
    a,b,c,d=bun['bbox'];ox,oy=(a+c)/2,(b+d)/2;pad=max(c-a,d-b)*.12
    assert not list(transform.inplace(doc.modelspace(),Matrix44.translate(-ox,-oy,0)))
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'subset.dxf';doc.saveas(p);render=render_drawing(p,'real-subset')
    svg=render_svg(render,stable_id('layout','real-subset','Model'),[a-ox-pad,b-oy-pad,c-ox+pad,d-oy+pad])
    visibility=[]
    for eid in sorted({v['entity_id'] for k in ('texts','segments') for v in bun[k]}):
        record=native['entities'].get(eid)
        if record:e,path,*_=record
        else:
            t=next(v for v in bun['texts'] if v['entity_id']==eid);path=t['entity_path'];e=native['doc'].entitydb[path[0]]
        root=native['doc'].entitydb[path[0]];states=[]
        for item in [root,e]:
            layer=native['doc'].layers.get(item.dxf.layer)
            states.append(dict(layer=item.dxf.layer,off=layer.is_off(),frozen=layer.is_frozen(),invisible=bool(item.dxf.get('invisible',0))))
        visibility.append(dict(entity_id=eid,entity_path=path,states=states,visible=not any(s['off'] or s['frozen'] or s['invisible'] for s in states)))
    return svg,dict(root_handles=handles,source_sha256=bun['source_sha256'],display_origin=[ox,oy],rendered_items=len(render['items']),render_diagnostics=render['diagnostics'],visibility=visibility)

def overlay(svg,report,origin):
    from shapely.geometry import LineString
    from shapely.ops import nearest_points
    ox,oy=origin;shapes=[]
    def line(a,b,color):shapes.append(f'<path d="M {a[0]-ox} {oy-a[1]} L {b[0]-ox} {oy-b[1]}" fill="none" stroke="{color}" stroke-width="2" vector-effect="non-scaling-stroke"/>')
    for item in report['items']:
        if 'vertices' in item:
            pts=' '.join(f'{x-ox},{oy-y}' for x,y in item['vertices']);shapes.append(f'<polygon points="{pts}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
        elif 'segments' in item:
            for s in item['segments']:line(s['a'],s['b'],'#ec7800')
            p,q=nearest_points(*[LineString([s['a'],s['b']]) for s in item['segments']]);line([p.x,p.y],[q.x,q.y],'#ed2789')
        elif 'a' in item:line(item['a'],item['b'],'#ec7800')
        elif 'bbox' in item:
            a,b,c,d=item['bbox'];shapes.append(f'<rect x="{a-ox}" y="{oy-d}" width="{c-a}" height="{d-b}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
    return svg.replace('</svg>',''.join(shapes)+'</svg>')

def main():
    logging.getLogger('ezdxf').setLevel(logging.ERROR);w=Workspace(replace(Settings.load(),data_dir=OUT/'data'));rows=records();contexts={}
    try:
        audit(w,rows)
        grouped=defaultdict(list)
        for f,d in rows:grouped[d['case']['case_id']].append((f,d))
        for key,group in grouped.items():
            representative=next((d for f,d in group if d['method']=='closed_contour' and d.get('result')),group[0][1]);case=representative['case']
            contexts[key]=context(w,case,representative['run'])
            service=AdaptationService(w,case['snapshot_id']);bun=service.artifact('input',representative['run']['input_artifact_id'])
            svg,meta=subset(w,case,bun);contexts[key]['source_subset']=meta
            dump(OUT/(key+'-render-provenance.json'),contexts[key])
            for f,d in group:
                if not d.get('result'):continue
                for kind in ['source','overlay']:
                    p=OUT/(f.stem+'-'+kind+'.svg')
                    if not meta['rendered_items']:
                        dump(OUT/(f.stem+'-'+kind+'-render-error.json'),dict(error='SOURCE_RENDER_EMPTY',candidate_not_visually_accepted=True));continue
                    contents=svg if kind=='source' else overlay(svg,d['result']['validation'],meta['display_origin'])
                    p.write_text(contents);raster(contents,p.with_suffix('.png'))
            print('RENDER',key,flush=True)
        dump(OUT/'context-provenance.json',contexts)
    finally:w.close()
if __name__=='__main__':main()
