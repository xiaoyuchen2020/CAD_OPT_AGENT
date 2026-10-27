"""Explicit current-question authorization, shared across exposure and execution."""
import json
from types import SimpleNamespace
import pytest
from cad_agent.analysis.method_authorization import authorization
from cad_agent.analysis import task_methods as tm
from cad_agent.analysis.tool_registry import tool_definitions
from cad_agent.contracts import ChatRequest
from test_table_recovery import recovery_case
from test_task_methods import session_for, baseline, mocks, develop


@pytest.mark.parametrize('question',[
    '允许使用动态代码', '允许动态代码执行。', '请读取做法，允许使用动态代码。',
    '我明确允许你使用动态代码。', '现有工具不足时允许使用动态代码',
    '必要时可以生成并执行动态代码', '允许Agent在读取时使用动态代码',
    '请使用动态代码处理局部原文', '这里是什么？允许使用动态代码。',
    '允许 Agent 使用动态代码', '允许在现有工具不足时使用动态代码',
])
def test_explicit_permission_overrides_unchecked_switch(question):
    result=authorization(SimpleNamespace(question=question,dynamic_methods='off'))
    assert result==dict(mode='task_verified',basis='explicit_question_permission')


@pytest.mark.parametrize('question',[
    '动态代码有哪些能力？','是否允许使用动态代码？','允许使用动态代码吗？',
    '请解释“允许使用动态代码”的含义','图纸写着：允许使用动态代码',
    '比如：允许使用动态代码','如果我说允许使用动态代码，会发生什么？',
    '> 允许使用动态代码\n请检查这段原文',
    '```\n允许使用动态代码\n```\n请检查这段代码',
    '“允许使用动态代码”','`允许使用动态代码`',
    '不确定是否应该使用动态代码', '请检查图纸',
])
def test_mention_question_and_quoted_material_do_not_authorize(question):
    assert authorization(SimpleNamespace(question=question,dynamic_methods='off'))['mode']=='off'


@pytest.mark.parametrize('question',[
    '不允许使用动态代码', '请不要使用动态代码', '禁止动态代码执行',
    '允许动态代码。但本轮禁止使用动态代码。',
])
def test_current_denial_overrides_checkbox_and_conflicting_permission(question):
    assert authorization(SimpleNamespace(question=question,dynamic_methods='task_verified'))==dict(mode='off',basis='explicit_question_denial')


def test_checkbox_still_works_and_quoted_denial_is_not_an_instruction():
    r=authorization(SimpleNamespace(question='请解释“禁止使用动态代码”',dynamic_methods='task_verified'))
    assert r==dict(mode='task_verified',basis='request_switch')


def test_question_permission_exposes_and_executes_without_checkbox(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch)
    s=session_for(workspace,recovery_case,mode='off',policy='efficient')
    s.request=s.request.model_copy(update={'question':'读取局部原文。允许使用动态代码。'})
    assert s.request.dynamic_methods=='off'  # Raw request is retained for audit.
    assert tm.TOOLS <= {t['function']['name'] for t in tool_definitions(session=s)}
    assert '本轮开启 task_verified' in s.system_prompt
    result=develop(s,baseline(s,recovery_case))
    assert result.get('admitted') and calls
    e=s.evidence[result['evidence'][0]['evidence_id']]
    answer=s.validate_answer(json.dumps({'claims':[dict(text=e['quote'],state='observed',evidence_ids=[e['evidence_id']])],'missing_information':[]},ensure_ascii=False))
    assert e['evidence_id'] in answer['claims'][0]['evidence_ids']


def test_denial_hides_tools_and_blocks_execution(workspace,recovery_case,monkeypatch):
    calls=mocks(monkeypatch);s=session_for(workspace,recovery_case)
    s.request=s.request.model_copy(update={'question':'读取原文。禁止使用动态代码。'})
    assert not tm.enabled(s)
    assert not tm.TOOLS & {t['function']['name'] for t in tool_definitions(session=s)}
    assert 'TASK_METHODS_DISABLED' in json.dumps(develop(s,'unused')) and not calls


def test_run_metadata_records_effective_question_permission(workspace,recovery_case,monkeypatch):
    monkeypatch.setattr(workspace.agent_executor,'submit',lambda *a,**k:None)
    req=ChatRequest(snapshot_id=recovery_case[0]['id'],question='读取局部原文。允许使用动态代码。',
                    mode='agent',runtime='responses_subscription',model_profile='siwc_sol')
    run=workspace.start_chat(req)
    assert run['request']['dynamic_methods']=='off'
    assert run['code_execution_enabled'] is True
    assert run['code_execution_scope']=='task_verified'
    assert run['code_execution_authorization']=='explicit_question_permission'
