import json
from types import SimpleNamespace
import pytest
from cad_agent.geometry.workbench_checks import GeometryClaim,check_claim
from cad_agent.analysis.geometry_context import project_geometry_messages


def measurement():
    return dict(status='local_pair_measured',measurements={'width_drawing_units':4},unit={'unit':'unknown'})


@pytest.mark.parametrize('kind,value,unit,code',[
    ('local_distance',4,'drawing_unit',None),
    ('local_distance',4,'mm','UNIT_EVIDENCE_REQUIRED'),
    ('local_distance',5,'drawing_unit','VALUE_MISMATCH'),
    ('whole_route_minimum',4,'drawing_unit','WHOLE_ROUTE_PROOF_REQUIRED'),
    ('finished_width',4,'drawing_unit','FINISHED_SURFACE_REQUIRED'),
    ('confirmed_object_identity',None,'drawing_unit','OBJECT_IDENTITY_UNCONFIRMED'),
])
def test_each_claim_separately(kind,value,unit,code):
    r=check_claim(GeometryClaim(claim_id='a',reference_id='r',kind=kind,value=value,unit=unit),measurement())
    assert r['supported']==(code is None)
    if code:assert code in [i['check_id'] for i in r['issues']]


def test_sampling_not_global_minimum():
    r=dict(segments=[{'sample_minimum_drawing_units':8},{'sample_minimum_drawing_units':5}],unit={'unit':'mm'})
    claim=GeometryClaim(claim_id='s',reference_id='r',kind='sampled_boundary_minimum',value=5,unit='mm')
    assert check_claim(claim,r)['supported']
    assert not check_claim(claim.model_copy(update={'kind':'whole_route_minimum'}),r)['supported']


def test_context_roundtrip_and_exact_only_dedup():
    items=[dict(entity_id='a',native={'start':[1,2,0]},status='ready'),dict(entity_id='b',native={'start':None})]
    data=dict(bundle_id='b',items=items,has_more=True,next_offset=2,unit='unknown')
    def msg(call,d):return dict(role='tool',tool_call_id=call,content=json.dumps(d))
    original=[msg('one',data),msg('two',data),msg('three',data|{'bundle_id':'other'})]
    output,stats=project_geometry_messages(original)
    assert stats['tables']==2 and stats['duplicate_pages']==1
    first=json.loads(output[0]['content']);table=first['entity_table']
    restored=[table['common_fields']|{k:v for k,v in zip(table['columns'],row) if k not in missing} for row,missing in zip(table['rows'],table['missing_fields'])]
    assert restored==items and first['has_more'] and first['next_offset']==2
    assert json.loads(output[1]['content'])['geometry_page_reference']=='one'
    assert json.loads(original[0]['content'])==data
    # Failure/status/page changes must be preserved independently.
    changed=[msg('a',data),msg('b',data|{'has_more':False})]
    assert project_geometry_messages(changed)[1]['duplicate_pages']==0
    wire=[dict(type='function_call_output',call_id=m['tool_call_id'],output=m['content']) for m in original]
    projected,stats=project_geometry_messages(wire)
    assert stats['duplicate_pages']==1 and json.loads(projected[1]['output'])['geometry_page_reference']=='one'


def test_shadow_enforce_share_checks_and_keep_local_results(workspace,tmp_path):
    import ezdxf
    from dataclasses import replace
    from cad_agent.geometry.workbench import GeometryWorkbench,PrepareBundle,ReadBundle,MeasurePair
    from cad_agent.geometry.workbench_checks import CheckClaims,validate
    d=ezdxf.new();d.units=0;m=d.modelspace();m.add_line((0,0),(10,0));m.add_line((0,4),(10,4))
    p=tmp_path/'checks.dxf';d.saveas(p);drawing=workspace.ingest('checks.dxf',p.read_bytes());s=GeometryWorkbench(workspace,drawing['snapshot_id'])
    b=s.prepare(PrepareBundle(layout_id=drawing['layouts'][0]['layout_id'],bbox=[-1,-1,11,6]))
    ids=[r['entity_id'] for r in s.read(ReadBundle(bundle_id=b['bundle_id']))['items']]
    r=s.measure(MeasurePair(bundle_id=b['bundle_id'],entity_ids=ids,basis='parallel_normal'))
    local=GeometryClaim(claim_id='local',reference_id=r['measurement_id'],kind='local_distance',value=4)
    invalid=local.model_copy(update={'claim_id':'global','kind':'whole_route_minimum'})
    args=CheckClaims(bundle_id=b['bundle_id'],claims=[local,invalid])
    shadow=validate(s,args)
    workspace.settings=replace(workspace.settings,geometry_validation_mode='enforce')
    enforced=validate(s,args)
    assert shadow['supported_claim_ids']==enforced['supported_claim_ids']==['local']
    assert shadow['blocked_claim_ids']==[] and enforced['blocked_claim_ids']==['global']
    assert shadow['claims'][1]['issues']==enforced['claims'][1]['issues']
    assert workspace.store.get('local_measurement',r['measurement_id'])==r
    assert s.read(ReadBundle(bundle_id=b['bundle_id'],kind='COLUMN'))['diagnostic']=='NATIVE_KIND_NOT_PRESENT'
    from cad_agent.geometry.workbench_routes import ExportResults,export
    out=export(s,ExportResults(bundle_id=b['bundle_id'],measurement_ids=[r['measurement_id']],validation_ids=[enforced['validation_id']]))
    from cad_agent.app import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(workspace.settings)) as client:
        response=client.get(out['download_url'])
        assert response.status_code==200 and response.json()['validations'][0]['blocked_claim_ids']==['global']
        assert 'attachment' in response.headers['content-disposition']
        assert client.get(out['download_url'].replace(drawing['snapshot_id'],'foreign')).status_code==404


