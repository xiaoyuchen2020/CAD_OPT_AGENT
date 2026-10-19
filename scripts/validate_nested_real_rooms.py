"""Frozen nested-room code replay on existing real drawings, with native provenance."""
import argparse,copy,json,logging,math,shutil,sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path
from shapely import set_precision,STRtree
from shapely.geometry import LineString,Point,Polygon,box
from shapely.ops import unary_union,polygonize
from cad_agent.config import ROOT,Settings
from cad_agent.service import Workspace
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.source import native_source
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import RoomMeasure
from cad_agent.extensions.table_recovery.bundle import prepare_region,digest
from scripts.room_boundary_pilot import visibility_map,dump,sha
from scripts.room_boundary_revision import validator,VALIDATOR_SHA
from scripts.nested_room_boundary import history as prior_history

OUT=ROOT/'artifacts/dynamic-room-nested-real-20261016'
PRIOR=ROOT/'artifacts/dynamic-room-nested-boundary-20261016'
SOURCE=ROOT/'artifacts/ar030-real-room-validation-20261015'

def read(name):return json.loads((OUT/name).read_text())
def history():
    rows=prior_history();m=json.loads((PRIOR/'evidence-sha256.json').read_text())
    assert all(sha(PRIOR/p)==h for p,h in m.items());return rows+[dict(root=PRIOR.name,files=len(m),unchanged=True)]
def workspace():return Workspace(replace(Settings.load(),data_dir=OUT/'data'))

def prepare():
    assert not OUT.exists();OUT.mkdir();(OUT/'data').mkdir()
    dump(OUT/'history-before.json',history())
    for n in ['adaptation-runtime.json','table-recovery-runtime.json']:shutil.copy2(PRIOR/'data'/n,OUT/'data'/n)
    shutil.copy2(PRIOR/'candidate-local-v2.py',OUT/'candidate-new.py')
    shutil.copy2(ROOT/'artifacts/dynamic-room-support-repair-20261016/candidate-4.py',OUT/'candidate-old.py')
    shutil.copy2(PRIOR/'validator-frozen.py',OUT/'validator-frozen.py');assert sha(OUT/'validator-frozen.py')==VALIDATOR_SHA
    dump(OUT/'production-before.json',{str(p.relative_to(ROOT)):sha(p) for folder in ['cad_agent','web'] for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts})
    c=sqlite3.connect(f'file:{SOURCE}/data/index.sqlite3?mode=ro',uri=True)
    records=[json.loads(s) for s, in c.execute("select data from records where kind='drawing'")];c.close()
    dump(OUT/'drawing-inventory.json',[dict(snapshot_id=d['id'],name=d['name'],unit=d['unit'],sha256=d['sha256']) for d in records])
    dump(OUT/'scope.json',dict(source='existing historical real-drawing snapshots',model_calls=0,automatic_adoption=False,automatic_registration=False,
        old_code_sha256=sha(OUT/'candidate-old.py'),new_code_sha256=sha(OUT/'candidate-new.py'),validator_sha256=VALIDATOR_SHA,
        selection='Existing native room labels near historical anchors; no historical contour or expected area is supplied to candidate algorithms.',
        unit_gate='Frozen validator requires native INSUNITS=4; no overrides or unit assumptions.'))

def load_drawing(w,pid):
    c=sqlite3.connect(f'file:{SOURCE}/data/index.sqlite3?mode=ro',uri=True)
    ds=[json.loads(s) for s, in c.execute("select data from records where kind='drawing'")];c.close()
    d=next(d for d in ds if d['name']==str(pid)+'.dxf');srcroot=SOURCE/'data'
    options=[(d,srcroot)]
    for root in [ROOT/'artifacts/table-recovery-gap-audit-20261016/search-data',ROOT/'artifacts/dynamic-nontable-real-20261016/data']:
        c=sqlite3.connect(f'file:{root}/index.sqlite3?mode=ro',uri=True)
        for s, in c.execute("select data from records where kind='drawing'"):
            candidate=json.loads(s)
            if candidate['sha256']==d['sha256']:options.append((candidate,root))
        c.close()
    # Reuse a later complete existing parse of the identical source where available;
    # never delete diagnostics or alter a snapshot's metadata.
    d,srcroot=min(options,key=lambda item:sum(x.get('code') in ['ENTITY_LIMIT','UNLOCATED_GEOMETRY','BLOCK_CYCLE','TEXT_READ_FAILED'] for x in item[0]['diagnostics']))
    sid=d['id']
    if not (OUT/'data/drawings'/sid).exists():shutil.copytree(srcroot/'drawings'/sid,OUT/'data/drawings'/sid)
    w.store.put('drawing',sid,d);assert sha(w.store.artifact(sid,'source.dxf'))==d['sha256']
    return d

