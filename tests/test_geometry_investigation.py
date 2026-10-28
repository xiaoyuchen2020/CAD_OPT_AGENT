import json
import math
from dataclasses import replace
from pathlib import Path
import ezdxf
import pytest
from cad_agent.geometry.investigation_topology import primitives,network
from cad_agent.geometry.investigation import (AnalyzeContext,TraceNetwork,MeasurePrimitives,ResolveBasis,EvidenceQuote,
    ReadNativeField,ReadArtifact,InvestigationDecision,analyze,trace,measure_primitives,basis,native_field,read_artifact,decision)
from cad_agent.geometry.workbench import GeometryWorkbench,PrepareBundle,ReadBundle
from cad_agent.geometry.experiment_protocol import freeze,load_frozen,score,compare_manifests,Guidance


def line(i,a,b):return dict(entity_id=i,kind='LINE',native=dict(start=[*a,0],end=[*b,0]),layout_id='l',status='ready',entity_path=[i],layer='0')


def test_trace_branches_gaps_and_independent_coverage():
    rows=[line('a',(0,0),(10,0)),line('b',(10,0),(20,0)),line('branch',(10,0),(10,10)),line('gap',(21,0),(30,0))]
    n=network(rows,.01,['a'],['a','b','branch','gap'])
    assert len(n['reachable_primitive_ids'])==3 and n['branches']
    assert n['coverage']['fraction']==pytest.approx(30/39)
    assert any(e['nearest_other_endpoint']['distance']==1 for e in n['open_endpoints'])
    assert not n['coordinates_modified'] and not n['global_minimum_proven']
    assert network(rows,.01,['a'])['coverage']['fraction'] is None


@pytest.mark.parametrize('bulge',[1,-1,.41421356237309503,-.41421356237309503])
def test_polyline_bulge_preserves_exact_arc_and_source_index(bulge):
    r=dict(entity_id='poly',entity_path=['block','handle'],layer='wall',kind='LWPOLYLINE',layout_id='l',status='ready',native=dict(vertices=[[0,0,0,0,bulge],[10,0,0,0,0],[20,0,0,0,0]],closed=False))
    edges,issues=primitives([r]);assert not issues
    assert [e['kind'] for e in edges]==['ARC','LINE']
    assert edges[0]['source_segment_index']==0 and edges[1]['source_entity_id']=='poly'
    assert sorted(tuple(round(v,8) for v in p[:2]) for p in edges[0]['endpoints'])==[(0.,0.),(10.,0.)]
    assert edges[0]['native']['radius']==pytest.approx(10*(1+bulge*bulge)/(4*abs(bulge)))


def test_no_transitive_endpoint_snapping():
    rows=[line('a',(0,0),(10,0)),line('b',(10.09,0),(20,0)),line('c',(10.18,0),(30,0))]
    n=network(rows,.1)
    ids={p['source_entity_id']:p['entity_id'] for p in n['primitives']}
    assert not any({c['a'][0],c['b'][0]}=={ids['a'],ids['c']} for c in n['connections'])


def fixture_drawing(workspace,tmp_path):
    doc=ezdxf.new();doc.units=0;m=doc.modelspace()
    m.add_lwpolyline([(0,0),(10,0),(20,0)])
    m.add_lwpolyline([(0,4),(10,4),(20,4)])
    m.add_text('A plan',dxfattribs={'insert':(0,8),'height':1})
    m.add_text('B section',dxfattribs={'insert':(22,8),'height':1})
    m.add_text('dimensions in mm',dxfattribs={'insert':(0,11),'height':1})
    p=tmp_path/'input.dxf';doc.saveas(p);d=workspace.ingest('input.dxf',p.read_bytes())
    service=GeometryWorkbench(workspace,d['snapshot_id'])
    b=service.prepare(PrepareBundle(layout_id=d['layouts'][0]['layout_id'],bbox=[-1,-1,40,15]))
    rows=service.read(ReadBundle(bundle_id=b['bundle_id'],kind='LWPOLYLINE'))['items']
    return service,b,rows


