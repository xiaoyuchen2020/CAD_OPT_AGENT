"""Scope identities, executable continuation and frozen Q11 source replay."""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.fact_tools import ResolveScope, resolve_scope
from cad_agent.analysis.scope_workflow import bind_scope_call
from cad_agent.analysis.tasks import TaskPlan, PlanStep, TaskLedger
from cad_agent.cad.titles import same_plan_title, plan_title_relation
from cad_agent.components.contracts import DimensionQuery
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.contracts import GeometryMeasure, SpatialQuery
from cad_agent.geometry.service import GeometryService
from test_room_batch import room_fixture


@pytest.mark.parametrize('a,b,match', [
    ('地下室一层平面图','地下一层平面图',True),
    ('地下室一层平时平面图','地下一层平面图',False),
    ('十一层平面图','一层平面图',False),
    ('地下室一层战时平面图','地下室一层平时平面图',False),
    ('地下室顶板平面图','地下室平面图',False),
    ('非机动车库夹层平面图','地下室一层平面图',False),
    ('坡道详图','地下一层平面图',False),
])
def test_title_equivalence_preserves_qualifiers(a,b,match):
    assert same_plan_title(a,b) == match
    assert plan_title_relation('地下室一层平时平面图','地下一层平面图') == 'unspecified_mode'


def session(workspace, drawing, question='测量三层平面图的几何距离。'):
    s=AgentRunner(workspace,ChatRequest(snapshot_id=drawing['id'],question=question,mode='agent',runtime='responses_subscription'))
    s.task.plan(TaskPlan(steps=[PlanStep(requirement_id=p['id'],kind='measure',acceptance='有可定位的尺寸或几何证据。',operations=['geometry']) for p in s.task.data['subtasks']]))
    return s


def invoke(s, tool, args):
    return json.loads(s.invoke_tool(tool, args))


def test_scope_continuation_is_task_owned_budgeted_and_persisted(workspace):
    _,_,drawing=room_fixture(workspace,units=4)
    s=session(workspace,drawing)
    resolved=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',names=['试点房间'],task_ids=['q1']))
    assert 'error' not in resolved,resolved
    scope=resolved['selected_scope']
    assert scope and not resolved['operation_receipts']
    pending=[c for c in s.task.pending_work() if c['operation']=='scope_measurement_next']
    assert pending and s.task.data['subtasks'][0]['calls']==1
    call=pending[0]
    result=invoke(s,call['tool'],call['arguments']|{'task_ids':call['task_ids']})
    assert 'error' not in result,result
    assert result['scope_binding']['scope_id']==scope['scope_id']
    assert s.task.data['subtasks'][0]['calls']==2
    assert call['call_id'] not in {c.get('call_id') for c in s.task.pending_work()}
    saved=TaskLedger.read(workspace,s.task.data['task_id'])
    assert saved['drawing_scopes'][scope['scope_id']]['source_sha256']==drawing['sha256']
    assert any(o['data'].get('scope_binding') for o in saved['observations'].values())


def test_scope_guard_rejects_cross_frame_bounds_entities_and_stale(workspace,monkeypatch):
    _,_,drawing=room_fixture(workspace,units=4);s=session(workspace,drawing)
    invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))
    scope=next(iter(s.task.data['drawing_scopes'].values()))
    with pytest.raises(ValueError,match='SCOPE_MISMATCH'):
        bind_scope_call(s,'read_dimensions',DimensionQuery(layout_id='elsewhere'))
    with pytest.raises(ValueError,match='SCOPE_BOUNDS'):
        bind_scope_call(s,'query_spatial',SpatialQuery(layout_id=scope['layout_id'],bbox=[-1e9,-1e9,1e9,1e9]))
    monkeypatch.setattr(GeometryService,'records',lambda *a:[dict(entity_id='outside',layout_id=scope['layout_id'],bbox=[1e8,1e8,1e9,1e9])])
    with pytest.raises(ValueError,match='SCOPE_ENTITY'):
        bind_scope_call(s,'measure_geometry',GeometryMeasure(operation='length',entity_ids=['outside']))
    scope['source_sha256']='stale'
    with pytest.raises(ValueError,match='SCOPE_STALE'):
        bind_scope_call(s,'read_dimensions',DimensionQuery(layout_id=scope['layout_id']))


