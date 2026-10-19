"""Synthetic DXF → real Session tools → persisted task → child session."""
import json
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from test_room_discovery import plan


def test_discovery_finish_and_parent_reuse(workspace):
    _, args, drawing = plan(workspace, ['会议室', None])
    parent = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'], question='读取空间候选'))
    wire = json.loads(parent.invoke_tool('discover_rooms', args.model_dump()))
    assert 'error' not in wire, wire
    assert wire['object_registry']['objects']
    objects = parent.task.data['object_registry']['objects']
    assert objects and all(o['identity']['engineering_confirmed'] is False for o in objects.values())
    event_count = len(parent.task.data['object_registry']['events'])
    duplicate = json.loads(parent.invoke_tool('discover_rooms', args.model_dump()))
    assert 'error' not in duplicate
    assert len(parent.task.data['object_registry']['events']) == event_count
    answer = parent.finish(json.dumps({'claims':[{'text':'所读围合仍为候选，空间身份尚未确认。','state':'unknown', 'evidence_basis':'tool_observation', 'observation_ids':[wire['observation_id']]}]}))
    assert answer['object_registry']['candidate_object_count']==len(objects)
    workspace.store.put('chat_run','registry-parent',dict(id='registry-parent', snapshot_id=drawing['id'],status='completed',task_id=parent.task.data['task_id']))
    child = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'],question='继续读取空间候选',parent_run_id='registry-parent'))
    restored=json.loads(child.invoke_tool('get_analysis_results',{}))
    assert 'error' not in restored, restored
    assert child.task.data['object_registry']['objects']==objects
    assert child.task.data['object_workflows']==parent.task.data['object_workflows']
    assert child.sent_image_ids==set()
    changed = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'],question='使用厘米继续测量',parent_run_id='registry-parent'))
    refused = json.loads(changed.invoke_tool('get_analysis_results',{}))
    assert refused.get('error'), refused
    assert 'object_registry' not in changed.task.data


def test_text_tools_and_text_finish_have_no_object_metadata(workspace):
    _, _, drawing = plan(workspace, ['会议室'])
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取图纸概览'))
    wire=json.loads(session.invoke_tool('get_drawing_overview',{'view':'layouts'}))
    assert 'error' not in wire
    assert 'object_registry' not in wire and 'object_workflow' not in wire
    assert 'object_registry' not in session.task.data
    result=session.finish(json.dumps({'claims':[{'text':'本轮仅检查布局入口，尚未核查空间身份。','state':'unknown'}]}))
    assert 'object_registry' not in result


def test_local_unit_confirmation_updates_registry_without_model(workspace):
    from test_room_batch import room_fixture
    from cad_agent.analysis.tasks import TaskLedger, TaskUnitConfirmation
    _, args, drawing=room_fixture(workspace)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取房间候选'))
    result=json.loads(session.invoke_tool('analyze_room_batch',args.model_dump()))
    assert 'error' not in result, result
    old_id=result['batch_id']
    original=set(session.task.data['object_registry']['objects'])
    assert all(o['measurement_basis']['status']=='unknown' for o in session.task.data['object_registry']['objects'].values())
    response=TaskLedger.confirm_unit(workspace,session.task.data['task_id'],TaskUnitConfirmation(batch_id=old_id,unit='mm',confirmed=True))
    assert response['model_calls']==0
    saved=TaskLedger.read(workspace,session.task.data['task_id'])
    assert set(saved['object_registry']['objects'])==original
    obj=next(iter(saved['object_registry']['objects'].values()))
    assert obj['measurement_basis']['unit']=='mm'
    assert obj['measurement_basis']['area_m2']==24
    assert obj['measurement_basis']['engineering_confirmed'] is False
    assert any(c.get('lifecycle_status')=='superseded' for c in saved['object_registry']['collections'].values())
    assert any(f['stages']['measurement']=='candidate' for f in saved['object_workflows'].values())
    resumed=CadAgentSession(workspace,session.request.model_copy(update={'resume_task_id':saved['task_id']}))
    assert next(iter(resumed.task.data['object_registry']['objects'].values()))['measurement_basis']['area_m2']==24


def test_local_page_continuation_updates_coverage_without_model(workspace):
    from test_room_geometry import ingest
    from cad_agent.analysis.tasks import TaskLedger, TaskBatchNavigation
    def add(doc,pt,line):
        doc.modelspace().add_text('三层平面图',dxfattribs={'insert':pt((1,-3)),'height':150})
        doc.modelspace().add_text('试点房间',dxfattribs={'insert':pt((8,1)),'height':150})
    _,_,drawing=ingest(workspace,build=add,units=0)
    frame=dict(frame_id='floor',name='三层平面图',layout_id=drawing['layouts'][0]['layout_id'],bbox=[-5000,-8000,15000,15000],method='closed_border_and_title_candidate')
    drawing['frames']=[frame];workspace.store.put('drawing',drawing['id'],drawing)
    regions=workspace.regions(drawing['id'])
    for r in regions:r.frame_id='floor';r.frame_name='三层平面图'
    workspace.store.artifact(drawing['id'],'regions.json').write_text(json.dumps([r.model_dump() for r in regions]))
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取房间候选'))
    result=json.loads(session.invoke_tool('analyze_room_batch',dict(floor_title='三层平面图',room_label='试点房间',limit=1)))
    assert result['total']==2 and result['has_more']
    assert result['object_registry']['coverage']['unread_count']==1
    response=TaskLedger.navigate_batch(workspace,session.task.data['task_id'],TaskBatchNavigation(batch_id=result['batch_id'],offset=1))
    assert response['model_calls']==0
    saved=TaskLedger.read(workspace,session.task.data['task_id'])
    assert len(saved['object_registry']['objects'])==2
    collection=next(iter(saved['object_registry']['collections'].values()))
    assert collection['coverage']['unread_count']==0
    assert collection['coverage']['enumeration_exhaustive'] is True
    assert collection['coverage']['engineering_room_count'] is None
