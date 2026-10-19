from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from cad_agent.analysis.result_sets import analyze, AnalyzeMeasurements, restore_previous
from cad_agent.analysis.grounding import verify_calculations
from cad_agent.analysis.session import Claim
from cad_agent.analysis.operations import infer_operations, receipts
from cad_agent.components.dimension_chain import check_chain, DimensionChainQuery
from test_components import ingest


def result_session():
    items=[]
    for n,area in [(4,6),(2,8),(1,8)]:
        x=n*20
        items.append(dict(number=n,region_id=f'r{n}',highlight=dict(kind='candidate_boundary',boundary=[[x,0],[x+area,0],[x+area,1],[x,1]])))
    return SimpleNamespace(request=SimpleNamespace(snapshot_id='s'),drawing={'sha256':'sha'},
        task=SimpleNamespace(data={}),room_batches={'b':dict(batch_id='b',source_sha256='sha',unit='m',items=items,total=3,scope={'layout_id':'l'})})


def test_sort_range_and_replace_use_polygon_values_with_stable_ties():
    s=result_session();r=analyze(s,AnalyzeMeasurements(batch_id='b',operation='range',limit=2,replace_highlights=True))
    assert r['calculation']['value']==2
    assert [v['number'] for v in r['ordered']]==[1,2]
    assert {i['number'] for i in s.highlight_action['items']}=={1,2}
    assert [i['number'] for i in s.highlight_action['items']]==[1,2]
    assert len(s.room_batches['b']['items'])==3
    assert all(v['basis']=='selected_enclosure' for v in r['values'])
    assert {r['operation'] for r in r['operation_receipts']}=={'area','arithmetic','ranking','selection','highlight'}
    analyze(s,AnalyzeMeasurements(batch_id='b',operation='rank',descending=False,replace_highlights=True))
    assert [i['number'] for i in s.highlight_action['items']]==[4,1,2]


def test_perimeter_is_exterior_and_sum_rejects_overlaps_and_missing():
    s=result_session();r=analyze(s,AnalyzeMeasurements(batch_id='b',metric='perimeter'))
    assert sorted(v['value'] for v in r['values'])==[14,18,18]
    s.room_batches['b']['items'][0]['highlight']=deepcopy(s.room_batches['b']['items'][1]['highlight'])
    with pytest.raises(ValueError,match='重叠'):analyze(s,AnalyzeMeasurements(batch_id='b',operation='sum'))
    s.room_batches['b']['items'][0]['highlight']['kind']='label'
    with pytest.raises(ValueError,match='未测量'):analyze(s,AnalyzeMeasurements(batch_id='b',operation='range'))


@pytest.mark.parametrize('fault',['foreign_batch','stale','unit'])
def test_result_collection_checks_source_and_units(fault):
    s=result_session();bid='b'
    if fault=='foreign_batch':bid='evil'
    if fault=='stale':s.room_batches['b']['source_sha256']='changed'
    if fault=='unit':s.room_batches['b']['unit']='unknown'
    with pytest.raises(ValueError):analyze(s,AnalyzeMeasurements(batch_id=bid))


def calc_claim(r):
    a,b=r['values'][0],r['values'][1]
    return Claim(text='差值为-2平方米，仍为围合候选口径。',state='derived',evidence_basis='tool_observation',observation_ids=['o'],calculations=[dict(operation='subtract',operands=[dict(value=str(v['value']),observation_id='o',measurement_ref=v['measurement_ref']) for v in (a,b)],result='-2',decimal_places=0)])


def test_measured_arithmetic_is_typed_referenced_and_recomputed():
    r=analyze(result_session(),AnalyzeMeasurements(batch_id='b'));c=calc_claim(r)
    obs={'o':{'tool':'analyze_measurements','data':r}}
    assert verify_calculations(c,{},obs)=={'-2'}
    for field,value in [('measurement_ref','invented'),('value','100')]:
        bad=c.model_copy(deep=True);setattr(bad.calculations[0].operands[0],field,value)
        with pytest.raises(ValueError):verify_calculations(bad,{},obs)
    obs['o']['data']['values'][0]['unit']='m'
    with pytest.raises(ValueError,match='单位'):verify_calculations(c,{},obs)