def test_no_scope_does_not_bind_other_requirement(workspace):
    _,_,drawing=room_fixture(workspace,units=4);s=session(workspace,drawing,'测量距离。读取说明。')
    invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))
    s.trace.append(dict(task_ids=['q2']))
    args=DimensionQuery(layout_id=drawing['layouts'][0]['layout_id'])
    assert bind_scope_call(s,'read_dimensions',args)==args


def replay_real_q11(workspace):
    """Read immutable frozen source; all derived writes go to pytest workspace."""
    root=Path(__file__).resolve().parents[1]/'artifacts/designer-facts-latest-20261019'
    db=root/'data/index.sqlite3'
    if not db.exists():pytest.skip('Frozen Q11 dataset is not installed')
    old=json.loads((root/'Q11-A-task.json').read_text())
    sid=old['snapshot_id']
    with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as conn:
        drawing=json.loads(conn.execute("SELECT data FROM records WHERE kind='drawing' AND id=?",(sid,)).fetchone()[0])
    workspace.store.put('drawing',sid,drawing)
    for name in ('source.dxf','regions.json','scene.json','entities.json'):
        dest=workspace.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/'data/drawings'/sid/name,dest)
    s=session(workspace,drawing,'测量地下一层平面图的机动车坡道净宽。')
    resolved=invoke(s,'resolve_drawing_scope',dict(floor_title='地下一层平面图',names=['机动车坡道'],task_ids=['q1']))
    assert 'error' not in resolved,resolved
    assert resolved['scope_status']=='single_candidate'
    scope=resolved['selected_scope'];assert scope['frame_id']=='frame_19e0c1f200dc3fb1c32048ed'
    assert {i['region_id'] for i in resolved['items']}=={'region_97e1ff86558d7a4a5251ef9e','region_900d1be81ed048a3c4703561'}
    assert all(i['frame_id']==scope['frame_id'] for i in resolved['items'])
    assert any(i['region_id']=='region_c3af098ee50adcd0bae0ff01' for i in resolved['excluded_candidates'])
    old_scope=next(o['data'] for o in old['observations'].values() if o['tool']=='resolve_drawing_scope' and o['data']['scope_status']=='single_candidate')
    assert len(old_scope['items'])==3 # frozen bug includes a conflicting detail frame
    call=resolved['next_calls'][0]
    result=invoke(s,call['tool'],call['arguments']|{'task_ids':['q1']})
    assert 'error' not in result,result
    assert result['scope_binding']['frame_id']==scope['frame_id']
    assert result.get('items'),result
    assert call['tool']=='inspect_scope_objects' and result['unit']=='unknown'
    assert result['window']['bbox'] != scope['bbox']
    assert all(not r['fulfilled'] for r in result['operation_receipts'])
    assert all(i['identity_status']=='unconfirmed' for i in result['items'])
    call=resolved['next_calls'][1]
    geometry=invoke(s,call['tool'],call['arguments']|{'task_ids':['q1']})
    assert 'error' not in geometry,geometry
    assert geometry['scope_binding']['frame_id']==scope['frame_id']
    assert geometry['items']
    assert s.task.data['subtasks'][0]['calls']==3
    return dict(evidence_class='frozen_source_deterministic_replay', model_calls=0,
        source_snapshot_id=sid, source_sha256=drawing['sha256'],
        frozen_run_id=json.loads((root/'Q11-A-run.json').read_text())['id'],
        frozen_task_id=old['task_id'], historical_mixed_candidate_count=len(old_scope['items']),
        replay_task_id=s.task.data['task_id'], selected_scope=scope,
        retained_candidates=resolved['items'], excluded_candidates=resolved['excluded_candidates'],
        first_object_window=dict(total=result.get('total'), returned=len(result['items']),
            scope_binding=result['scope_binding'], window=result['window'],
            entity_ids=[eid for i in result['items'] for eid in i['entity_ids']]),
        second_object_window=dict(total=geometry['total'], returned=len(geometry['items']),
            scope_binding=geometry['scope_binding'],window=geometry['window'],
            entity_ids=[eid for i in geometry['items'] for eid in i['entity_ids']]),
        budgeted_calls=s.task.data['subtasks'][0]['calls'],
        operation_receipts=s.task.data['subtasks'][0]['operation_receipts'],
        limitations=['名称及原生尺寸、局部线形均为候选。', '没有确认最不利结构净宽、单/双车道或完成面扣减。',
                     '局部名称窗口不代表已穷尽坡道对象或全图。'])


