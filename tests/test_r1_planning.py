"""Frozen real planning failures, replayed offline without drawing/model writes."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from cad_agent.analysis.tasks import TaskLedger, TaskPlan, CoverageTarget
from cad_agent.analysis.planning import PlanValidationError, plan_error_feedback, _literal_fields

CASES = json.loads(Path(__file__).with_name('test_r1_frozen_plans.json').read_text())


def ledger(case):
    obj=object.__new__(TaskLedger)
    obj.data=dict(question=case['question'],subtasks=deepcopy(case['subtasks']),planned=False)
    for step in obj.data['subtasks']:
        step.update(status='pending',delivered=False,coverage=[])
    obj.save=lambda:None
    obj.public=lambda:deepcopy(obj.data)
    return obj


def refs(requirement_id, *quotes):
    return [dict(requirement_id=requirement_id,quote=q) for q in quotes]


def corrected(case):
    """Explicit review corrections, not automatic inferred intent or a model run."""
    args=deepcopy(case['arguments'])
    for step in args['steps']:
        for cell in step.get('coverage',[]):
            field=cell['field']
            if case['id']=='Q01-B' and field=='全部构造层、材料和厚度':
                cell['field_refs']=refs('q1','全部构造层','材料','厚度')
            elif case['id']=='Q06-B':
                if field=='逐层砂浆材料和厚度及总厚度':
                    cell['field']='砂浆材料和厚度及总厚度'
                    cell['field_refs']=refs('q1','砂浆材料','厚度','总厚度')
                elif field=='材料及厚度':
                    cell['field_refs']=refs('q1','材料','厚度')
            elif case['id']=='Q13-B' and field=='缺资料时逐项说明':
                cell['field']='资料时逐项说明'
            elif case['id']=='Q14-B' and cell['object'] in ('井坑与车位','井坑与通道'):
                cell['object_refs']=refs('q2','井坑',cell['object'].split('与')[1])
            elif case['id']=='Q16-B':
                if field.startswith('与'):cell['field']='算术差'
                if field=='缺失数据':cell['field']='分别说明'
            elif case['id']=='Q18-B' and field=='名称依据':
                cell['field']='图中有依据'
    return args


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_frozen_invalid_plan_has_exact_feedback_and_no_partial_mutation(case):
    obj=ledger(case);before=deepcopy(obj.data)
    with pytest.raises(PlanValidationError) as caught:
        obj.plan(TaskPlan.model_validate(case['arguments']))
    feedback=plan_error_feedback(caught.value)
    assert feedback['code']=='PLAN_VALIDATION_FAILED'
    assert all('.coverage.' in e['path'] and e['path'].endswith(('.field','.object')) for e in feedback['issues'])
    assert all(e.get('repair') and e.get('requirement_id') for e in feedback['issues'])
    assert obj.data==before
    assert feedback['next_action'] and '不要原样重试' in feedback['issues'][0]['repair']


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_explicit_review_corrections_preserve_requirements_and_question_spans(case):
    obj=ledger(case);out=obj.plan(TaskPlan.model_validate(corrected(case)))
    nodes={s['id']:s for s in out['subtasks']}
    assert out['planned'] and set(nodes)=={s['id'] for s in case['subtasks']}
    for step in out['subtasks']:
        assert not step['delivered'] and step['status']=='pending'
        for cell in step['coverage']:
            assert cell['state']=='unread' and cell['evidence_ids']==[]
            for field in ('object','field'):
                for binding in cell['question_binding'][field]:
                    if 'requirement_span' in binding:
                        lo,hi=binding['requirement_span'];text=nodes[binding['requirement_id']]['requirement']
                    else:
                        lo,hi=binding['question_span'];text=out['question']
                    assert text[lo:hi]==binding['quote']


def simple_case(question='读取甲站的材料和厚度。'):
    return dict(question=question,subtasks=[dict(id='q1',requirement=question)],
                arguments=dict(steps=[dict(requirement_id='q1',kind='lookup',acceptance='逐项读取',
                    coverage=[dict(object='甲站',field='材料和厚度',field_refs=refs('q1','材料','厚度'))])]))


@pytest.mark.parametrize('fault',['invented','wrong_quote','reversed','overlap','wrong_owner','extra_label','extra_key'])
def test_explicit_bindings_do_not_authorize_invented_requirements(fault):
    case=simple_case();cell=case['arguments']['steps'][0]['coverage'][0]
    if fault=='invented':cell.update(field='造价优化',field_refs=[])
    if fault=='wrong_quote':cell['field_refs'][0]['quote']='性能'
    if fault=='reversed':cell.update(field='厚度和材料',field_refs=refs('q1','厚度','材料'))
    if fault=='overlap':cell.update(field='材料和材料',field_refs=refs('q1','材料','材料'))
    if fault=='wrong_owner':cell['field_refs'][0]['requirement_id']='q99'
    if fault=='extra_label':cell['field']='材料和厚度和价格'
    if fault=='extra_key':cell['state']='addressed'
    obj=ledger(case);before=deepcopy(obj.data)
    with pytest.raises(ValueError):obj.plan(TaskPlan.model_validate(case['arguments']))
    assert obj.data==before


def test_cross_requirement_composition_is_not_silently_inferred():
    case=simple_case();case['subtasks'].append(dict(id='q2',requirement='读取厚度。'))
    case['arguments']['steps'].append(dict(requirement_id='q2',kind='lookup',acceptance='读取'))
    case['arguments']['steps'][0]['coverage'][0]['field_refs'][1]['requirement_id']='q2'
    with pytest.raises(PlanValidationError,match='同一原始分项'):
        ledger(case).plan(TaskPlan.model_validate(case['arguments']))


def test_fields_are_atomic_with_original_binding_and_24_limit_preserved():
    fields='材料、密度等级、强度、厚度及适用部位'
    question='甲站、乙站、丙站、丁站、戊站、己站的'+fields+'。'
    case=simple_case(question)
    case['arguments']['steps'][0]['coverage']=[dict(object=obj,field=fields) for obj in ['甲站','乙站','丙站','丁站','戊站','己站']]
    obj=ledger(case);out=obj.plan(TaskPlan.model_validate(case['arguments']))
    step=out['subtasks'][0]
    assert len(step['coverage'])==24 and step['coverage_overflow']
    assert {c['field'] for c in step['coverage']}=={'材料','密度等级','强度','厚度','适用部位'}
    assert all(c['planning_label']['field']==fields for c in step['coverage'])
    obj.ensure_coverage()
    assert len(obj.data['subtasks'][0]['coverage'])==24 and obj.data['subtasks'][0]['coverage_overflow']


@pytest.mark.parametrize('field',['饱和密度','重叠或净距','T=(A/4000㎡)×2','范围（甲、乙）'])
def test_non_enumeration_fields_are_not_inferred(field):
    assert _literal_fields(field)==[field]


def test_original_lower_bound_cannot_be_deleted_and_binding_does_not_reset_state():
    case=simple_case('读取甲站、乙站的材料、厚度。');obj=ledger(case);obj.ensure_coverage()
    obj.data['subtasks'][0]['coverage'][0].update(state='source_candidate',evidence_ids=['old'])
    case['arguments']['steps'][0]['coverage']=[dict(object='甲站',field='材料')]
    out=obj.plan(TaskPlan.model_validate(case['arguments']))
    assert len(out['subtasks'][0]['coverage'])==4
    first=out['subtasks'][0]['coverage'][0]
    assert first['state']=='source_candidate' and first['evidence_ids']==['old'] and first['question_binding']


def test_tool_feedback_exposes_real_semantic_and_schema_errors(workspace,demo_bytes):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    drawing=workspace.ingest('r1.dxf',demo_bytes)
    request=ChatRequest(snapshot_id=drawing['id'],question='读取甲站的材料和厚度。',mode='agent',runtime='responses_subscription',model_profile='siwc_sol')
    runner=AgentRunner(workspace,request)
    args=simple_case()['arguments'];args['steps'][0]['coverage'][0]['field']='不存在的字段'
    result=json.loads(runner.invoke_tool('plan_task',args))
    assert result['error']['code']=='PLAN_VALIDATION_FAILED'
    assert result['error']['issues'][0]['path']=='steps.0.coverage.0.field'
    assert not runner.task.data['planned'] and not request.source_read_batching
    args['steps'][0]['coverage'][0]['field']=123
    result=json.loads(runner.invoke_tool('plan_task',args))
    assert result['error']['issues'][0]['path']=='steps.0.coverage.0.field'
    assert result['error']['issues'][0]['code']=='string_type'
    result=json.loads(runner.invoke_tool('plan_task',simple_case()['arguments']))
    assert 'error' not in result and runner.task.data['planned']


def test_dependency_errors_identify_the_field():
    case=simple_case();case['arguments']['steps'][0]['depends_on']=['q99']
    with pytest.raises(PlanValidationError) as caught:ledger(case).plan(TaskPlan.model_validate(case['arguments']))
    assert caught.value.issues[0]['path']=='steps.0.depends_on.0'