def test_atomic_capabilities_are_not_satisfied_by_label_batch():
    ops=infer_operations('求围合周长、平均面积以及最大和最小面积差，只显示前两处。')
    assert {'perimeter','arithmetic','selection','ranking'}<=set(ops)
    assert receipts('analyze_room_batch',{'items':[{'candidate_area_m2':25}]})==[]
    assert receipts('plan_task',{'operation_receipts':[{'operation':'perimeter','fulfilled':True}]})==[]


def chain(doc):
    m=doc.modelspace()
    for start,end,y,text in [(0,2000,-500,'<>'),(2000,5000,-500,'<>'),(0,5000,-1000,'5100')]:
        m.add_linear_dim(base=(0,y),p1=(start,0),p2=(end,0),text=text).render()


def test_native_chain_checks_real_endpoints_and_detects_override_discrepancy(workspace):
    _,d,lid=ingest(workspace,chain)
    r=check_chain(workspace,d['id'],DimensionChainQuery(layout_id=lid,unit_override='mm'))
    assert r['sum_value']==5000 and r['difference']==-100
    assert r['geometric_sum']==r['geometric_total']==5000
    assert len(r['parts'])==2 and len(r['highlight_entity_ids'])==3
    assert r['total']['rendered_texts']==['5100']
    assert r['display_arithmetic']['sum']=='5000' and r['display_arithmetic']['difference']=='-100'


def test_noncontinuous_and_duplicate_segments_are_not_summed(workspace):
    def build(doc):
        m=doc.modelspace()
        for a,b,y in [(0,2000,-500),(2500,5000,-500),(0,5000,-1000)]:m.add_linear_dim(base=(0,y),p1=(a,0),p2=(b,0)).render()
    _,d,lid=ingest(workspace,build)
    r=check_chain(workspace,d['id'],DimensionChainQuery(layout_id=lid))
    assert r['status']=='unresolved' and 'difference' not in r


def test_parent_restoration_reloads_objects_and_checks_source_version(workspace):
    from test_room_batch import room_fixture
    from cad_agent.geometry.room_batch import UnitConfirmation
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    service,q,d=room_fixture(workspace)
    b=service.confirm(service.run(q)['batch_id'],UnitConfirmation(unit='mm',confirmed=True))
    parent=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='测量候选面积'))
    parent.task.data['batches'][b['batch_id']]=b;parent.task.save()
    workspace.store.put('chat_run','parent',dict(id='parent',snapshot_id=d['id'],status='completed',task_id=parent.task.data['task_id']))
    child=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='只保留最大一处',parent_run_id='parent'))
    restored=restore_previous(child)
    assert restored['batches'][0]['batch_id']==b['batch_id']
    assert child.room_batches[b['batch_id']]['items'][0]['number']==1
    assert child.sent_image_ids==set()
    r=analyze(child,AnalyzeMeasurements(batch_id=b['batch_id'],limit=1,replace_highlights=True))
    assert r['ordered'][0]['value']==pytest.approx(24)
    d['sha256']='changed';workspace.store.put('drawing',d['id'],d)
    with pytest.raises(ValueError,match='版本'):restore_previous(child)


def test_discovery_collection_keeps_identity_and_scoped_ranking(workspace):
    from test_room_contours import fixture
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    service,record,candidate=fixture(workspace)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=record['snapshot_id'],question='比较未命名空间周长'))
    page=service.discovery_summary(record,0,20);s.register_observation('discover_rooms',page)
    r=analyze(s,AnalyzeMeasurements(discovery_id=record['measurement_id'],candidate_ids=[candidate['candidate_id']],metric='perimeter',replace_highlights=True))
    assert r['values'][0]['value']==pytest.approx(40)
    assert r['coverage']['selected_count']==1
    assert s.highlight_action['kind']=='geometry' and len(s.highlight_action['regions'])==1
    with pytest.raises(ValueError,match='已读取'):analyze(s,AnalyzeMeasurements(discovery_id=record['measurement_id'],candidate_ids=['fake']))


def test_final_projection_preserves_measurements_without_geometry_repetition():
    from cad_agent.analysis.boundary_context import synthesis_observations
    r=analyze(result_session(),AnalyzeMeasurements(batch_id='b',operation='range'))
    obs={'o':dict(observation_id='o',tool='analyze_measurements',data=r)}
    out=synthesis_observations(obs)[0]['data']
    assert out['calculation']==r['calculation']
    assert out['ordered']==r['ordered']
    assert 'values' not in out and 'values' in obs['o']['data']


