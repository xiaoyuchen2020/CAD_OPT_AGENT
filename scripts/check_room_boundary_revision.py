"""Independent frozen validation, controls, stability replays and review rendering."""
import copy,json,math,random,re,runpy
from pathlib import Path
from scripts.room_boundary_revision import OUT,OLD,ROOT,read,dump,sha,digest,validator,workspace,source_check,fresh_baseline
from scripts.report_cross_adaptation import raster

def seal(b):
    body={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']};h=digest(body);return body|dict(input_id='room_'+h,bundle_sha256=h)
def selected():
    run=read('run.json');number=run.get('accepted_attempt')
    if number is None:number=max(v['number'] for v in run['attempts'] if (OUT/f'execution-{v["number"]}.json').exists() and read(f'execution-{v["number"]}.json')['output'].get('status')=='candidate')
    a=next(v for v in run['attempts'] if v['number']==number);return a,read(f'execution-{number}.json')['output']

def preserve():
    b=read('input.json');w=workspace()
    try:source_check(w,b);after=fresh_baseline(w,b);before=read('baseline-before-fresh.json');assert digest(after)==digest(before);dump(OUT/'baseline-after-fresh.json',after)
    finally:w.close()
    manifest=read('production-before.json');changes=[p for p,h in manifest.items() if not (ROOT/p).exists() or sha(ROOT/p)!=h]
    evidence=[]
    for name in ['dynamic-adaptation-20261016','dynamic-cross-drawing-20261016','dynamic-shadow-near-axis-20261016','dynamic-multimethod-20261016','dynamic-nontable-real-20261016','dynamic-room-boundary-20261016']:
        root=ROOT/'artifacts'/name;m=json.loads((root/'evidence-sha256.json').read_text());m=m.get('files',m)
        assert all(sha(root/p)==h for p,h in m.items()),name;evidence.append(dict(root=name,files=len(m),unchanged=True))
    dump(OUT/'preservation.json',dict(fresh_baseline_equal=True,before_sha256=digest(before),after_sha256=digest(after),
        source_and_parsed_hashes_verified=True,execution_input_byte_identical=True,validator_byte_identical=True,
        production_files_checked=len(manifest),concurrent_workspace_changes=changes,prior_evidence=evidence,
        this_task_production_edits=False,automatic_adoption=False,automatic_registration=False))
    print('preserved',sum(x['files'] for x in evidence),'historical files; concurrent changes',len(changes),flush=True)

def negatives():
    a,o=selected();b=read('input.json');v=validator();assert v.validate(b,o)['validation_status']=='passed';rows=[]
    def check(name,bb,oo,error=None):
        result=v.validate(bb,oo);assert result['validation_status']=='rejected',(name,result)
        if error:assert result['errors']==[error],(name,result)
        rows.append(dict(name=name,expected_error=error,validation=result))
    for name,change,error in [('wrong_input',dict(input_id='foreign'),'WRONG_INPUT_ID'),('wrong_area',dict(area=o['area']+10000),'AREA_MISMATCH'),('missing_exclusions',dict(excluded_crossing_ids=[]),'CROSSING_EXCLUSIONS_INCOMPLETE_OR_FALSE')]:check(name,b,o|change,error)
    bad=copy.deepcopy(o);bad['edge_segment_ids'][0]=['foreign'];check('foreign_edge',b,bad,'FOREIGN_OR_DUPLICATE_SEGMENT')
    bad=copy.deepcopy(o);bad['edge_segment_ids'][0]=[o['excluded_crossing_ids'][0]];check('crossing_as_boundary',b,bad,'NONSTRUCTURAL_BOUNDARY')
    bad=copy.deepcopy(o);bad['vertices'][0][0]+=50;check('invented_edge',b,bad)
    sid=o['edge_segment_ids'][0][0]
    for name,error in [('hidden','HIDDEN_BOUNDARY_SOURCE'),('unit','UNIT_EVIDENCE_INVALID'),('xclip','UNSUPPORTED_VISIBILITY'),('target','TARGET_OUTSIDE'),('coverage','INCOMPLETE_SOURCE'),('integrity','INPUT_INTEGRITY_FAILED')]:
        bb=copy.deepcopy(b);oo=copy.deepcopy(o)
        if name=='hidden':next(s for s in bb['segments'] if s['segment_id']==sid)['visibility']['visible']=False
        elif name=='unit':bb['unit_evidence']['insunits']=0
        elif name=='xclip':bb['segments'][0]['visibility']['supported']=False
        elif name=='target':bb['target']['seed']=[bb['bbox'][0]-100,bb['bbox'][1]-100];next(t for t in bb['texts'] if t['entity_id']==bb['target']['label_entity_id'])['origin']=bb['target']['seed']
        elif name=='coverage':bb['coverage']['blocking_diagnostics']=[dict(code='UNLOCATED_GEOMETRY')]
        else:bb['segments'][0]['a'][0]+=100
        if name!='integrity':bb=seal(bb);oo['input_id']=bb['input_id']
        check(name,bb,oo,error)
    old=json.loads((OLD/'execution-2.json').read_text())['output'];check('old_generated_backtrack',b,old,'BOUNDARY_NUMERICAL_BACKTRACK')
    dump(OUT/'negative-controls.json',rows);print('negatives',len(rows),flush=True)

def replays():
    from cad_agent.extensions.adaptation.sandbox import execute
    a,o=selected();code=(OUT/a['code_path']).read_text();original=read('input.json');v=validator();cases=[]
    for label,dx,dy,rev in [('translated',1000000,-2000000,False),('permuted_reversed',0,0,True)]:
        b=copy.deepcopy(original)
        def pt(p):return [p[0]+dx,p[1]+dy]
        b['target']['seed']=pt(b['target']['seed']);b['bbox']=[n+(dx if i%2==0 else dy) for i,n in enumerate(b['bbox'])]
        for t in b['texts']:t['origin']=pt(t['origin']);t['bbox']=[n+(dx if i%2==0 else dy) for i,n in enumerate(t['bbox'])]
        for s in b['segments']:s['a']=pt(s['a']);s['b']=pt(s['b'])
        if rev:
            random.Random(16016).shuffle(b['segments'])
            for s in b['segments']:s['a'],s['b']=s['b'],s['a']
        b['test_derivation']=dict(kind='synthetic_metamorphic',parent_input_id=original['input_id'],translation=[dx,dy],permuted_reversed=rev);cases.append((label,seal(b),'passed'))
    # Independently constructed rectangle with overlapping, reversed and tiny-spur support.
    b,expected=runpy.run_path(str(ROOT/'tests/test_room_boundary_pilot.py'))['fixture']()
    b['segments'].extend([dict(segment_id='overlap',entity_path=['overlap'],a=[800,0],b=[200,0],visibility=dict(effective_layer='WALL',visible=True,supported=True)),
      dict(segment_id='near_overlap',entity_path=['near_overlap'],a=[1000,1e-9],b=[500,1e-9],visibility=dict(effective_layer='WALL',visible=True,supported=True)),
      dict(segment_id='spur1',entity_path=['spur1'],a=[600,0],b=[600,60],visibility=dict(effective_layer='WALL',visible=True,supported=True)),
      dict(segment_id='spur2',entity_path=['spur2'],a=[600+1e-9,60],b=[600+1e-9,0],visibility=dict(effective_layer='WALL',visible=True,supported=True))])
    b['test_derivation']=dict(kind='independent_synthetic_control');cases.append(('synthetic_overlap_spur',seal(b),'passed'))
    hidden=copy.deepcopy(b);next(s for s in hidden['segments'] if s['segment_id']=='i2')['visibility']['visible']=False;cases.append(('synthetic_hidden_boundary',seal(hidden),'not_passed'))
    unknown=copy.deepcopy(b);unknown['unit_evidence'].update(insunits=0,effective_unit='unknown',metres_per_unit=None);cases.append(('synthetic_unknown_unit',seal(unknown),'not_passed'))
    rows=[]
    for name,b,expect in cases:
        dump(OUT/f'{name}-input.json',b);ex=execute(OUT/'data',b,code,timeout=20);dump(OUT/f'{name}-execution.json',ex);validation=v.validate(b,ex['output']);dump(OUT/f'{name}-validation.json',validation)
        ok=validation['validation_status']=='passed' if expect=='passed' else validation['validation_status']!='passed'
        if validation['validation_status']=='passed':
            expected_area=2000000 if name=='synthetic_overlap_spur' else o['area']
            ok=ok and abs(validation['checks']['area_native']-expected_area)<=1
        rows.append(dict(name=name,expected=expect,expectation_met=ok,validation_status=validation['validation_status'],errors=validation['errors'],
            area_native=ex['output'].get('area'),code_sha256=a['code_sha256'],evidence_class='synthetic_control_not_new_drawing'))
        print('REPLAY',name,validation['validation_status'],validation['errors'],flush=True)
    dump(OUT/'replay-results.json',rows)

def render():
    a,o=selected();meta=read('render-provenance.json');ox,oy=meta['display_origin'];svg=(OUT/'source.svg').read_text();b=read('input.json')
    pts=' '.join(f'{x-ox},{oy-y}' for x,y in o['vertices']);outline=f'<polygon points="{pts}" fill="#078967" fill-opacity=".1" stroke="#078967" stroke-width="2.4" vector-effect="non-scaling-stroke"/>'
    overlay=svg.replace('</svg>',outline+'</svg>');(OUT/'candidate-overlay.svg').write_text(overlay);raster(overlay,OUT/'candidate-overlay.png')
    xs=[p[0] for p in o['vertices']];ys=[p[1] for p in o['vertices']]
    crops=[('candidate-detail',[min(xs)-700,min(ys)-700,max(xs)+700,max(ys)+700]),('upper-door',[522900,169050,524250,170450]),('lower-door',[522900,151700,524250,154500])]
    for name,(x,y,xx,yy) in crops:
        z=re.sub(r'viewBox="[^"]+"',f'viewBox="{x-ox} {oy-yy} {xx-x} {yy-y}"',overlay,count=1);(OUT/f'{name}.svg').write_text(z);raster(z,OUT/f'{name}.png')
    ids=set(o['excluded_crossing_ids']);lines=[]
    for s in b['segments']:
        if s['segment_id'] in ids:
            x,y=s['a'];xx,yy=s['b'];lines.append(f'<path d="M {x-ox} {oy-y} L {xx-ox} {oy-yy}" fill="none" stroke="#da2777" stroke-width="1.5" vector-effect="non-scaling-stroke"/>')
    z=svg.replace('</svg>',''.join(lines)+outline+'</svg>');(OUT/'excluded-crossings.svg').write_text(z);raster(z,OUT/'excluded-crossings.png')
    print('source overlays ready',flush=True)

def numeric_diagnostics():
    from shapely import set_precision
    from shapely.geometry import Polygon,LineString
    b=read('input.json');a,o=selected();old=json.loads((OLD/'execution-2.json').read_text())['output'];rows=[]
    for label,raw in [('previous_rejected',old),('new_raw_candidate',o)]:
        p=Polygon(raw['vertices']);q=set_precision(p,b['policy']['tolerance_units']*1e-5)
        rows.append(dict(label=label,vertices=len(raw['vertices']),valid=p.is_valid,raw_to_canonical_boundary_distance_mm=p.boundary.hausdorff_distance(q.boundary),raw_to_canonical_area_delta_mm2=p.symmetric_difference(q).area))
    structural=[s for s in b['segments'] if s['visibility']['visible'] and s['visibility']['effective_layer'] in b['policy']['wall_layers']+b['policy']['opening_layers']]
    overlaps=[]
    for i,s in enumerate(structural):
        line=LineString([s['a'],s['b']])
        for t in structural[:i]:
            length=set_precision(line,1e-5).intersection(set_precision(LineString([t['a'],t['b']]),1e-5)).length
            if length>.1:overlaps.append(dict(segment_ids=[s['segment_id'],t['segment_id']],overlap_mm=length))
    dump(OUT/'numeric-diagnostics.json',dict(candidate_comparison=rows,native_overlapping_segment_pairs=len(overlaps),overlaps=overlaps,not_used_as_generator_expected_output=True))
    print('numeric diagnostics',rows,'overlaps',len(overlaps),flush=True)

if __name__=='__main__':
    import sys
    for action in sys.argv[1:]:globals()[action]()
