"""Readback, adversarial controls, source overlays and report for the opt-in pilot."""
import copy,json,html,math
from pathlib import Path
from collections import Counter
from scripts.room_boundary_pilot import OUT,ROOT,PRIOR,read,dump,sha,workspace,baseline,digest
from scripts.room_boundary_pilot_validation import validate,VERSION
from scripts.report_cross_adaptation import raster

def seal(b):
    b={k:v for k,v in b.items() if k not in ['input_id','bundle_sha256']};h=digest(b);return b|dict(input_id='room_'+h,bundle_sha256=h)

def verify():
    w,r=workspace()
    try:
        after=baseline(w,r);dump(OUT/'baseline-after.json',after)
        before=read('baseline-before.json');assert digest(before)==digest(after)
        # Immutable observation readback alone can conceal a changed recomputation.
        # Intercept only this service instance's save boundary to observe fresh raw calculation.
        from cad_agent.geometry.service import GeometryService
        from cad_agent.geometry.contracts import RoomMeasure
        service=GeometryService(w,r['case']['snapshot_id'])
        service.save=lambda args,result,algorithm: dict(result,algorithm_version=algorithm)
        fresh=service.room(RoomMeasure(**before['request']))
        expected={k:before[k] for k in fresh}
        assert digest(fresh)==digest(expected),'FRESH_BASELINE_RECOMPUTATION_CHANGED'
        dump(OUT/'fresh-baseline-recomputation.json',dict(result=fresh,expected_projection=expected,actual_sha256=digest(fresh),expected_sha256=digest(expected),matches=True,immutable_store_bypassed_for_observation=True))

        b=read('input.json');source=w.store.artifact(b['snapshot_id'],'source.dxf');assert sha(source)==b['source_sha256']
        assert all(sha(source.parent/n)==h for n,h in b['parsed_artifact_sha256'].items())
        old=read('production-before.json');changes=[p for p,h in old.items() if not (ROOT/p).exists() or sha(ROOT/p)!=h]
        evidence=[]
        for name in ['dynamic-adaptation-20261016','dynamic-cross-drawing-20261016','dynamic-shadow-near-axis-20261016','dynamic-multimethod-20261016','dynamic-nontable-real-20261016']:
            root=ROOT/'artifacts'/name;manifest=json.loads((root/'evidence-sha256.json').read_text())
            if 'files' in manifest:manifest=manifest['files']
            failures=[p for p,h in manifest.items() if sha(root/p)!=h];assert not failures,(name,failures)
            evidence.append(dict(root=name,files=len(manifest),unchanged=True))
        dump(OUT/'preservation.json',dict(baseline_before_sha256=digest(before),baseline_after_sha256=digest(after),baseline_unchanged=True,
             fresh_baseline_recomputation_matches=True,production_files_checked=len(old),production_changes=changes,sealed_prior_evidence=evidence,source_and_parsed_hashes_verified=True,
             automatic_adoption=False,automatic_registration=False))
        print('preserved',len(old),'production files','changes',changes,flush=True)
    finally:w.close()

def negative_controls():
    import runpy
    real=read('input.json');raw=read('execution-2.json')['output'];rows=[]
    base=validate(real,raw);assert base['errors']==['BOUNDARY_NUMERICAL_BACKTRACK'];dump(OUT/'raw-candidate-rejection.json',base)
    def check(name,b,o,expected,scope):
        v=validate(b,o);assert v['errors']==[expected],(name,v)
        rows.append(dict(name=name,scope=scope,expected_error=expected,actual=v))
    check('foreign_input',real,raw|dict(input_id='foreign'),'WRONG_INPUT_ID','real_candidate')
    bad=copy.deepcopy(raw);bad['edge_segment_ids'][0]=['fabricated'];check('foreign_native_id',real,bad,'FOREIGN_OR_DUPLICATE_SEGMENT','real_candidate')
    bad=copy.deepcopy(raw);bad['vertices'][0][0]+=30;check('displaced_edge',real,bad,'UNSUPPORTED_EDGE_OR_BRIDGE','real_candidate')
    errors={'hidden_boundary':'HIDDEN_BOUNDARY_SOURCE','unknown_units':'UNIT_EVIDENCE_INVALID','unsupported_xclip':'UNSUPPORTED_VISIBILITY','source_tamper':'INPUT_INTEGRITY_FAILED','target_outside':'TARGET_OUTSIDE','incomplete_source':'INCOMPLETE_SOURCE'}
    for name,error in errors.items():
        b=copy.deepcopy(real);o=copy.deepcopy(raw)
        if name=='hidden_boundary':next(s for s in b['segments'] if s['segment_id']==o['edge_segment_ids'][0][0])['visibility']['visible']=False
        elif name=='unknown_units':b['unit_evidence']['insunits']=0
        elif name=='unsupported_xclip':b['segments'][0]['visibility']['supported']=False
        elif name=='source_tamper':b['segments'][0]['a'][0]+=50
        elif name=='target_outside':
            b['target']['seed']=[b['bbox'][0]-1000,b['bbox'][1]-1000];next(t for t in b['texts'] if t['entity_id']==b['target']['label_entity_id'])['origin']=b['target']['seed']
        else:b['coverage']['blocking_diagnostics']=[dict(code='UNLOCATED_GEOMETRY')]
        if name!='source_tamper':b=seal(b);o['input_id']=b['input_id']
        check(name,b,o,error,'real_candidate_with_mutated_evidence')
    b,o=runpy.run_path(str(ROOT/'tests/test_room_boundary_pilot.py'))['fixture']()
    positive=validate(b,o);assert positive['validation_status']=='passed';dump(OUT/'validator-synthetic-positive.json',dict(input=b,output=o,validation=positive))
    for name,change,error in [('wrong_area',dict(area=o['area']+100),'AREA_MISMATCH'),('omitted_crossing',dict(excluded_crossing_ids=[]),'CROSSING_EXCLUSIONS_INCOMPLETE_OR_FALSE'),('axis_as_boundary',dict(edge_segment_ids=[['axis'],['i1'],['i2'],['i3']]),'NONSTRUCTURAL_BOUNDARY')]:check(name,b,o|change,error,'synthetic_valid_positive_control')
    dump(OUT/'negative-controls.json',rows);print('negatives',len(rows),flush=True)