def test_context_topology_measure_and_paged_evidence(workspace,tmp_path):
    service,b,rows=fixture_drawing(workspace,tmp_path);bid=b['bundle_id'];ids=[r['entity_id'] for r in rows]
    c=analyze(service,AnalyzeContext(bundle_id=bid,entity_ids=ids))
    assert len(c['label_candidates'])>=2
    assert c['ownership']['state']=='competing_candidates'
    assert all(f['role_status']=='unconfirmed' for f in c['entity_features'])
    n=trace(service,TraceNetwork(bundle_id=bid,entity_ids=ids,seed_entity_ids=ids[:1],census_entity_ids=ids,tolerance_drawing_units=.001))
    assert n['primitive_count']==4 and n['coverage']['fraction']==.5
    page=read_artifact(service,ReadArtifact(bundle_id=bid,collection='geometry_network',reference_id=n['network_id'],field='primitives',limit=1))
    assert page['has_more'] and page['next_offset']==1
    full=service.w.store.get('geometry_network',n['network_id'])
    pp=[p['entity_id'] for p in full['primitives'] if p['source_segment_index']==0]
    result=measure_primitives(service,MeasurePrimitives(bundle_id=bid,network_id=n['network_id'],primitive_ids=pp))
    assert result['measurements']['width_drawing_units']==4
    assert set(result['source_entity_ids'])==set(ids)
    assert all(r['segment_index']==0 for r in result['source_segments'])
    proof=basis(service,ResolveBasis(bundle_id=bid,measurement_id=result['measurement_id'],target_unit='mm'))
    assert proof['conversion']['status']=='blocked'
    with pytest.raises(ValueError,match='BASIS_QUOTE'):
        label=c['label_candidates'][0]
        basis(service,ResolveBasis(bundle_id=bid,evidence=[EvidenceQuote(region_id=label['region_id'],quote='not in original',role='unit')]))
    excluded=decision(service,InvestigationDecision(bundle_id=bid,entity_ids=ids,reference_ids=[c['context_analysis_id']],decision='exclude_candidate',reason='counterevidence'))
    assert excluded['interpretation_author']=='model'
    with pytest.raises(ValueError,match='NOT_FOUND'):
        decision(service,InvestigationDecision(bundle_id=bid,reference_ids=['invented'],decision='retain_candidate',reason='x'))


def test_native_continuation_is_lossless(workspace,tmp_path):
    d=ezdxf.new();d.modelspace().add_lwpolyline([(i,i%3) for i in range(150)])
    p=tmp_path/'large.dxf';d.saveas(p);data=workspace.ingest('large.dxf',p.read_bytes());service=GeometryWorkbench(workspace,data['snapshot_id'])
    b=service.prepare(PrepareBundle(layout_id=data['layouts'][0]['layout_id'],bbox=[-1,-1,160,10]))
    r=service.read(ReadBundle(bundle_id=b['bundle_id']))['items'][0]
    assert r['native_truncated'] and r['continuation']
    pages=[native_field(service,ReadNativeField(bundle_id=b['bundle_id'],entity_id=r['entity_id'],field='vertices',offset=i,limit=64)) for i in (0,64,128)]
    assert sum(len(p['items']) for p in pages)==150 and not pages[-1]['has_more']
    summary=service.read(ReadBundle(bundle_id=b['bundle_id'],view='summary'))['items'][0]
    assert 'native' not in summary and 'vertices' in summary['native_fields']


def test_progress_repeated_calls_exclusion_and_public_pipeline(workspace,tmp_path):
    from test_remediation_scope import session,invoke
    service,b,rows=fixture_drawing(workspace,tmp_path)
    s=session(workspace,service.drawing);bid=b['bundle_id'];ids=[r['entity_id'] for r in rows]
    args=dict(bundle_id=bid,entity_ids=ids,task_ids=['q1'])
    c=invoke(s,'analyze_geometry_context',args);assert 'error' not in c,c
    for _ in range(3):assert 'error' not in invoke(s,'analyze_geometry_context',args)
    p=invoke(s,'plan_geometry_investigation',dict(bundle_id=bid,task_ids=['q1']))
    assert p['progress']['new_source_or_geometry_events']==1
    assert p['progress']['consecutive_without_new_evidence']>=3
    assert p['recommendation']=='change_strategy'
    n=invoke(s,'trace_geometry_network',args|dict(seed_entity_ids=ids[:1],census_entity_ids=ids,tolerance_drawing_units=.001))
    assert 'error' not in n,n
    stored=workspace.store.get('geometry_network',n['network_id'])
    pp=[r['entity_id'] for r in stored['primitives'] if r['source_segment_index']==0]
    measured=invoke(s,'measure_geometry_primitives',dict(bundle_id=bid,network_id=n['network_id'],primitive_ids=pp,task_ids=['q1']))
    assert 'error' not in measured,measured
    assert measured['observation_id'] in s.observations
    assert not measured['operation_receipts'][0]['fulfilled']
    delta=invoke(s,'read_geometry_workset',dict(since_revision=4,task_ids=['q1']))
    assert all(e['revision']>4 for e in delta['events'])
    checked=invoke(s,'validate_geometry_claims',dict(bundle_id=bid,claims=[dict(claim_id='bad',reference_id=measured['measurement_id'],kind='whole_route_minimum',value=4)],task_ids=['q1']))
    assert checked['claims'][0]['repair_actions'][0]['tool']=='plan_geometry_investigation'


