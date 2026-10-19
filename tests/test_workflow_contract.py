"""Task/geometry/publication integration, including original failed task contracts."""
import json
from copy import deepcopy
import pytest
from shapely.geometry import Polygon
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.tasks import TaskPlan, PlanStep
from cad_agent.analysis.unit_policy import user_unit_policy
from cad_agent.contracts import ChatRequest
from test_room_batch import room_fixture


def room_choices(s):
    review=list(s.boundary_reviews.values())[-1]
    choices=[]
    for row in review['items']:
        eligible=[c for c in row['candidates'] if c['eligible']]
        c=min(eligible,key=lambda c:Polygon(c['boundary']).area) if eligible else None
        choices.append(dict(region_id=row['region_id'],candidate_id=c['candidate_id'] if c else '',
                            decision='select' if c else 'uncertain',reason='测试标注：选择闭合房间内部，排除家具框；无有效候选时保留未知。'))
    return dict(review_id=review['review_id'],choices=choices)


def complete_room_operations(s):
    """Deterministic test reviewer, not a live-model quality acceptance."""
    for _ in range(8):
        pending=s.task.pending_work()
        if not pending:return
        work=pending[0];ids=work['task_ids']
        if work['operation']=='boundary_review':
            out=json.loads(s.invoke_tool('prepare_room_boundaries',dict(batch_id=work['batch_id'],task_ids=ids)))
            assert 'error' not in out,out
            s.sent_image_ids.update(out['image_ids'])
            out=json.loads(s.invoke_tool('select_room_boundaries',room_choices(s)|{'task_ids':ids}))
        else:
            out=json.loads(s.invoke_tool('resolve_room_units',dict(batch_id=work['batch_id'],task_ids=ids)))
        assert 'error' not in out,out
    assert not s.task.pending_work()


def make_session(workspace,question,units=0):
    _,q,d=room_fixture(workspace,units)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question=question,mode='agent',runtime='responses_subscription',model_profile='siwc_sol'))
    s.task.auto_plan()
    return s,q


def test_single_question_ids_share_one_contract(workspace):
    s,q=make_session(workspace,'帮我高亮这些房间轮廓')
    s.invoke_tool('analyze_room_batch',q.model_dump())
    complete_room_operations(s)
    obs=next(oid for oid,o in s.observations.items() if o['tool']=='select_room_boundaries')
    answer={'claims':[dict(text='已高亮所选房间轮廓候选。',state='derived',question_part_ids=['q1'],
                 evidence_basis='tool_observation',observation_ids=[obs],observation_kind='visual_interpretation')]}
    out=s.finish(json.dumps(answer))
    assert out['task']['status']=='completed' and out['claims'][0]['question_part_ids']==['q1']
    answer['claims'][0]['question_part_ids']=['q99']
    with pytest.raises(ValueError,match='不存在'):s.finish(json.dumps(answer))


@pytest.mark.parametrize('question',[
    '三层平面图有哪些试点房间？面积分别是多少？并高亮。如果没有尺寸标注，默认图纸单位为毫米。',
    '请计算试点房间的面积并标出轮廓。没有尺寸标注就按毫米计算。',
    '面积是多少？请圈出房间边界。本次使用毫米。',
])
def test_semantic_equivalent_requests_complete_geometry_before_delivery(workspace,question):
    s,q=make_session(workspace,question)
    ids=[p['id'] for p in s.task.parts]
    s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':ids})
    assert s.task.pending_work() and not s.focus.review_ready() and not s.policy.stop_reason
    assert 'prepare_room_boundaries' in s.focus.names()
    with pytest.raises(ValueError,match='TASK_WORK_PENDING'):
        s.finish(json.dumps({'claims':[dict(text='尚未测量',state='unknown',question_part_ids=ids)]}))
    complete_room_operations(s)
    assert not s.task.pending_work()
    b=s.room_batch;i=b['items'][0]
    assert b['unit']=='mm' and i['candidate_area_m2']==pytest.approx(24)
    assert i['area_m2'] is None and i['area_basis']=='selected_wall_enclosure'
    assert i['candidate_area_m2']==pytest.approx(Polygon(i['highlight']['boundary']).area*1e-6)
    assert not any(t['status']=='waiting_input' for t in s.task.data['subtasks'])


