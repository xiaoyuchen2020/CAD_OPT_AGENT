"""Opt-in local nested-face repair; candidate code executes only in pinned Docker."""
import argparse, copy, hashlib, json, math, random, shutil
from scripts import room_boundary_revision as base
from scripts import check_room_boundary_revision as checks

PREVIOUS = base.ROOT / 'artifacts/dynamic-room-support-repair-20261016'
OUT = base.ROOT / 'artifacts/dynamic-room-nested-boundary-20261016'
dump, sha, seal = base.dump, base.sha, checks.seal
PRIOR_TOKENS = 26435

def read(name):return json.loads((OUT/name).read_text())

def configure():
    base.OUT = checks.OUT = OUT

def history():
    rows=[]
    for name in ['dynamic-adaptation-20261016','dynamic-cross-drawing-20261016','dynamic-shadow-near-axis-20261016',
                 'dynamic-multimethod-20261016','dynamic-nontable-real-20261016','dynamic-room-boundary-20261016',
                 'dynamic-room-boundary-revision-20261016','dynamic-room-support-repair-20261016']:
        root=base.ROOT/'artifacts'/name;manifest=json.loads((root/'evidence-sha256.json').read_text());manifest=manifest.get('files',manifest)
        assert all(sha(root/p)==h for p,h in manifest.items())
        rows.append(dict(root=name,files=len(manifest),unchanged=True))
    return rows

def fixture():
    from tests.test_room_boundary_pilot import fixture as original
    return original()[0]

def rect(b, name, bounds):
    x,y,xx,yy=bounds; ps=[[x,y],[xx,y],[xx,yy],[x,yy]]
    for i,(a,c) in enumerate(zip(ps,ps[1:]+ps[:1])):
        b['segments'].append(dict(segment_id=f'{name}{i}',entity_path=[f'{name}{i}'],a=a,b=c,
            visibility=dict(effective_layer='WALL',visible=True,supported=True)))
    return ps

def cases():
    result=[]; points=[[0,0],[1000,0],[1000,2000],[0,2000]]
    def add(name,b,expected=True,polygon=None):
        b['test_derivation']=dict(kind='independent_synthetic_control',name=name)
        result.append(dict(name=name,bundle=seal(b),expected_pass=expected,expected_polygon=(polygon or points) if expected else None))
    add('nested_double',fixture())
    b=fixture();rect(b,'third',[-400,-400,1400,2400]);add('nested_triple',b)
    b=fixture();rect(b,'small_decoy',[1400,300,1500,400]);b['bbox']=[-600,-600,2000,2600];add('smaller_non_target_ring',b)
    b=fixture();sibling=rect(b,'sibling',[1600,0,2200,1000]);rect(b,'sibling_outer',[1400,-200,2400,1200]);b['bbox']=[-600,-600,2600,2600]
    add('disconnected_sibling',copy.deepcopy(b))
    b['target']['seed']=[1900,500];b['texts'][0]['origin']=[1900,500];add('target_in_sibling',b,polygon=sibling)
    b=fixture();rect(b,'reversed_duplicate',[0,0,1000,2000]);random.Random(61).shuffle(b['segments'])
    for s in b['segments']:s['a'],s['b']=s['b'],s['a']
    add('duplicate_reversed_nested',b)
    b=json.loads((PREVIOUS/'synthetic_overlap_spur-input.json').read_text());add('nested_overlap_spur',b)
    for name,angle,offset in [('translated_nested',0,[1000000,-2000000]),('oblique_nested',.538,[0,0])]:
        b=fixture();co,si=math.cos(angle),math.sin(angle)
        def p(q):return [q[0]*co-q[1]*si+offset[0],q[0]*si+q[1]*co+offset[1]]
        for s in b['segments']:s['a']=p(s['a']);s['b']=p(s['b'])
        b['target']['seed']=p(b['target']['seed']);b['texts'][0]['origin']=b['target']['seed']
        ps=[q for s in b['segments'] for q in [s['a'],s['b']]]
        b['bbox']=[min(q[0] for q in ps)-500,min(q[1] for q in ps)-500,max(q[0] for q in ps)+500,max(q[1] for q in ps)+500]
        add(name,b,polygon=[p(q) for q in points])
    b=fixture();b['target']['seed']=[-100,1000];b['texts'][0]['origin']=[-100,1000];add('seed_in_annulus',b,False)
    b=fixture();rect(b,'island',[100,100,300,300]);add('interior_hole',b,False)
    b=fixture();b['target']['seed']=[0,1000];b['texts'][0]['origin']=[0,1000];add('seed_on_boundary',b,False)
    b=fixture();b['target']['seed']=[1400,2400];b['texts'][0]['origin']=[1400,2400];add('seed_outside',b,False)
    b=fixture();b['segments']=[s for s in b['segments'] if s['segment_id']!='i0'];add('open_inner_ring',b,False)
    b=fixture();b['segments'][0]['visibility']['visible']=False;add('hidden_inner_edge',b,False)
    b=fixture();b['unit_evidence'].update(insunits=0,effective_unit='unknown',metres_per_unit=None);add('unknown_units',b,False)
    b=fixture();b['segments'][0]['visibility']['supported']=False;add('unsupported_visibility',b,False)
    b=fixture();b['segments'].append(dict(segment_id='partition',entity_path=['partition'],a=[600,0],b=[600,600],visibility=dict(effective_layer='WALL',visible=True,supported=True)))
    add('real_dangling_partition',b,False)
    b=fixture();rect(b,'narrow_island',[200,200,200.02,800]);add('finite_width_inner_hole',b,False)
    b=fixture();rect(b,'island',[100,100,300,300]);b['segments'].append(dict(segment_id='link',entity_path=['link'],a=[0,100],b=[100,100],visibility=dict(effective_layer='WALL',visible=True,supported=True)))
    add('connected_inner_hole',b,False)
    return result


