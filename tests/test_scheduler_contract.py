"""Selection, tool exposure and stopping share the same bounded schedule."""
import json
from copy import deepcopy
import pytest
from cad_agent.analysis.focus import ToolsetRequest
from cad_agent.analysis.scheduling import execution_state, schedule
from cad_agent.analysis.tasks import TaskPlan
from test_task_orchestration import session, plan


def test_expansion_cap_cannot_hide_required_relation_tools(workspace):
    s,_=session(workspace);s.task.plan(plan(s))
    step=s.task.data['subtasks'][3]
    step['operations']=['relations']
    s.focus.expansions['q4']=2
    result=s.focus.expand(ToolsetRequest(family='geometry',gap='核对目标对象之间的实际关系与局部边界。'),['q4'])
    assert result['enabled'] is False
    work=[w for w in schedule(s) if w['operation']=='relations']
    assert work and work[0]['executable']
    assert 'query_building_relations' in work[0]['next_action']['tools']
    assert set(work[0]['next_action']['tools'])<=s.focus.names()
    s.task.select(work[0]['next_action']['task_ids'])
    assert s.focus.expansions['q4']==2


def test_budget_blocked_dependency_and_independent_work_are_distinct(workspace):
    s,_=session(workspace);s.task.plan(plan(s))
    upstream=s.task.data['subtasks'][0]
    dependent=s.task.data['subtasks'][1]
    upstream.update(calls=12,status='partial',gap_kind='budget_exhausted')
    dependent['operations']=['geometry']
    assert execution_state(s.task.data,'q1')['blocked_reason']=='subtask_tool_budget'
    state=execution_state(s.task.data,'q2')
    assert not state['executable'] and state['blocked_by']=='q1'
    assert state['gap_kind']=='budget_exhausted'
    assert 'q4' in s.task.runnable() and 'q2' not in s.task.runnable()
    with pytest.raises(ValueError,match='TASK_NOT_EXECUTABLE'):s.task.select(['q2'])
    pending=[p for p in schedule(s) if p['task_ids']==['q2']]
    assert pending and all(not p['executable'] for p in pending)
    assert not dependent['delivered']


def test_replanning_registered_bounded_and_preserves_progress(workspace):
    s,_=session(workspace);s.task.plan(plan(s))
    step=s.task.data['subtasks'][0]
    step.update(calls=12,evidence_ids=['existing-source'],operation_receipts=[{'operation':'geometry','fulfilled':False}])
    step['operations']=['relations']
    old=deepcopy(step)
    out=s.focus.expand(ToolsetRequest(family='sources',tool_name='plan_task',gap='独立标题读取不应依赖耗尽预算的几何要求，需要修正依赖。'),['q2'])
    assert out['enabled'] and 'plan_task' in s.focus.names()
    revised=plan(s).model_dump()
    revised['steps'][1]['depends_on']=[]
    s.task.plan(TaskPlan.model_validate(revised))
    assert 'q2' in s.task.runnable()
    assert step['calls']==old['calls'] and step['evidence_ids']==old['evidence_ids']
    assert step['operation_receipts']==old['operation_receipts']
    assert 'relations' in step['operations']  # revisions cannot remove required work
    s.task.plan(TaskPlan.model_validate(revised))
    with pytest.raises(ValueError,match='修订次数'):s.task.plan(TaskPlan.model_validate(revised))
    assert 'plan_task' not in s.focus.names()
    assert s.task.data['plan_revisions']==2


def test_finish_only_demands_executable_pending_and_keeps_unknown(workspace):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    base,_=session(workspace)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=base.request.snapshot_id,question='核查对象相邻关系。',mode='agent',runtime='responses_subscription',model_profile='siwc_sol'))
    rid=next(r.region_id for r in workspace.regions(s.request.snapshot_id) if r.text=='三层平面图')
    s.invoke_tool('read_source_context',{'region_id':rid,'neighbors':0,'task_ids':['q1']})
    answer={'claims':[{'text':'对象相邻关系尚未查明。','state':'unknown','question_part_ids':['q1']}]}
    with pytest.raises(ValueError,match='TASK_WORK_PENDING.*next_action'):s.finish(json.dumps(answer))
    step=s.task.data['subtasks'][0];step.update(calls=12,status='partial',gap_kind='budget_exhausted')
    out=s.finish(json.dumps(answer))
    assert out['task']['status']=='partial'
    assert out['task']['subtasks'][0]['gap_kind']=='budget_exhausted'
    assert out['task']['subtasks'][0]['status']!='completed'
    assert out['claims'][0]['state']=='unknown'


def test_runtime_unavailable_pending_is_explicitly_blocked(workspace):
    s,_=session(workspace);s.task.plan(plan(s))
    from dataclasses import replace
    s.w.settings=replace(s.w.settings,provider='deepseek')
    s.task.data['scope_next_calls']=[{'tool':'render_region','operation':'scope_review','arguments':{},'task_ids':['q4'],'scope_id':'scope'}]
    s.task.data['active_scope_by_task']={'q4':'scope'}
    pending=[p for p in schedule(s) if p.get('tool')=='render_region']
    assert pending and not pending[0]['executable']
    assert pending[0]['gap_kind']=='unsupported_capability'
    assert 'render_region' not in {t['function']['name'] for t in s.offered_tools(planning_only=True)}