def test_real_q11_frozen_scope_and_followup(workspace):
    replay_real_q11(workspace)


def test_unspecified_mode_cannot_select_wartime_or_peacetime(workspace):
    _,_,drawing=room_fixture(workspace,units=4)
    regions=workspace.regions(drawing['id'])
    title=next(r for r in regions if r.text=='三层平面图')
    title.text='地下室一层平时平面图'
    other=title.model_copy(update={'region_id':'wartime-title','frame_id':'wartime','text':'地下室一层战时平面图'})
    regions.append(other)
    drawing['frames'][0]['name']=title.text
    drawing['frames'].append(dict(drawing['frames'][0],frame_id='wartime',name=other.text))
    workspace.store.put('drawing',drawing['id'],drawing)
    workspace.store.artifact(drawing['id'],'regions.json').write_text(json.dumps([r.model_dump() for r in regions]))
    s=session(workspace,drawing)
    result=invoke(s,'resolve_drawing_scope',dict(floor_title='地下一层平面图',task_ids=['q1']))
    assert result['selected_scope'] is None and result['items']==[]
    assert all('unspecified_operating_mode' in c['reasons'] for c in result['choices'])
    blocked=invoke(s,'read_dimensions',dict(layout_id=drawing['layouts'][0]['layout_id'],task_ids=['q1']))
    assert 'SCOPE_UNRESOLVED' in blocked['error']['message']
    exact=invoke(s,'resolve_drawing_scope',dict(floor_title='地下一层平时平面图',task_ids=['q1']))
    assert exact['selected_scope']['frame_id']=='floor'


def test_scope_owner_cache_and_efficient_defaults(workspace):
    _,_,drawing=room_fixture(workspace,units=4)
    s=session(workspace,drawing,'测量距离。读取尺寸。')
    first=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))
    scope=first['selected_scope']
    args=dict(layout_id=scope['layout_id'])
    a=invoke(s,'read_dimensions',args|{'task_ids':['q1']})
    assert a['scope_binding']['arguments']['limit']==5
    second=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q2']))
    assert second['selected_scope']['scope_id']==scope['scope_id']
    b=invoke(s,'read_dimensions',args|{'task_ids':['q2']})
    assert not b.get('already_returned') and b['scope_binding']['task_ids']==['q2']
    # Unrelated native dimensions do not discharge the object-window review.
    pending=[c for c in s.task.pending_work() if c['operation']=='scope_measurement_next']
    assert pending and all(c['tool']=='inspect_scope_objects' for c in pending)
    # An independent source read retains the original efficient default too.
    invoke(s,'search_sources',dict(query='三层',task_ids=['q2']))
    assert s.trace[-1]['arguments']['limit']==5


def test_dimensions_crossing_scope_are_diagnostic_not_measurement_receipts(workspace):
    from cad_agent.analysis.scope_workflow import record_scope_call
    from cad_agent.analysis.operations import receipts
    _,_,drawing=room_fixture(workspace,units=4);s=session(workspace,drawing)
    scope=invoke(s,'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))['selected_scope']
    result=dict(items=[dict(object_id='crossing',layout_id=scope['layout_id'],bbox=[-1e9,0,1e9,1],definition_points=[])],
                total=1,operation_receipts=[dict(operation='dimensions',fulfilled=True)])
    record_scope_call(s,'read_dimensions',dict(layout_id=scope['layout_id']),result)
    assert result['items']==[] and result['excluded_scope_items'][0]['object_id']=='crossing'
    assert not any(r['fulfilled'] for r in receipts('read_dimensions',result))
