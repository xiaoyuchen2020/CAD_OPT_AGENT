import math
import json
import ezdxf
import pytest
from cad_agent.geometry.local_measurement import measure_pair
from cad_agent.geometry.workbench import GeometryWorkbench,PrepareBundle,ReadBundle,MeasurePair,RenderWorkbench


def line(name,a,b,layout='l'):
    return dict(entity_id=name,layout_id=layout,status='ready',kind='LINE',native=dict(start=list(a)+[0],end=list(b)+[0]))


def arc(name,r,start,end,center=(0,0,0)):
    return dict(entity_id=name,layout_id='l',status='ready',kind='ARC',native=dict(center=list(center),radius=r,start_angle=start,end_angle=end))


@pytest.mark.parametrize('angle',[0,37,90,184])
@pytest.mark.parametrize('scale',[.01,1,1000])
def test_parallel_translation_rotation_scale(angle,scale):
    t=math.radians(angle)
    def p(x,y):return [1e6+scale*(x*math.cos(t)-y*math.sin(t)),-2e6+scale*(x*math.sin(t)+y*math.cos(t))]
    m=measure_pair(line('a',p(0,0),p(10,0)),line('b',p(8,3),p(2,3)),'parallel_normal')
    assert m['status']=='local_pair_measured'
    assert m['measurements']['width_drawing_units']==pytest.approx(3*scale,abs=1e-8)
    assert math.dist(*m['measurement_line'])==pytest.approx(3*scale,abs=1e-8)
    assert m['domain']['overlap_length']==pytest.approx(6*scale,abs=1e-8)


@pytest.mark.parametrize('b,code',[(line('b',[20,3],[30,3]),'NO_FINITE_OVERLAP'),(line('b',[0,3],[3,7]),'NONPARALLEL_LINES'),(line('b',[1,3],[1,3]),'DEGENERATE_LINE')])
def test_invalid_local_inputs_return_diagnostic(b,code):
    m=measure_pair(line('a',[0,0],[10,0]),b,'parallel_normal')
    assert m['status']=='unsupported' and not m['measurements']
    assert m['diagnostics'][0]['code']==code


def test_arc_wrap_and_domain():
    m=measure_pair(arc('a',8,350,40),arc('b',12,355,20),'concentric_radial')
    assert m['measurements']['width_drawing_units']==4
    assert m['domain']['intervals_degrees']==[[0,20],[355,360]]
    assert math.dist(*m['measurement_line'])==pytest.approx(4)
    assert not m['global_minimum_proven']


@pytest.mark.parametrize('b,code',[(arc('b',12,70,90),'NO_ANGULAR_OVERLAP'),(arc('b',12,350,20,(1,0,0)),'NONCONCENTRIC_ARCS')])
def test_arc_rejects_wrong_premise(b,code):
    assert measure_pair(arc('a',8,350,40),b,'concentric_radial')['diagnostics'][0]['code']==code


def test_workbench_source_binding_and_render(workspace,tmp_path):
    d=ezdxf.new();d.units=0;m=d.modelspace()
    m.add_line((0,0),(20,0));m.add_line((0,4),(20,4));m.add_text('plan title',dxfattribs={'insert':(0,6),'height':1})
    p=tmp_path/'input.dxf';d.saveas(p);drawing=workspace.ingest('input.dxf',p.read_bytes())
    sid=drawing['snapshot_id'];w=GeometryWorkbench(workspace,sid)
    request=PrepareBundle(layout_id=drawing['layouts'][0]['layout_id'],bbox=[-1,-1,22,9])
    b=w.prepare(request);assert w.prepare(request)==b
    data=w.read(ReadBundle(bundle_id=b['bundle_id'],kind='LINE'))
    ids=[r['entity_id'] for r in data['items']]
    args=MeasurePair(bundle_id=b['bundle_id'],entity_ids=ids,basis='parallel_normal')
    r=w.measure(args);assert r['measurements']['width_drawing_units']==4
    assert w.measure(args)==r
    from types import SimpleNamespace
    from cad_agent.analysis.spatial_delivery import spatial_measurement_issues
    assert spatial_measurement_issues(SimpleNamespace(text='候选间距4毫米'),[r])
    assert not spatial_measurement_issues(SimpleNamespace(text='候选间距4原图单位'),[r])
    image=w.render(RenderWorkbench(bundle_id=b['bundle_id'],measurement_id=r['measurement_id']))
    from cad_agent.geometry.vision import VisionService
    assert VisionService(workspace,sid).png(image['image_id']).startswith(b'\x89PNG')
    with pytest.raises(ValueError,match='SOURCE_NOT_IN_BUNDLE'):
        w.measure(args.model_copy(update={'entity_ids':['fake',ids[0]]}))
    source=workspace.store.artifact(sid,'source.dxf');source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='SOURCE_CHANGED'):w.read(ReadBundle(bundle_id=b['bundle_id']))