def test_gold_scoring_missing_objects_and_assistance(tmp_path):
    case=dict(case_id='one',source_sha256='a'*64,split='heldout',question='identify',scope={'bbox':[0,0,10,10]},annotation_basis='independent review',census_complete=True,
        objects=[dict(object_key='ramp A',view_kind='plan',source_paths=[['a'],['b']],measurements=[dict(source_paths=[['a'],['b']],value=4,absolute_tolerance=.01)],route_segment_keys=['1','2'])])
    p=tmp_path/'gold.json';f=freeze([case],p);assert load_frozen(p)==f
    pred=dict(source_sha256='a'*64,objects=[dict(object_key='ramp A',view_kind='section',source_paths=[['a'],['b']],measurements=[dict(source_paths=[['a'],['b']],value=4,unit='drawing_unit')],covered_segment_keys=['1'],whole_route_complete=True)])
    result=score(case,pred,[dict(category='direct_selection',text='use a b',object_keys=['ramp A'])])
    assert result['metrics']['numerical_precision']['rate']==1
    assert result['metrics']['attribution']['rate']==0 and result['metrics']['route_coverage']['rate']==.5
    assert result['metrics']['claim_scope']['rate']==0 and result['unassisted_matched_labels']==0
    missing=score(case,dict(source_sha256='a'*64,objects=[]))
    assert missing['metrics']['numerical_precision']['rate']==0
    with pytest.raises(FileExistsError):freeze([case],p)
    with pytest.raises(ValueError,match='LEAKAGE'):freeze([case,case|dict(case_id='two',split='development')],tmp_path/'bad.json')
    assert not compare_manifests({'model':'a','validation_mode':'shadow'},{'model':'b','validation_mode':'enforce'},'validation_mode')['comparable']
    assert not compare_manifests({'model':'a','validation_mode':'shadow'},{'model':'a','validation_mode':'enforce'},'validation_mode')['comparable']
    manifest=dict(model='a',reasoning_effort='high',source_sha256='a'*64,rule_sha256='b'*64,question_sha256='c'*64,code_files={'a.py':'d'*64},runner_sha256='e'*64,tool_names=['x'],runtime='test',docker_image=None,code_execution_mode='disabled',budget_profile={'max_rounds':None,'max_input_tokens':None},geometry_context_projection=True,validation_mode='shadow')
    assert compare_manifests(manifest,manifest|{'validation_mode':'enforce'},'validation_mode')['comparable']


def test_sdk_contract_and_failures(tmp_path):
    from cad_agent.geometry.sandbox_sdk import capabilities,write_candidate,classify_failure
    assert not capabilities()['network']
    assert classify_failure('NameError: true',1)['retry']=='after_repair_only'
    out=write_candidate('result.json',{'value':3},'a'*64,'b'*64,str(tmp_path))
    assert out['status']=='sandbox_candidate_requires_host_replay'
    with pytest.raises(ValueError):write_candidate('../bad.json',{},'a'*64,'b'*64,str(tmp_path))
    with pytest.raises(FileExistsError):write_candidate('result.json',{},'a'*64,'b'*64,str(tmp_path))