def test_dimension_projection_keeps_all_exact_values_and_diagnostic_types():
    from cad_agent.analysis.boundary_context import synthesis_observations
    items=[dict(object_id=f'd{i}',rendered_texts=[str(i)],text_override='<>',
                actual_measurement=i+.123456789,definition_points=[[i,0],[i+1,0]],
                applied_unit='mm',diagnostics=[]) for i in range(20)]
    data=dict(items=items,total=40,has_more=True,source_diagnostics=[{'code':'UNSUPPORTED','entity_id':f'e{i}'} for i in range(100)])
    saved=deepcopy(data)
    out=synthesis_observations({'o':dict(observation_id='o',tool='read_dimensions',data=data)})[0]['data']
    rows=[dict(zip(out['dimension_fields'],row)) for row in out['dimension_rows']]
    assert [(r['object_id'],r['actual_measurement'],r['definition_points']) for r in rows]==[(r['object_id'],r['actual_measurement'],r['definition_points']) for r in items]
    assert out['has_more'] and out['total']==40
    assert out['source_diagnostics']==['UNSUPPORTED'] and out['source_diagnostics_count']==100
    assert data==saved


def test_followup_unit_change_cannot_reuse_old_physical_measurements():
    s=result_session();s.task.data['unit_policy']={'condition':'explicit','unit':'mm'}
    with pytest.raises(ValueError,match='重新测量'):analyze(s,AnalyzeMeasurements(batch_id='b'))


def test_relation_final_table_keeps_connection_uncertainty_and_exact_widths():
    from cad_agent.analysis.boundary_context import synthesis_observations
    items=[dict(object_id=f'opening{i}',width_m=i+.25,passage_status='not_established',
                boundary=[[0,0],[1,0],[1,1]],entity_ids=['e1'],room_ids=[],wall_ids=['wall'],
                sides=[dict(side=0,room_ids=['room'],status='unique'),dict(side=1,room_ids=[],status='unresolved')],
                diagnostics=['two_sided_space_connection_unresolved'],
                classification_evidence=dict(labels=[dict(text='M0900',nominal_width_hint_m=.9,bbox=[0,0,1,1])])) for i in range(3)]
    data=dict(view='openings',items=items,total=30,has_more=True)
    d=synthesis_observations({'o':dict(observation_id='o',tool='get_component_evidence',data=data)},final=True)[0]['data']
    rows=[d['component_defaults']|dict(zip(d['component_fields'],r)) for r in d['component_rows']]
    assert [r['width_m'] for r in rows]==[.25,1.25,2.25]
    assert all(r['passage_status']=='not_established' and r['sides'][1]['status']=='unresolved' for r in rows)
    assert all('boundary' not in r and 'entity_ids' not in r for r in rows)
    assert d['total']==30 and d['has_more'] and data['items'][0]['entity_ids']==['e1']


def test_deployment_can_use_same_limits_as_frozen_regression(monkeypatch):
    from cad_agent.config import Settings
    monkeypatch.setenv('CAD_AGENT_TIMEOUT_SECONDS','360')
    monkeypatch.setenv('CAD_AGENT_MAX_TOOL_CALLS','28')
    s=Settings.load()
    assert s.agent_timeout_seconds==360 and s.agent_max_tool_calls==28


def test_floor_title_abbreviation_does_not_match_other_floors(workspace):
    from test_room_batch import room_fixture
    from cad_agent.cad.titles import same_plan_title
    service,q,d=room_fixture(workspace)
    assert service.run(q.model_copy(update={'floor_title':'三层'}))['total']==1
    assert same_plan_title('三层平面图','三层')
    assert not same_plan_title('十三层平面图','三层')
    assert not same_plan_title('地下三层平面图','三层')
    assert not same_plan_title('三层剖面图','三层')


def test_enclosure_inspection_never_deducts_unclassified_loops(workspace):
    from test_room_contours import fixture
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.enclosure_inspection import inspect, EnclosureInspection
    _,r,c=fixture(workspace)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=r['snapshot_id'],question='检查内部对象'))
    s.room_batches['b']=dict(batch_id='b',unit=r['applied_unit'],scope={'layout_id':r['layout_id']},items=[dict(number=1,highlight=dict(kind='candidate_boundary',boundary=c['boundary']))])
    result=inspect(s,EnclosureInspection(batch_id='b',number=1))
    assert result['total']==3 and result['exclusions']==[]
    assert result['post_exclusion_area_m2'] is None
    assert all(i['semantic_status']=='unclassified_closed_object' and not i['deducted'] for i in result['items'])