def test_tools_available_without_paid_channel_assumption():
    from cad_agent.analysis.tool_registry import runtime_supports,TOOL_SPECS
    assert runtime_supports('measure_local_geometry','native','deepseek')
    assert runtime_supports('measure_local_geometry','responses_subscription','responses_subscription')
    assert 'measure_local_geometry' in TOOL_SPECS


def test_public_tool_pipeline_registers_local_observation(workspace,tmp_path):
    from test_remediation_scope import session,invoke
    d=ezdxf.new();d.units=0;m=d.modelspace()
    m.add_line((0,0),(20,0));m.add_line((0,4),(20,4))
    path=tmp_path/'public.dxf';d.saveas(path)
    drawing=workspace.ingest('public.dxf',path.read_bytes());s=session(workspace,drawing)
    args=dict(layout_id=drawing['layouts'][0]['layout_id'],bbox=[-1,-1,22,9],task_ids=['q1'])
    b=invoke(s,'prepare_geometry_bundle',args);assert 'error' not in b,b
    r=invoke(s,'read_geometry_bundle',dict(bundle_id=b['bundle_id'],kind='LINE',task_ids=['q1']))
    out=invoke(s,'measure_local_geometry',dict(bundle_id=b['bundle_id'],entity_ids=[i['entity_id'] for i in r['items']],basis='parallel_normal',task_ids=['q1']))
    assert 'error' not in out,out
    assert out['observation_id'] in s.observations
    assert out['measurements']['width_drawing_units']==4
    ids=[i['entity_id'] for i in r['items']]
    h=invoke(s,'register_geometry_hypothesis',dict(bundle_id=b['bundle_id'],entity_ids=ids,view_kind='plan',interpretation='local candidate',task_ids=['q1']))
    route=invoke(s,'measure_geometry_route',dict(bundle_id=b['bundle_id'],hypothesis_id=h['hypothesis_id'],segments=[{'entity_ids':ids}],task_ids=['q1']))
    assert 'error' not in route,route
    # A route references a hypothesis; it must not replace that hypothesis's row.
    assert s.task.data['geometry_workset']['hypothesis_ids'][h['hypothesis_id']]['request']['view_kind']=='plan'
    state=invoke(s,'read_geometry_workset',dict(task_ids=['q1']))
    assert 'error' not in state,state
    assert h['hypothesis_id'] in state['hypothesis_ids'] and route['route_id'] in state['route_ids']
    s.task.data['geometry_workset']['hypothesis_ids'][h['hypothesis_id']]=dict(route)
    repaired=invoke(s,'read_geometry_workset',dict(task_ids=['q1']))
    assert h['hypothesis_id'] in repaired['recovered_hypothesis_ids']
    assert s.task.data['geometry_workset']['hypothesis_ids'][h['hypothesis_id']]['request']['view_kind']=='plan'
    from dataclasses import replace
    from cad_agent.analysis.delivery import AnswerValidationError
    workspace.settings=replace(workspace.settings,geometry_validation_mode='enforce')
    claim=dict(text='选定源实体的局部候选间距为4原图单位，工程归属和全程覆盖未确认。',state='derived',evidence_basis='tool_observation',observation_ids=[out['observation_id']],question_part_ids=['q1'])
    outcomes=[dict(requirement_id=p['id'],status='partial',gap_kind='ambiguous_scope',reason='局部测量成立，图示归属和全程覆盖未确认。') for p in s.task.parts]
    s.validate_answer(json.dumps(dict(claims=[claim],requirement_outcomes=outcomes),ensure_ascii=False))
    for text in ('局部间距为4毫米。','全程最小净宽为4原图单位。'):
        with pytest.raises(AnswerValidationError):s.validate_answer(json.dumps(dict(claims=[claim|{'text':text}],requirement_outcomes=outcomes),ensure_ascii=False))
    source=workspace.store.artifact(drawing['snapshot_id'],'source.dxf')
    source.write_bytes(source.read_bytes()+b'changed')
    invalid=invoke(s,'measure_local_geometry',dict(bundle_id=b['bundle_id'],entity_ids=ids,basis='parallel_normal',task_ids=['q1']))
    assert 'error' in invalid and 'GEOMETRY_SOURCE_CHANGED' in json.dumps(invalid)