def scan(papers,extra=False):
    w=workspace();rows=json.loads((SOURCE/'review_points.json').read_text());screen=[]
    try:
        for pid in papers:
            d=load_drawing(w,pid);sid=d['id'];idx=DrawingIndex(w,sid);native=native_source(w,sid);doc=native['doc']
            print('DRAWING',pid,'INSUNITS',int(doc.units),flush=True)
            anchors=[r for r in rows if r['paper_id']==pid];vmap={};used=set();drawing_rows=[];full=d['name']!=str(pid)+'.dxf'
            if extra:
                names=['强电井','弱电井','水井','新风井','排烟井','风井','集气室','扩散室','除尘室','滤毒室','通信机房','广电机房','排烟机房','新风机房','消防泵房','生活水泵房']
                labels=sorted([t for t in idx.scene['texts'] if t['text'] in names],key=lambda t:(names.index(t['text']),t['origin']))
                distinct=[]
                for t in labels:
                    if any(t['text']==p['text'] and t['layout_id']==p['layout_id'] and math.dist(t['origin'],p['origin'])<500 for p in distinct):continue
                    distinct.append(t)
                anchors=[dict(key=f'{pid}-native-{i+1:03}',source=dict(name=next(r['source']['name'] for r in rows if r['paper_id']==pid)),measurement=dict(label=t['origin'],roomType=t['text'])) for i,t in enumerate(distinct[:160])]
            known_rows={r['key']:r for r in read('screening.json')} if (OUT/'screening.json').exists() else {}
            for row in anchors:
                key=row['key']+('-full' if full else '');case=dict(key=key,paper_id=pid,dwg_name=row['source']['name'],room_type=row['measurement']['roomType'],snapshot_id=sid,source_sha256=d['sha256'])
                if key in known_rows:
                    drawing_rows.append(known_rows[key]);continue
                if int(doc.units)!=4:case.update(screen_status='input_unit_unsupported',insunits=int(doc.units));drawing_rows.append(case);continue
                point=row['measurement']['label'];room_type=row['measurement']['roomType']
                labels=[t for t in idx.scene['texts'] if room_type in t['text'] and len(t['text'])<25 and math.dist(t['origin'],point)<1000]
                if not labels:case['screen_status']='no_native_label_at_anchor';drawing_rows.append(case);continue
                label=min(labels,key=lambda t:math.dist(t['origin'],point));eid=label['entity_id']
                if eid in used:case['screen_status']='duplicate_native_label';drawing_rows.append(case);continue
                used.add(eid);x,y=label['origin'];radius=4000 if '井' in room_type else 8000 if extra else 20000
                bounds=[x-radius,y-radius,x+radius,y+radius]
                case.update(label=label['text'],label_entity_id=eid,seed=[x,y],layout_id=label['layout_id'],bbox=bounds)
                try:
                    b=prepare_region(idx,label['layout_id'],bounds)
                    roots={v['entity_path'][0] for k in ['segments','texts','excluded_geometry'] for v in b[k]}
                    for root in roots:
                        if root not in vmap:vmap[root]=visibility_map(doc,[root])
                    for k in ['segments','texts','excluded_geometry']:
                        for v in b[k]:v['visibility']=vmap[v['entity_path'][0]][tuple(v['entity_path'])]
                    b['target']=dict(label_entity_id=eid,label=label['text'],seed=[x,y])
                    b['unit_evidence']=dict(insunits=int(doc.units),measurement=doc.header.get('$MEASUREMENT'),effective_unit='mm',metres_per_unit=.001,source_sha256=b['source_sha256'],override_used=False,basis='Native DXF INSUNITS=4')
                    b['visibility_scope']='Model layer off/frozen, entity invisible, INSERT inheritance; XCLIP unsupported; not paper viewport or plotting.'
                    b['policy']=dict(wall_layers=['WALL','COLU'],opening_layers=['WINDOW'],excluded_layer_patterns=['AXIS','DIM','DOTE','轴'],tolerance_units=1.0,max_opening_units=3000,minimum_parallel_support_fraction=.5,wall_thickness_range=[60,500])
                    body={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']};h=digest(body);b=body|dict(input_id='room_'+h,bundle_sha256=h)
                    segments=[s for s in b['segments'] if s['visibility']['visible'] and s['visibility']['effective_layer'] in ['WALL','COLU','WINDOW']]
                    case.update(segments=len(b['segments']),structural_segments=len(segments),blocking=b['coverage']['blocking_diagnostics'])
                    if b['coverage']['blocking_diagnostics']:case['screen_status']='input_coverage_blocked'
                    elif any(not s['visibility']['supported'] for s in b['segments']):case['screen_status']='visibility_unsupported'
                    elif len(segments)>4000:case['screen_status']='sandbox_graph_bound'
                    elif not segments:case['screen_status']='no_supported_wall_layers'
                    else:
                        lines=[set_precision(LineString([s['a'],s['b']]),1e-5) for s in segments]
                        fs=list(polygonize(unary_union(lines)));seed=Point(x,y);containing=[p for p in fs if p.contains(seed)]
                        shells=[p for p in fs if Polygon(p.exterior).contains(seed)]
                        case.update(native_containing_faces=len(containing),containing_exterior_loops=len(shells),native_holes=[len(p.interiors) for p in containing])
                        case['screen_status']='nested_candidate' if len(containing)==1 and not containing[0].interiors and len(shells)>1 else 'non_nested_control' if len(containing)==1 and not containing[0].interiors else 'native_target_open_or_holes'
                    dump(OUT/'inputs'/f'{key}.json',b)
                    case['input_path']=f'inputs/{key}.json'
                except Exception as exc:case.update(screen_status='input_error',error=str(exc)[:250])
                drawing_rows.append(case);print('SCREEN',key,case['screen_status'],case.get('containing_exterior_loops'),case.get('structural_segments'),flush=True)
            dump(OUT/f'screen-{"extra-" if extra else ""}{pid}{"-full" if full else ""}.json',drawing_rows);screen.extend(drawing_rows)
        if (OUT/'screening.json').exists():
            existing=read('screening.json');known={r['key'] for r in existing};screen=existing+[r for r in screen if r['key'] not in known]
        dump(OUT/'screening.json',screen)
    finally:w.close()

def fresh(w,case):
    gs=GeometryService(w,case['snapshot_id']);gs.save=lambda args,result,algorithm:dict(result,algorithm_version=algorithm)
    return gs.room(RoomMeasure(layout_id=case['layout_id'],seed=case['seed']))

def pilot_locals():
    """Select additional real labels inside the already source-audited pilot region."""
    source=ROOT/'artifacts/dynamic-room-support-repair-20261016';original=json.loads((source/'input.json').read_text());sid=original['snapshot_id']
    w=workspace()
    try:
        if not (OUT/'data/drawings'/sid).exists():shutil.copytree(source/'data/drawings'/sid,OUT/'data/drawings'/sid)
        c=sqlite3.connect(f'file:{source}/data/index.sqlite3?mode=ro',uri=True);d=json.loads(c.execute("select data from records where kind='drawing' and id=?",(sid,)).fetchone()[0]);c.close();w.store.put('drawing',sid,d)
    finally:w.close()
    rows=[];seen=[]
    for t in original['texts']:
        if t['text'] not in ['补风井','排烟井','女卫','男卫','器材','消控室']:continue
        if any(p['text']==t['text'] and math.dist(p['origin'],t['origin'])<700 for p in seen):continue
        seen.append(t);key='11816-pilot-local-'+str(len(seen));b=copy.deepcopy(original)
        b['target']=dict(label_entity_id=t['entity_id'],label=t['text'],seed=t['origin'])
        body={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']};h=digest(body);b=body|dict(input_id='room_'+h,bundle_sha256=h)
        segs=[s for s in b['segments'] if s['visibility']['visible'] and s['visibility']['effective_layer'] in ['WALL','COLU','WINDOW']]
        fs=list(polygonize(unary_union([set_precision(LineString([s['a'],s['b']]),1e-5) for s in segs])));point=Point(t['origin'])
        containing=[p for p in fs if p.contains(point)];shells=[p for p in fs if Polygon(p.exterior).contains(point)]
        status='nested_candidate' if len(containing)==1 and not containing[0].interiors and len(shells)>1 else 'non_nested_control' if len(containing)==1 and not containing[0].interiors else 'native_target_open_or_holes'
        case=dict(key=key,paper_id=11816,dwg_name='孔浦地块人防地下室施工图20240911_t3.dwg',room_type=t['text'],label=t['text'],label_entity_id=t['entity_id'],snapshot_id=sid,source_sha256=b['source_sha256'],seed=t['origin'],layout_id=b['layout_id'],bbox=b['bbox'],input_path=f'inputs/{key}.json',screen_status=status,containing_exterior_loops=len(shells),native_holes=[len(p.interiors) for p in containing],scope_basis='Unchanged previously audited native region; new target label only')
        dump(OUT/case['input_path'],b);rows.append(case);print('PILOT_LOCAL',key,t['text'],status,len(shells),flush=True)
    dump(OUT/'screen-pilot-locals.json',rows)
    if (OUT/'screening.json').exists():
        current=read('screening.json');known={r['key'] for r in current};dump(OUT/'screening.json',current+[r for r in rows if r['key'] not in known])

def precise_scope():
    """Remove only proved-disjoint unsupported segments admitted by broad AABB query."""
    rows=read('screening.json');bylabel={r.get('label_entity_id'):r for r in rows if r['key'].endswith('-full')};added=[]
    for discovered in read('global-discovery.json')['nested_labels']:
        source=bylabel[discovered['entity_id']];b=read(source['input_path']);original=copy.deepcopy(b)
        scope=box(*b['bbox']).buffer(b['tolerance']);excluded=[];kept=[]
        for s in b['segments']:
            line=LineString([s['a'],s['b']])
            if not s['visibility']['supported'] and line.disjoint(scope):
                excluded.append(dict(segment=s,reason='Native segment is geometrically disjoint from scope including tolerance; AABB false positive',distance_to_scope_native=line.distance(scope)))
            else:kept.append(s)
        assert excluded and all(r['distance_to_scope_native']>0 for r in excluded)
        b['segments']=kept;b['scope_excluded_disjoint_segments']=excluded
        b['scope_refinement']=dict(parent_input_id=original['input_id'],parent_bundle_sha256=original['bundle_sha256'],method='Exact native line/region disjoint proof; no clipping state changed; no intersecting or potentially intersecting line removed')
        body={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']};h=digest(body);b=body|dict(input_id='room_'+h,bundle_sha256=h)
        assert all(s['visibility']['supported'] for s in b['segments'])
        structural=[s for s in b['segments'] if s['visibility']['visible'] and s['visibility']['effective_layer'] in ['WALL','COLU','WINDOW']]
        fs=list(polygonize(unary_union([set_precision(LineString([s['a'],s['b']]),1e-5) for s in structural])));point=Point(b['target']['seed']);containing=[p for p in fs if p.contains(point)];shells=[p for p in fs if Polygon(p.exterior).contains(point)]
        assert len(containing)==1 and not containing[0].interiors and len(shells)>1
        case=dict(source,key=source['key']+'-precise',screen_status='nested_candidate',containing_exterior_loops=len(shells),scope_refined=True,parent_case=source['key'])
        case['input_path']=f'inputs/{case["key"]}.json';dump(OUT/case['input_path'],b);added.append(case)
        dump(OUT/'scope-audits'/f'{case["key"]}.json',dict(original_input_id=original['input_id'],refined_input_id=b['input_id'],original_segment_count=len(original['segments']),remaining_segment_count=len(kept),excluded=excluded,all_remaining_segments_unchanged=True,validation_criteria_unchanged=True))
        print('PRECISE_SCOPE',case['key'],'removed disjoint',len(excluded),'nested loops',len(shells),flush=True)
    known={r['key'] for r in rows};dump(OUT/'screening.json',rows+[r for r in added if r['key'] not in known])

def discover():
    segments={};texts={};inputs=[]
    for f in sorted((OUT/'inputs').glob('*-full.json')):
        b=json.loads(f.read_text())
        if b['snapshot_id']!='snap_a286d51bccb116dba9e2ee9b':continue
        inputs.append(dict(path=str(f.relative_to(OUT)),sha256=sha(f)))
        for s in b['segments']:
            if s['visibility']['visible'] and s['visibility']['supported'] and s['visibility']['effective_layer'] in ['WALL','COLU','WINDOW']:segments[s['segment_id']]=s
        for t in b['texts']:
            if t['visibility']['visible']:texts[t['entity_id']]=t
    fs=list(polygonize(unary_union([set_precision(LineString([s['a'],s['b']]),1e-5) for s in segments.values()])))
    shells=[Polygon(f.exterior) for f in fs];tree=STRtree(shells);found=[]
    for t in texts.values():
        if len(t['text'])>16:continue
        point=Point(t['origin']);indices=[int(i) for i in tree.query(point) if shells[int(i)].contains(point)]
        if len(indices)>1:found.append(dict(text=t['text'],entity_id=t['entity_id'],origin=t['origin'],containing_exteriors=len(indices),bounds=[list(shells[i].bounds) for i in indices],areas=[shells[i].area for i in indices]))
    result=dict(discovery_only=True,not_complete_source_claim=True,segments=len(segments),faces=len(fs),texts=len(texts),nested_labels=found,inputs=inputs)
    dump(OUT/'global-discovery-reproduced.json',result)
    assert {x['entity_id'] for x in found}=={x['entity_id'] for x in read('global-discovery.json')['nested_labels']}
    print('reproduced discovery',len(found),'nested native labels',flush=True)

def run():
    from cad_agent.extensions.adaptation.sandbox import execute
    w=workspace();rows=[];v=validator()
    try:
        for case in read('screening.json'):
            if case['screen_status'] not in ['nested_candidate','non_nested_control']:continue
            key=case['key'];target=OUT/'results'/f'{key}.json'
            if target.exists():rows.append(json.loads(target.read_text()));continue
            b=read(case['input_path']);r=dict(case=case,model_calls=0)
            for version in ['old','new']:
                try:
                    ex=execute(OUT/'data',b,(OUT/f'candidate-{version}.py').read_text(),timeout=20)
                    validation=v.validate(b,ex['output']);dump(OUT/'executions'/f'{key}-{version}.json',ex)
                    r[version]=validation
                except Exception as exc:r[version]=dict(validation_status='execution_failed',errors=[str(exc)[:200]])
            if r['new']['validation_status']=='passed':
                before=fresh(w,case);after=fresh(w,case);assert digest(before)==digest(after)
                dump(OUT/'baselines'/f'{key}.json',dict(before=before,after=after,unchanged=True))
                r.update(baseline_status=before['status'],baseline_candidate_area_m2=before.get('candidate_area_m2'),baseline_unchanged=True)
                out=read(f'executions/{key}-new.json')['output'];p=Polygon(out['vertices']);same=False
                for c in before.get('candidates',[]):
                    if c.get('boundary'):
                        q=Polygon(c['boundary'],c.get('holes',[]))
                        if p.symmetric_difference(q).area<max(1,p.length) and p.boundary.hausdorff_distance(q.boundary)<=2:same=True
                r['baseline_contains_same_geometry']=same
            r['nested_repair_gain']=r['old']['validation_status']!='passed' and r['new']['validation_status']=='passed'
            r['component_geometry_gain_candidate']=r['nested_repair_gain'] and not r.get('baseline_contains_same_geometry',True)
            dump(target,r);rows.append(r)
            print('RESULT',key,r['old']['validation_status'],r['new']['validation_status'],r.get('baseline_status'),r.get('baseline_contains_same_geometry'),flush=True)
        dump(OUT/'results.json',rows)
    finally:w.close()

if __name__=='__main__':
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','scan','run','pilot_locals','precise_scope','discover']);p.add_argument('--papers',default='11794,11798,11805,11807,11809,11810,11816');p.add_argument('--extra',action='store_true');a=p.parse_args()
    scan([int(x) for x in a.papers.split(',')],extra=a.extra) if a.action=='scan' else globals()[a.action]()