def test_room_batch_cannot_deliver_unrelated_lookup(workspace):
    s,q=make_session(workspace,'列出试点房间。读取防火说明。')
    s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2']})
    step=s.task.data['subtasks'][1]
    assert not step['delivered'] and not step['batch_ids'] and step['status']=='pending'
    with pytest.raises(ValueError,match='未覆盖'):
        s.finish(json.dumps({'claims':[dict(text='已列名称',state='unknown',question_part_ids=['q1'])]}))


def test_failure_is_not_published_and_resume_keeps_selected_geometry(workspace):
    s,q=make_session(workspace,'帮我高亮这些房间轮廓')
    s.invoke_tool('analyze_room_batch',q.model_dump());complete_room_operations(s)
    selected=deepcopy(s.room_batch['items'][0]['highlight'])
    s.task.publication_failed({'code':'TEST','message':'presentation rejected'})
    assert s.task.public()['status']=='partial' and s.task.public()['publication_status']=='failed'
    resumed=AgentRunner(workspace,s.request.model_copy(update={'resume_task_id':s.task.data['task_id']}))
    assert list(resumed.room_batches.values())[0]['items'][0]['highlight']==selected
    assert not resumed.task.pending_work() and resumed.sent_image_ids


@pytest.mark.parametrize('text,expected',[
    ('没有尺寸标注时采用厘米。','cm'),('本次测量按米。','m'),('单位未知时默认毫米。','mm'),
    ('不要默认毫米。',None),('如果是地下室则默认毫米。',None),('毫米还是米？',None),
])
def test_user_unit_authorization_is_not_model_guess(text,expected):
    p=user_unit_policy(text)
    assert (p['unit'] if p else None)==expected


def test_dimension_presence_blocks_conditional_unit_but_not_geometry(workspace,monkeypatch):
    import cad_agent.analysis.unit_policy as units
    s,q=make_session(workspace,'请测量并高亮试点房间。如果没有尺寸标注，默认毫米。')
    monkeypatch.setattr(units,'read_dimensions',lambda *a:{'total_native_dimensions':2,'truncated':False,'source_diagnostics':[]})
    out=json.loads(s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':[p['id'] for p in s.task.parts]}))
    assert out['unit_resolution']['dimension_check']['native_count']==2
    assert out['unit']=='unknown' and out['unit_resolution']['status']=='needs_input'
    complete_room_operations(s)
    assert s.room_batch['items'][0]['highlight']['kind']=='candidate_boundary'
    assert s.room_batch['items'][0]['candidate_area_m2'] is None


def test_declared_unit_conflict_is_not_silently_converted(workspace):
    s,q=make_session(workspace,'计算房间面积。本次按米测量。',units=4)
    out=json.loads(s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':[p['id'] for p in s.task.parts]}))
    assert out['unit']=='unknown' and '冲突' in out['unit_resolution']['reason']


def test_delivered_area_must_match_highlight(workspace):
    s,q=make_session(workspace,'计算并高亮房间。本次按毫米测量。')
    # Include an explicit measurement goal for this deliberately short wording.
    s.task.data['subtasks'][0]['kind']='measure'
    s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':[p['id'] for p in s.task.parts]});complete_room_operations(s)
    s.task.check_batch_rows()
    b=next(iter(s.task.data['batches'].values()));b['items'][0]['candidate_area_m2']+=1
    with pytest.raises(ValueError,match='面积与高亮'):s.task.check_batch_rows()


@pytest.mark.parametrize('text,state',[
    ('面积24.00m²','numerically_close'),('面积30㎡','different'),('面积30m','unit_text_incomplete'),
])
def test_annotation_comparison_keeps_unit_and_area_basis_explicit(workspace,monkeypatch,text,state):
    from types import SimpleNamespace
    from shapely.geometry import box
    service,q,d=room_fixture(workspace)
    batch=service.run(q)
    row=SimpleNamespace(text=text,bbox=[1,1,2,2])
    monkeypatch.setattr(service.index,'scope',lambda *a:[row])
    monkeypatch.setattr(service.index,'evidence',lambda r:{'evidence_id':'annotation','quote':r.text})
    item={'candidate_area_m2':24,'diagnostics':[]}
    service.check_area_annotations(batch,item,box(0,0,6,4))
    assert item['area_annotation_checks'][0]['status']==state
    assert item['area_annotation_checks'][0]['evidence']['quote']==text
    assert item['candidate_area_m2']==24