def prepare():
    assert not OUT.exists()
    old=history();OUT.mkdir();(OUT/'data').mkdir()
    for n in ['input.json','source.svg','source.png','source-audit.json','render-provenance.json','baseline-overlay.svg','baseline-overlay.png','validator-frozen.py']:
        shutil.copy2(PREVIOUS/n,OUT/n)
    for n in ['adaptation-runtime.json','table-recovery-runtime.json']:shutil.copy2(PREVIOUS/'data'/n,OUT/'data'/n)
    dump(OUT/'history-before.json',old)
    dump(OUT/'scope.json',dict(user_request='继续修复嵌套闭环的目标边界选择问题',execution_mode='local_assistant_repair',max_calls=0,
        total_token_budget=96000,previous_tokens=PRIOR_TOKENS,outgoing='None',automatic_adoption=False,automatic_registration=False))
    dump(OUT/'cases.json',cases())
    dump(OUT/'production-before.json',{str(p.relative_to(base.ROOT)):sha(p) for folder in ['cad_agent','web'] for p in (base.ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts})
    w=base.workspace()
    try:base.source_check(w,read('input.json'));dump(OUT/'baseline-before-fresh.json',base.fresh_baseline(w,read('input.json')))
    finally:w.close()
    print('prepared',len(read('cases.json')),'synthetic cases;',sum(r['files'] for r in old),'historical files',flush=True)


def evaluate():
    from cad_agent.extensions.adaptation.sandbox import execute
    from shapely.geometry import Polygon
    state=read('run.json');a=state['attempts'][-1];assert a['status']=='generated'
    code=(OUT/a['code_path']).read_text();assert hashlib.sha256(code.encode()).hexdigest()==a['code_sha256']
    number=a['number'];v=base.validator();rows=[]
    b=read('input.json');ex=execute(OUT/'data',b,code,timeout=20);dump(OUT/f'execution-{number}.json',ex)
    validation=v.validate(b,ex['output']);dump(OUT/f'validation-{number}.json',validation)
    rows.append(dict(name='kongpu_real',expected_pass=True,status=validation['validation_status'],errors=validation['errors'],expectation_met=validation['validation_status']=='passed'))
    for case in read('cases.json'):
        b=case['bundle'];ex=execute(OUT/'data',b,code,timeout=20);r=v.validate(b,ex['output'])
        ok=(r['validation_status']=='passed')==case['expected_pass'];difference=None
        if case['expected_pass'] and r['validation_status']=='passed':
            p=Polygon(case['expected_polygon']);q=Polygon(ex['output']['vertices']);difference=p.symmetric_difference(q).area
            ok=ok and difference<1 and abs(p.area-ex['output']['area'])<1
        row=dict(name=case['name'],expected_pass=case['expected_pass'],status=r['validation_status'],errors=r['errors'],expectation_met=ok,reference_symmetric_difference_native=difference)
        rows.append(row);dump(OUT/f'{case["name"]}-attempt-{number}.json',dict(execution=ex,validation=r,assessment=row))
        print('CASE',case['name'],r['validation_status'],'OK' if ok else 'FAIL',r['errors'],flush=True)
    dump(OUT/f'evaluation-{number}.json',rows)
    a.update(status='completed',validation=validation)
    if all(r['expectation_met'] for r in rows):state.update(status='passed',accepted_attempt=number)
    else:state.update(status='needs_repair' if number<2 else 'closed')
    dump(OUT/'run.json',state)

def verify():
    checks.negatives();checks.replays();checks.numeric_diagnostics();checks.render();checks.preserve()
    p=read('preservation.json');p['prior_evidence']=history();dump(OUT/'preservation.json',p)
    a,o=checks.selected();old=json.loads((PREVIOUS/'execution-4.json').read_text())['output']
    from shapely.geometry import Polygon
    p,q=Polygon(o['vertices']),Polygon(old['vertices'])
    dump(OUT/'real-regression.json',dict(area_before=old['area'],area_after=o['area'],symmetric_difference_native=p.symmetric_difference(q).area,
        hausdorff_distance_native=p.boundary.hausdorff_distance(q.boundary),exclusions_equal=sorted(o['excluded_crossing_ids'])==sorted(old['excluded_crossing_ids']),
        output_byte_semantics_equal=base.digest(o)==base.digest(old),raw_output_not_modified=True))

def compare():
    from cad_agent.extensions.adaptation.sandbox import execute
    code=(PREVIOUS/'candidate-4.py').read_text();v=base.validator();rows=[]
    for c in read('cases.json'):
        if not c['expected_pass']:continue
        ex=execute(OUT/'data',c['bundle'],code,timeout=20);validation=v.validate(c['bundle'],ex['output'])
        dump(OUT/f'{c["name"]}-previous.json',dict(execution=ex,validation=validation,code_sha256=sha(PREVIOUS/'candidate-4.py')))
        rows.append(dict(name=c['name'],previous_status=validation['validation_status'],previous_errors=validation['errors']))
    dump(OUT/'previous-positive-comparison.json',rows)
    print('previous positive comparison',rows,flush=True)

if __name__=='__main__':
    configure()
    p=argparse.ArgumentParser();p.add_argument('actions',nargs='+',choices=['prepare','evaluate','verify','compare'])
    for action in p.parse_args().actions:globals()[action]()
