"""Object/field omissions cannot be mistaken for subscription completeness."""
import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.tasks import TaskPlan, PlanStep, CoverageTarget, coverage_targets
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime


def runner(workspace, demo_bytes, question='请读取甲站、乙站的材料、厚度。'):
    drawing=workspace.ingest('coverage.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=drawing['id'],question=question,
                  mode='agent',runtime='codex_subscription',model_profile='codex_sol'))
    if s.task.needs_plan:s.task.auto_plan()
    return s


def read(s, quote, ids=None, truncated=False):
    ids=ids or [s.task.parts[0]['id']]
    eid='ev_'+str(len(s.evidence))
    evidence={eid:{'evidence_id':eid,'quote':quote,'region_id':'region_'+eid,'text_span':[0,len(quote)]}}
    result={'items':[{'relationship':'target','next_start':10 if truncated else None,'evidence':evidence[eid]}]}
    s.evidence.update(evidence)
    s.task.record(ids,'read_source_context',result,evidence,{})
    s.focus.record('read_source_context',ids,result)
    return eid


@pytest.mark.parametrize('question,objects',[
    ('请查找汽车库、设备机房、住宅公共部位的顶棚做法。',{'汽车库','设备机房','住宅公共部位'}),
    ('核对数据中心、实验大厅、仪器室的围护构造。',{'数据中心','实验大厅','仪器室'}),
    ('请读取北侧展廊、南侧展廊的饰面。',{'北侧展廊','南侧展廊'}),
])
def test_explicit_lists_are_question_owned_not_fixed_room_dictionary(question,objects):
    assert {c['object'] for c in coverage_targets(question)}==objects


def test_one_read_leaves_other_objects_and_fields_scheduled(workspace,demo_bytes):
    s=runner(workspace,demo_bytes)
    read(s,'甲站材料为铝板。')
    pending=s.task.pending_work()
    assert {(p['object'],p['field']) for p in pending}=={('甲站','厚度'),('乙站','材料'),('乙站','厚度')}
    assert not s.focus.review_ready()
    assert 'read_source_context' in s.focus.names()
    s.usage=[{'total_tokens':32000}]
    assert CodexSubscriptionRuntime().stop_reason(s) is None


def test_all_read_candidates_do_not_become_actual_completion(workspace,demo_bytes):
    s=runner(workspace,demo_bytes)
    read(s,'甲站材料为铝板，厚度3mm；乙站材料为钢板，厚度4mm。')
    assert not s.task.pending_work()
    assert s.focus.review_ready()  # Allow grounded final delivery, not an automatic stop.
    assert {c['state'] for c in s.task.coverage_summary()['requirements'][0]['items']}=={'source_candidate'}
    s.usage=[{'total_tokens':32000}]
    assert CodexSubscriptionRuntime().stop_reason(s) is None


def test_unrecognized_grammar_and_aliases_do_not_complete_complex_work(workspace,demo_bytes):
    s=runner(workspace,demo_bytes,'调查北廊饰面。请另核查南廊的尺寸。')
    ids=[p['id'] for p in s.task.parts]
    read(s,'北侧展廊为铝板。',ids)
    assert not s.task.pending_work()  # No recognized list or executable operation.
    s.usage=[{'total_tokens':32000}]
    assert not s.task.coverage_ready()
    assert CodexSubscriptionRuntime().stop_reason(s) is None


@pytest.mark.parametrize('question',['请核对门、窗各自材料及防水层顺序。','北侧展廊与南侧展廊分别采用什么饰面？'])
def test_ambiguous_list_does_not_invent_literal_objects_or_allow_early_stop(workspace,demo_bytes,question):
    s=runner(workspace,demo_bytes,question)
    read(s,'门采用木饰面。')
    summary=s.task.coverage_summary()['requirements'][0]
    assert summary['unparsed_list'] and not summary['items']
    s.usage=[{'total_tokens':32000}]
    assert CodexSubscriptionRuntime().stop_reason(s) is None


def test_plan_can_add_question_owned_targets_but_cannot_remove_lower_bound(workspace,demo_bytes):
    s=runner(workspace,demo_bytes)
    s.task.plan(TaskPlan(steps=[PlanStep(requirement_id='q1',kind='lookup',acceptance='逐对象核查',
        coverage=[CoverageTarget(object='甲站',field='材料')])]))
    assert len(s.task.data['subtasks'][0]['coverage'])==4
    other=runner(workspace,demo_bytes)
    with pytest.raises(ValueError,match='用户原问题'):
        other.task.plan(TaskPlan(steps=[PlanStep(requirement_id='q1',kind='lookup',acceptance='逐项核查',
            coverage=[CoverageTarget(object='新造房间',field='材料')])]))


def test_search_and_truncated_read_never_discharge_targets(workspace,demo_bytes):
    s=runner(workspace,demo_bytes)
    quote='甲站、乙站材料和厚度均见表。'
    evidence={'e':{'evidence_id':'e','quote':quote}}
    s.task.record(['q1'],'search_sources',{'items':[{'snippet':quote}]},evidence,{})
    read(s,quote,truncated=True)
    assert len(s.task.pending_work())==4


def test_unread_or_conflicting_field_remains_partial_after_other_fact_delivery(workspace,demo_bytes):
    s=runner(workspace,demo_bytes)
    eid=read(s,'甲站材料为铝板，厚度3mm；乙站材料为钢板，厚度4mm。')
    result={'claims':[{'text':'甲站材料为铝板。','state':'observed','evidence_ids':[eid]},
                      {'text':'乙站材料有冲突。','state':'conflict','evidence_ids':[eid]},
                      {'text':'乙站厚度尚不能确定。','state':'unknown','evidence_ids':[]}],
            'requirement_outcomes':[{'requirement_id':'q1','status':'fulfilled','gap_kind':'none','reason':'已读取'}]}
    task=s.task.finalise(result)
    states={(c['object'],c['field']):c['state'] for c in task['subtasks'][0]['coverage']}
    assert states=={('甲站','材料'):'addressed',('甲站','厚度'):'source_candidate',('乙站','材料'):'conflict',('乙站','厚度'):'unknown'}
    assert task['status']=='partial' and not s.task.coverage_ready()


def test_coverage_never_increases_hard_tool_time_or_token_limits(workspace,demo_bytes,monkeypatch):
    s=runner(workspace,demo_bytes);runtime=CodexSubscriptionRuntime()
    read(s,'甲站材料为铝板。')
    before=(runtime.exploration_budget(s),runtime.total_budget(s))
    s.calls=s.w.settings.agent_max_tool_calls
    assert runtime.stop_reason(s)=='tool_call_budget'
    s.calls=1
    monkeypatch.setattr(s,'_remaining',lambda:10)
    assert runtime.stop_reason(s)=='subscription_answer_time_reserve'
    monkeypatch.setattr(s,'_remaining',lambda:300)
    s.usage=[{'total_tokens':before[0]}]
    assert runtime.stop_reason(s)=='subscription_exploration_budget'
    assert before==(runtime.exploration_budget(s),runtime.total_budget(s))


def test_concrete_block_does_not_mark_fact_complete(workspace,demo_bytes):
    s=runner(workspace,demo_bytes)
    read(s,'甲站材料为铝板。')
    step=s.task.data['subtasks'][0]
    step.update(status='blocked_capability',reason='subtask_tool_budget_or_repetition')
    from cad_agent.analysis.scheduling import schedule
    assert s.task.coverage_ready() and s.task.pending_work()
    assert not any(item['executable'] for item in schedule(s))
    assert all(c['state']!='addressed' for c in step['coverage'])