def metamorphic():
    from cad_agent.extensions.adaptation.sandbox import execute
    state=read('run.json');a=next(a for a in state['attempts'] if a['number']==2);code=(OUT/a['code_path']).read_text()
    original=read('input.json');rows=[]
    for label,dx,dy,reverse in [('translated',1000000.,-2000000.,False),('shuffled_reversed',0.,0.,True)]:
        b=copy.deepcopy(original)
        def point(p):return [p[0]+dx,p[1]+dy]
        b['bbox']=[v+(dx if i%2==0 else dy) for i,v in enumerate(b['bbox'])];b['target']['seed']=point(b['target']['seed'])
        for t in b['texts']:t['origin']=point(t['origin']);t['bbox']=[v+(dx if i%2==0 else dy) for i,v in enumerate(t['bbox'])]
        for s in b['segments']:s['a']=point(s['a']);s['b']=point(s['b'])
        if reverse:
            b['segments'].reverse()
            for s in b['segments']:s['a'],s['b']=s['b'],s['a']
        b['test_derivation']=dict(synthetic_metamorphic=True,parent_input_id=original['input_id'],translation=[dx,dy],reversed=reverse)
        b=seal(b);dump(OUT/f'{label}-input.json',b)
        execution=execute(OUT/'data',b,code,timeout=20);v=validate(b,execution['output'])
        dump(OUT/f'{label}-execution.json',execution);dump(OUT/f'{label}-validation.json',v)
        rows.append(dict(name=label,synthetic_metamorphic=True,source_evidence_not_a_new_drawing=True,code_sha256=a['code_sha256'],validation_status=v['validation_status'],errors=v['errors'],candidate_area_native=execution['output'].get('area')))
    dump(OUT/'metamorphic-checks.json',rows);print('metamorphic',len(rows),flush=True)

def render():
    state=read('run.json');passed=[a for a in state['attempts'] if (OUT/f'execution-{a["number"]}.json').exists() and read(f'execution-{a["number"]}.json')['output'].get('status')=='candidate']
    if not passed:return
    a=passed[-1];o=read(f'execution-{a["number"]}.json')['output'];b=read('input.json');meta=read('render-provenance.json');ox,oy=meta['display_origin'];svg=(OUT/'source.svg').read_text()
    pts=' '.join(f'{x-ox},{oy-y}' for x,y in o['vertices'])
    contour=f'<polygon points="{pts}" fill="#008866" fill-opacity=".1" stroke="#008866" stroke-width="2.4" vector-effect="non-scaling-stroke"/>'
    overlay=svg.replace('</svg>',contour+'</svg>');(OUT/'candidate-overlay.svg').write_text(overlay);raster(overlay,OUT/'candidate-overlay.png')
    excluded=set(o['excluded_crossing_ids']);parts=[]
    for s in b['segments']:
        if s['segment_id'] in excluded:
            x,y=s['a'];xx,yy=s['b'];parts.append(f'<path d="M {x-ox} {oy-y} L {xx-ox} {oy-yy}" fill="none" stroke="#dd2777" stroke-width="1.5" vector-effect="non-scaling-stroke"/>')
    ex=svg.replace('</svg>',''.join(parts)+contour+'</svg>');(OUT/'crossing-exclusions.svg').write_text(ex);raster(ex,OUT/'crossing-exclusions.png')
    # Keep native geometry and layer visibility; crop display only for detailed review.
    import re
    xs=[p[0] for p in o['vertices']];ys=[p[1] for p in o['vertices']];pad=700
    view=f'{min(xs)-ox-pad} {oy-max(ys)-pad} {max(xs)-min(xs)+2*pad} {max(ys)-min(ys)+2*pad}'
    zoom=re.sub(r'viewBox="[^"]+"',f'viewBox="{view}"',overlay,count=1)
    (OUT/'candidate-detail.svg').write_text(zoom);raster(zoom,OUT/'candidate-detail.png')
    # Use existing rejected baseline geometry for side-by-side comparison, same source render.
    before=read('baseline-before.json')
    dump(OUT/'visual-review-pending.json',dict(source_sha256=b['source_sha256'],candidate_code_sha256=a['code_sha256'],reviewer='assistant',status='awaiting_actual_image_review',no_user_signoff_claim=True))

if __name__=='__main__':
    import sys
    for action in sys.argv[1:]:globals()[action]()