def test_extent_guard_preserves_negative_scope_explanations():
    from cad_agent.analysis.spatial_delivery import geometry_extent_issues
    data=dict(bundle_id='b',route_id='r')
    assert geometry_extent_issues(SimpleNamespace(text='全程最小净宽为4。'),data)
    assert not geometry_extent_issues(SimpleNamespace(text='局部间距4原图单位，不能证明全程最小净宽。'),data)
    assert geometry_extent_issues(SimpleNamespace(text='单位未知，全程最小净宽为4。'),data)
    from cad_agent.analysis.spatial_delivery import spatial_numeric_evidence,spatial_measurement_issues
    data.update(unit={'unit':'unknown'},segments=[{'sample_minimum_drawing_units':4,'sample_count':99}])
    assert spatial_numeric_evidence(data)==[4]
    assert spatial_measurement_issues(SimpleNamespace(text='局部采样最小值4毫米'),[data])
    assert not spatial_measurement_issues(SimpleNamespace(text='局部采样最小值4原图单位'),[data])


def test_nonfinite_arcs_and_angular_dimension_not_accepted():
    from test_geometry_workbench import arc
    from cad_agent.geometry.local_measurement import measure_pair
    from cad_agent.geometry.candidate_routes import candidates
    r=measure_pair(arc('a',5,0,90),arc('b',float('nan'),0,90),'concentric_radial')
    assert r['status']=='unsupported' and r['diagnostics'][0]['code']=='NONFINITE_ARC'
    with pytest.raises(ValueError,match='LINEAR_DIMENSION'):
        candidates([],'LINE',dict(kind='DIMENSION',native=dict(dimtype=2,defpoint2=[0,0],defpoint3=[1,1])))


def test_cross_view_reuse_is_advisory_and_superseded_hypothesis_not_active():
    from cad_agent.geometry.workbench_checks import view_conflicts
    def h(hid,kind):return dict(hypothesis_id=hid,bundle_id='b',request=dict(view_kind=kind,entity_ids=['edge']))
    workset=dict(hypothesis_ids={'plan':h('plan','plan'),'section':h('section','section')})
    issues=view_conflicts(workset)
    assert issues[0]['disposition']=='review_mapping_not_block_geometry'
    workset['superseded_hypotheses']={'section':['new_section']}
    assert not view_conflicts(workset)
    from cad_agent.analysis.spatial_delivery import is_spatial_fact,spatial_claim_issues
    legacy=dict(hypothesis_id='h',semantic_identity='model_asserted_not_confirmed')
    assert is_spatial_fact(legacy)
    assert spatial_claim_issues(SimpleNamespace(text='已确认房间净宽',state='observed'),legacy)


def test_dimension_and_shared_diagnostics_roundtrip_preserves_all_fields():
    from cad_agent.analysis.geometry_context import restore_geometry_messages
    def msg(call,data):return dict(role='tool',tool_call_id=call,content=json.dumps(data))
    data=dict(kind='dimensions',record_id='r',source_sha256='a',items=[dict(id='one',value=7,unit='unknown'),dict(id='two',value=None,unit='unknown')],
        diagnostics=[dict(code='unlocated',entity_id='e'+str(i)) for i in range(60)],coverage={'complete':False},has_more=True,next_offset=2)
    messages=[msg('one',data|{'observation_id':'o1'}),msg('two',data|{'record_id':'r2','observation_id':'o2'}),msg('three',data|{'observation_id':'o3'})]
    projected,stats=project_geometry_messages(messages)
    assert stats['shared_fields']==1 and stats['duplicate_pages']==1
    assert [json.loads(m['content']) for m in restore_geometry_messages(projected)]==[json.loads(m['content']) for m in messages]
    assert stats['characters_saved']>0