def test_unit_conversion_and_claim_repairs(workspace,tmp_path):
    from cad_agent.geometry.workbench import MeasurePair
    from cad_agent.geometry.workbench_checks import CheckClaims,GeometryClaim,validate
    from cad_agent.analysis.spatial_delivery import spatial_measurement_issues,geometry_extent_issues
    from types import SimpleNamespace
    doc=ezdxf.new();doc.units=4;m=doc.modelspace();m.add_line((0,0),(10000,0));m.add_line((0,4000),(10000,4000))
    p=tmp_path/'units.dxf';doc.saveas(p);drawing=workspace.ingest('units.dxf',p.read_bytes());service=GeometryWorkbench(workspace,drawing['snapshot_id'])
    b=service.prepare(PrepareBundle(layout_id=drawing['layouts'][0]['layout_id'],bbox=[-1,-1,10001,4001]));bid=b['bundle_id']
    ids=[r['entity_id'] for r in service.read(ReadBundle(bundle_id=bid,kind='LINE'))['items']]
    local=service.measure(MeasurePair(bundle_id=bid,entity_ids=ids,basis='parallel_normal'))
    converted=basis(service,ResolveBasis(bundle_id=bid,measurement_id=local['measurement_id'],target_unit='m'))
    assert converted['conversion']['value']==4
    checked=validate(service,CheckClaims(bundle_id=bid,claims=[GeometryClaim(claim_id='x',reference_id=converted['basis_id'],kind='converted_local_distance',value=4,unit='m')]))
    assert checked['claims'][0]['supported']
    assert not spatial_measurement_issues(SimpleNamespace(text='局部候选间距4米'),[converted])
    assert not spatial_measurement_issues(SimpleNamespace(text='局部候选间距4米'),[checked])
    assert geometry_extent_issues(SimpleNamespace(text='全程最小净宽为4米'),converted)
    wrong=validate(service,CheckClaims(bundle_id=bid,claims=[GeometryClaim(claim_id='x',reference_id=converted['basis_id'],kind='converted_local_distance',value=4,unit='drawing_unit')]))
    assert not wrong['claims'][0]['supported']


def test_source_and_scope_mismatches_fail_without_erasing_local_results(workspace,tmp_path):
    service,b,rows=fixture_drawing(workspace,tmp_path)
    with pytest.raises(ValueError,match='CENSUS'):
        trace(service,TraceNetwork(bundle_id=b['bundle_id'],entity_ids=[rows[0]['entity_id']],census_entity_ids=['fake'],tolerance_drawing_units=1))
    with pytest.raises(ValueError,match='OUTSIDE_BUNDLE'):
        analyze(service,AnalyzeContext(bundle_id=b['bundle_id'],object_bbox=[-100,-100,100,100]))
    source=workspace.store.artifact(service.sid,'source.dxf');source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='SOURCE_CHANGED'):
        basis(service,ResolveBasis(bundle_id=b['bundle_id']))


def test_reflected_ocs_arc_and_polyline_preserve_world_geometry():
    arc=dict(entity_id='a',kind='ARC',layout_id='l',status='ready',native=dict(center=[-10,20,3],radius=5,start_angle=0,end_angle=90,extrusion=[0,0,-1]))
    rows,diag=primitives([arc]);assert not diag
    assert rows[0]['native']['center']==[10,20,-3]
    assert rows[0]['length_drawing_units']==pytest.approx(math.pi*5/2)
    assert sorted(tuple(round(v,8) for v in p) for p in rows[0]['endpoints'])==[(5,20,-3),(10,25,-3)]
    poly=dict(entity_id='p',kind='LWPOLYLINE',layout_id='l',status='ready',native=dict(vertices=[[0,0,0,0,1],[10,0,0,0,0]],closed=False,elevation=2,extrusion=[0,0,-1]))
    edges,diag=primitives([poly]);assert not diag
    assert sorted(tuple(round(v,8) for v in p) for p in edges[0]['endpoints'])==[(-10,0,-2),(0,0,-2)]
    assert edges[0]['length_drawing_units']==pytest.approx(5*math.pi)


def test_network_route_preserves_source_segments_and_gaps(workspace,tmp_path):
    from cad_agent.geometry.investigation import MeasureNetworkRoute,PrimitivePair,network_route
    from cad_agent.geometry.workbench_routes import hypothesis,Hypothesis
    service,b,rows=fixture_drawing(workspace,tmp_path);bid=b['bundle_id'];ids=[r['entity_id'] for r in rows]
    n=trace(service,TraceNetwork(bundle_id=bid,entity_ids=ids,seed_entity_ids=ids[:1],census_entity_ids=ids,tolerance_drawing_units=.001))
    net=workspace.store.get('geometry_network',n['network_id']);first=[p['entity_id'] for p in net['primitives'] if p['source_segment_index']==0]
    h=hypothesis(service,Hypothesis(bundle_id=bid,entity_ids=ids,interpretation='two boundary candidates'))
    route=network_route(service,MeasureNetworkRoute(bundle_id=bid,network_id=n['network_id'],hypothesis_id=h['hypothesis_id'],pairs=[PrimitivePair(primitive_ids=first)]))
    assert route['segments'][0]['sample_minimum_drawing_units']==4
    assert len(route['unpaired_declared_primitive_ids'])==2
    assert route['measurement_coverage']['length_fraction'] is None
    assert not route['global_minimum_proven']