def test_tiled_discovery_has_finite_scope_and_does_not_promote_coverage(workspace,monkeypatch):
    from test_room_contours import fixture
    from cad_agent.geometry.contracts import RoomDiscover
    from cad_agent.geometry.tiled_discovery import discover_with_subdivision
    import cad_agent.geometry.tiled_discovery as tiled
    service,record,_=fixture(workspace)
    original=deepcopy(record);original['diagnostics']=['segment_limit_exceeded'];original['candidates']=[]
    monkeypatch.setattr(service,'discover',lambda args:original)
    calls=[]
    def discover(records,bounds,factor,args):
        calls.append(bounds)
        return {'candidates':[],'contours':[],'diagnostics':[],'complete':True}
    monkeypatch.setattr(tiled,'discover_enclosures',discover)
    # This tests the subdivision policy with a local spy. Spawn isolation and
    # parity are independently covered in test_geometry_isolated_stages.py.
    executor=workspace.geometry_executor.compute
    monkeypatch.setattr(workspace.geometry_executor,'compute',lambda operation,payload,timeout=None:
        tiled.compute_subdivision(payload) if operation=='tiled_discovery' else executor(operation,payload,timeout))
    args=RoomDiscover(layout_id=record['layout_id'],bbox=record['scope_bbox'])
    result=discover_with_subdivision(service,args)
    # Empty strips must not consume the four occupied-neighborhood slots.
    assert len(calls)==16 and not result['complete'] and not result['subdivision']['full_scope_checked']
    assert result['subdivision']['checked_bboxes']==calls
    assert original['diagnostics']==['segment_limit_exceeded']
    assert result['baseline_discovery_id']==original['measurement_id']


def test_screened_result_reuse_cannot_reintroduce_rejected_objects(monkeypatch):
    from cad_agent.geometry.discovery_review import DiscoveryReviewService
    s=result_session();s.w=object()
    selected=[dict(item_id=i['region_id'],number=i['number'],boundary=i['highlight']['boundary']) for i in s.room_batches['b']['items']]
    record=dict(source_sha256='sha',applied_unit='m',selected=selected,layout_id='l',scope_bbox=[0,0,100,100])
    monkeypatch.setattr(DiscoveryReviewService,'__init__',lambda *a:None)
    monkeypatch.setattr(DiscoveryReviewService,'get',lambda *a:deepcopy(record))
    s.observations={'o':dict(tool='review_discovered_spaces',data={'review_id':'review'})}
    out=analyze(s,AnalyzeMeasurements(review_id='review',descending=False,limit=1,replace_highlights=True))
    assert out['ordered'][0]['number']==4 and out['ordered'][0]['value']==6
    assert s.highlight_action['object_ids']==['r4']
    assert s.highlight_action['regions'][0]['boundary']==selected[0]['boundary']
    assert len(record['selected'])==3
    with pytest.raises(ValueError,match='已通过审查'):analyze(s,AnalyzeMeasurements(review_id='review',candidate_ids=['furniture']))
    record['source_sha256']='changed'
    with pytest.raises(ValueError,match='来源'):analyze(s,AnalyzeMeasurements(review_id='review'))


@pytest.mark.parametrize('actual,expected',[('mm','completed'),('m','blocked_capability')])
def test_missing_door_does_not_undo_verified_unit_constraint(workspace,demo_bytes,actual,expected):
    from cad_agent.analysis.tasks import TaskLedger
    from cad_agent.contracts import ChatRequest
    d=workspace.ingest('units.dxf',demo_bytes)
    t=TaskLedger(workspace,ChatRequest(snapshot_id=d['id'],question='本次使用毫米。'))
    t.data['observations']={'o':dict(tool='review_space_connections',data={'applied_unit':actual})}
    claim=dict(state='unknown',text='目标门洞无法测量',question_part_ids=['q1'],observation_ids=['o'])
    t.finalise(dict(claims=[claim]))
    assert t.data['subtasks'][0]['status']==expected


def test_planning_distinguishes_geometric_lengths_from_native_dimension_annotations():
    from cad_agent.analysis.operations import planned_operations
    assert planned_operations('将两侧边界量出来',['dimensions'])==['geometry']
    assert planned_operations('测量相邻柱的间距',['dimensions'])==['geometry']
    assert 'dimensions' in planned_operations('测量墙间净宽，并与尺寸标注对照',['dimensions'])
    assert 'dimensions' in planned_operations('读取结构标高',['dimensions'])
