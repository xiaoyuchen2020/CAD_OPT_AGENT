"""Native source review and independent evidence audit for real nested-room replays."""
import argparse,copy,json,re
from collections import Counter
from shapely.geometry import LineString,box,Polygon
from cad_agent.geometry.source import native_source
from scripts.validate_nested_real_rooms import OUT,ROOT,read,dump,sha,workspace,fresh,history,validator,VALIDATOR_SHA
from scripts.report_nontable_real import subset
from scripts.report_cross_adaptation import raster

def selected():
    rows=read('results.json');gain=[r for r in rows if r['nested_repair_gain']]
    controls=[r for r in rows if r['case']['key'] in ['11816-native-061-full-precise','11816-native-001-full','11816-pilot-local-3','11816-native-071-full']]
    return gain+controls

def render():
    w=workspace()
    try:
        for r in selected():
            case=r['case'];key=case['key'];b=read(case['input_path']);target=OUT/'figures';target.mkdir(exist_ok=True)
            svg,meta=subset(w,case,b);ox,oy=meta['display_origin'];candidate=read(f'executions/{key}-new.json')['output']
            if candidate.get('vertices'):
                p=Polygon(candidate['vertices']);x,y,xx,yy=p.bounds;pad=max(900,min(xx-x,yy-y)*.55);bounds=[x-pad,y-pad,xx+pad,yy+pad]
                pts=' '.join(f'{x-ox},{oy-y}' for x,y in candidate['vertices'])
                outline=f'<polygon points="{pts}" fill="#078967" fill-opacity=".13" stroke="#078967" stroke-width="2.4" vector-effect="non-scaling-stroke"/>'
            else:
                x,y=case['seed'];bounds=[x-2500,y-2500,x+2500,y+2500];outline=''
            x,y,xx,yy=bounds;svg=re.sub(r'viewBox="[^"]+"',f'viewBox="{x-ox} {oy-yy} {xx-x} {yy-y}"',svg,count=1)
            for kind,content in [('source',svg),('overlay',svg.replace('</svg>',outline+'</svg>'))]:
                dest=target/f'{key}-{kind}.svg';dest.write_text(content);raster(content,dest.with_suffix('.png'))
            meta.update(display_bbox=bounds,source_input_id=b['input_id'],candidate_validation_status=r['new']['validation_status'])
            dump(target/f'{key}-provenance.json',meta);print('RENDER',key,meta['rendered_items'],flush=True)
    finally:w.close()

def verify():
    w=workspace();rows=read('results.json');reports=[];neg=[];v=validator();sources={};fresh_checks=[]
    try:
        for r in rows:
            case=r['case'];key=case['key'];b=read(case['input_path']);source=w.store.artifact(b['snapshot_id'],'source.dxf')
            if b['snapshot_id'] not in sources:
                assert sha(source)==b['source_sha256'];assert all(sha(source.parent/n)==h for n,h in b['parsed_artifact_sha256'].items())
                sources[b['snapshot_id']]=dict(snapshot_id=b['snapshot_id'],source_sha256=b['source_sha256'],source_and_parsed_hashes_verified=True)
            if r['new']['validation_status']!='passed':continue
            native=native_source(w,b['snapshot_id']);assert int(native['doc'].units)==4
            all_segments=b['segments']+[r['segment'] for r in b.get('scope_excluded_disjoint_segments',[])]
            for s in all_segments:
                e,path,*_=native['entities'][s['entity_id']];assert path==s['entity_path']
                if e.dxftype()=='LINE':ps=[e.dxf.start,e.dxf.end]
                elif e.dxftype()=='LWPOLYLINE':
                    ps=list(e.vertices_in_wcs())
                    if e.closed:ps+=ps[:1]
                else:
                    ps=list(e.points_in_wcs())
                    if e.is_closed:ps+=ps[:1]
                for k,i in [('a',s['segment_index']),('b',s['segment_index']+1)]:assert max(abs(s[k][j]-ps[i][j]) for j in [0,1])<1e-7
            for e in b.get('scope_excluded_disjoint_segments',[]):
                s=e['segment'];line=LineString([s['a'],s['b']]);assert line.disjoint(box(*b['bbox']).buffer(b['tolerance']))
            reports.append(dict(key=key,segments_verified=len(all_segments),scope_disjoint_proofs=len(b.get('scope_excluded_disjoint_segments',[])),native_units=4))
            baseline=read(f'baselines/{key}.json')['before'];after=fresh(w,case)
            from cad_agent.extensions.table_recovery.bundle import digest
            assert digest(baseline)==digest(after);fresh_checks.append(dict(key=key,before_sha256=digest(baseline),after_sha256=digest(after),unchanged=True))
            if not r['nested_repair_gain']:continue
            o=read(f'executions/{key}-new.json')['output']
            changes=[('foreign_input',dict(input_id='foreign')),('wrong_area',dict(area=o['area']+10000)),('foreign_source',dict(edge_segment_ids=[['foreign']]+o['edge_segment_ids'][1:]))]
            for name,change in changes:
                bad=v.validate(b,o|change);assert bad['validation_status']=='rejected';neg.append(dict(key=key,mutation=name,result=bad))
            # Returning the original broad-scope bundle must still reject unknown clipping.
            if case.get('parent_case'):
                parent=next(c for c in read('screening.json') if c['key']==case['parent_case']);rawb=read(parent['input_path'])
                bad=v.validate(rawb,o|dict(input_id=rawb['input_id']));assert bad['errors']==['UNSUPPORTED_VISIBILITY'];neg.append(dict(key=key,mutation='restore_disjoint_but_unsupported_parent_scope',result=bad))
        manifest=read('production-before.json');changed=[p for p,h in manifest.items() if not (ROOT/p).exists() or sha(ROOT/p)!=h]
        dump(OUT/'source-binding-audit.json',dict(sources=list(sources.values()),cases=reports))
        dump(OUT/'negative-controls.json',neg)
        dump(OUT/'preservation.json',dict(fresh_baselines=fresh_checks,prior_evidence=history(),concurrent_production_changes=changed,this_task_production_edits=False,automatic_adoption=False,automatic_registration=False,validator_sha256=VALIDATOR_SHA))
        print('AUDIT',len(reports),'native cases;',len(neg),'negative controls;',len(fresh_checks),'fresh unchanged baselines',flush=True)
    finally:w.close()

if __name__=='__main__':
    import logging
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    p=argparse.ArgumentParser();p.add_argument('action',choices=['render','verify']);a=p.parse_args();globals()[a.action]()