@pytest.mark.parametrize('scale,mirror,offset',[(1,1,0),(1000,-1,1e7),(.01,1,-1e6)])
def test_renaming_and_coordinate_changes_do_not_select_expected_width(scale,mirror,offset):
    from cad_agent.geometry.local_measurement import measure_pair
    def p(x,y):return (offset+mirror*scale*x,offset+scale*y)
    records=[line('arbitrary-renamed-one',p(0,0),p(10,0)),line('unrelated-new-id',p(0,4),p(10,4))]
    for r in records:r['layer']='renamed-without-semantics'
    edges,diag=primitives(records);assert not diag
    result=measure_pair(*edges,'parallel_normal')
    assert result['measurements']['width_drawing_units']==pytest.approx(4*scale,rel=1e-7)
    assert result['identity_status']=='candidate'


def test_empty_hypothesis_plans_geometry_binding_not_invalid_trace():
    from cad_agent.geometry.investigation_progress import plan
    h=dict(hypothesis_id='h',bundle_id='b',request=dict(entity_ids=[],label_region_ids=['r']))
    result=plan(dict(hypothesis_ids={'h':h}),'b','h')
    assert any(c['gap']=='geometry_binding' for c in result['next_calls'])
    assert not any(c['tool']=='trace_geometry_network' for c in result['next_calls'])


def test_endpoint_search_matches_bruteforce_without_optional_scipy():
    import random
    from cad_agent.geometry.investigation_topology import endpoint_tree,nearest_other
    random.seed(42)
    rows=[(i//2,i%2,[random.uniform(-100,100) for _ in range(3)]) for i in range(80)]
    tree=endpoint_tree(rows)
    for owner,_,p in rows:
        expected=min((math.dist(p,q),j,k) for j,k,q in rows if j!=owner)
        assert nearest_other(tree,p,owner)==expected


def test_investigation_export_retains_new_evidence_collections(workspace,tmp_path):
    from cad_agent.geometry.workbench_routes import export,ExportResults
    service,b,rows=fixture_drawing(workspace,tmp_path);bid=b['bundle_id']
    ctx=analyze(service,AnalyzeContext(bundle_id=bid))
    physical=basis(service,ResolveBasis(bundle_id=bid))
    out=export(service,ExportResults(bundle_id=bid,context_analysis_ids=[ctx['context_analysis_id']],basis_ids=[physical['basis_id']]))
    stored=workspace.store.get('geometry_resultset',out['result_id'])
    assert stored['context_analyses'][0]['context_analysis_id']==ctx['context_analysis_id']
    assert stored['bases'][0]['unit_resolution']['state']=='unknown'


def test_seed_component_counts_cannot_be_read_as_all_selected_endpoints():
    n=network([line('a',(0,0),(10,0)),line('b',(0,4),(10,4))],.001,['a'])
    assert n['traversal_scope']=='seed_reachable_component'
    assert n['endpoint_counts']==dict(input_endpoints=4,traversed_endpoints=2,open_traversed_endpoints=2,open_input_endpoints=4)


def test_scoring_requires_correct_polyline_segment_indices():
    case=dict(case_id='poly',source_sha256='a'*64,split='development',question='measure',scope={'layout':'l'},annotation_basis='synthetic exact segments',objects=[dict(object_key='pair',measurements=[dict(source_paths=[['poly'],['poly']],segment_indices=[0,2],value=4,absolute_tolerance=0)])])
    p=dict(source_sha256='a'*64,objects=[dict(object_key='pair',measurements=[dict(source_paths=[['poly'],['poly']],segment_indices=[0,1],value=4,unit='drawing_unit')])])
    assert score(case,p)['metrics']['numerical_precision']['rate']==0
    p['objects'][0]['measurements'][0]['segment_indices']=[0,2]
    assert score(case,p)['metrics']['numerical_precision']['rate']==1
