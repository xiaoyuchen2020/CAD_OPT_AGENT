"""Durable human input, applied only between model steps within the same source scope."""
from copy import deepcopy
import time
from uuid import uuid4
from cad_agent.adapters.model import ModelError


def enqueue(workspace, run_id, request):
    manager = workspace.execution
    with manager.transaction() as c:
        run = manager.read(c,'chat_run',run_id)
        if run['status'] not in ('queued','running') or run.get('cancel_requested') or run.get('execution_lease_until',0)<time.time():
            raise ValueError('只能修改正在执行且尚未取消的问题。')
        if run.get('input_closed'):
            raise ValueError('答案已进入发布，请完成后继续提问。')
        if run['request']['runtime'] not in ('native','responses_subscription'):
            raise ValueError('当前运行时不支持运行中修改问题。')
        revision = run.get('input_revision',0)
        if request.expected_revision != revision:
            raise ValueError('问题已更新，请刷新后重试。')
        if revision >= 4: raise ValueError('本轮修改次数已用完，请在结束后继续提问。')
        from cad_agent.contracts import ChatRequest
        from cad_agent.analysis.method_authorization import authorization
        original = ChatRequest.model_validate(run.get('effective_request',run['request']))
        pending = [v for v in run.get('pending_inputs',[]) if v['revision']>run.get('applied_input_revision',0)]
        retained = authorization(original)['mode']
        for entry in pending:
            original = original.model_copy(update={'question':entry['question'],'dynamic_methods':retained})
            retained = authorization(original)['mode']
        original = original.model_copy(update={'dynamic_methods':retained})
        candidate = original.model_copy(update={'question':request.question})
        permission = authorization(candidate)['mode']
        if permission == 'task_verified' and (not run.get('code_execution_enabled') or authorization(original)['mode'] != 'task_verified'):
            raise ValueError('运行中不能新增动态代码权限；请建立明确授权的新问题。')
        message = dict(id='input_'+uuid4().hex, revision=revision+1, question=request.question)
        run.setdefault('pending_inputs',[]).append(message)
        run['input_revision'] = revision+1
        manager.write(c,'chat_run',run_id,run)
        manager.events.append_in(c,run_id,'input.accepted',message)
        return dict(run_id=run_id,input_revision=revision+1,status='queued_for_next_step')


def has_pending(session):
    execution = getattr(session,'execution',None)
    if execution is None: return False
    run = execution.manager.store.get(execution.kind,execution.key)
    return run.get('input_revision',0) > getattr(session,'input_revision',run.get('applied_input_revision',0))


def apply_pending(session, messages):
    execution = getattr(session,'execution',None)
    if execution is None: return messages
    execution.check_cancelled()
    run = execution.manager.store.get(execution.kind,execution.key)
    applied = getattr(session,'input_revision',run.get('applied_input_revision',0))
    pending = [v for v in run.get('pending_inputs',[]) if v['revision'] > applied]
    if not pending: return messages
    item = pending[-1]  # Each entry is a complete replacement question; all entries remain in events.
    from cad_agent.analysis.method_authorization import authorization
    retained = authorization(session.request)['mode']
    for entry in pending:
        probe = session.request.model_copy(update={'question':entry['question'],'dynamic_methods':retained})
        retained = authorization(probe)['mode']
    request = session.request.model_copy(update={'question':item['question'], 'resume_task_id':None,
                                                 'dynamic_methods':retained})
    # Explicit denial in the replacement question takes priority over the retained grant.
    from cad_agent.analysis.tasks import TaskLedger
    fresh = TaskLedger(session.w,request)
    old = session.task.data
    preserved = {k:deepcopy(old[k]) for k in ('task_id','created_at','run_ids','revision','execution_owner',
        'execution_token','execution_lease_until','evidence_records','task_methods','task_programs','program_results',
        'tool_revisions','tool_version_routes','task_method_unbound_evidence','program_unbound_evidence','execution_policy_version') if k in old}
    fresh.data.update(preserved,status='running',input_revision=item['revision'],
                      evidence=deepcopy(session.evidence),observations=deepcopy(session.observations))
    fresh.data['input_history'] = old.get('input_history',[]) + [dict(revision=applied,question=old['question'])]
    # Changes to requirements do not refund usage, global calls, failures or final-repair allowance.
    from cad_agent.analysis.coverage import EvidenceCoverage
    from cad_agent.analysis.focus import TaskFocus
    manager = execution.manager
    with manager.transaction() as c:
        current = manager.read(c,execution.kind,execution.key)
        if current.get('execution_token') != execution.token or current.get('execution_lease_until',0)<time.time() or current.get('cancel_requested'):
            from cad_agent.execution import ExecutionCancelled
            raise ExecutionCancelled('任务取消或执行租约已失效。')
        saved = manager.read(c,'agent_task',old['task_id'])
        if saved.get('execution_token') != execution.token or saved.get('execution_lease_until',0)<time.time():
            from cad_agent.execution import ExecutionCancelled
            raise ExecutionCancelled('任务租约已失效。')
        fresh.data.update(revision=saved.get('revision',0)+1,execution_lease_until=saved['execution_lease_until'])
        current['applied_input_revision']=item['revision']
        current['effective_question']=item['question']
        current['effective_request']=request.model_dump()
        current['code_execution_enabled']=authorization(request)['mode']=='task_verified'
        current['code_execution_authorization']=authorization(request)['basis']
        manager.write(c,'agent_task',old['task_id'],fresh.data)
        manager.write(c,execution.kind,execution.key,current)
        manager.events.append_in(c,execution.key,'input.applied',item)
    session.request=request
    session.task=fresh
    session.input_revision=item['revision']
    session.coverage=EvidenceCoverage(request.question)
    session.coverage.parts=fresh.parts
    session.focus=TaskFocus(session)
    session.prepared_answer=None
    session.validation_issue=None
    session._validated_delivery_claims=[]
    session._validated_delivery_user_inputs=[]
    session.answer_field_issues=[]
    session.answer_repair_targets=[]
    session.room_batches={}
    session.highlight_action=None
    session.visual_images={}
    session.visual_bindings={}
    session.sent_image_ids=set()
    session.reviewed_image_ids=set()
    session.geometry_reviews={}
    session.policy.cache.clear()
    # Explicit global stop/budget causes remain; never replenish a stopped budget.
    return messages + [{'role':'user','content':
        '用户修改了当前问题。以下完整问题替代之前的问题，重新核对要求与来源适用性；图纸集合和授权边界保持原限制。\n'
        + request.question}, {'role':'developer','content':
        '当前要求版本：'+str(item['revision'])+'；之前的交付草稿和要求完成状态已失效。新任务：'
        + __import__('json').dumps(fresh.context(),ensure_ascii=False)}]


def require_current(session, run):
    if run.get('input_revision',0) != getattr(session,'input_revision',0):
        raise ModelError('INPUT_SUPERSEDED','发布前收到新的问题修正；旧答案未发布，修正已保存。')


def prepare_delivery(session):
    """Atomically close human input only for a result built from the latest revision.

    Publication still performs the ordinary execution and revision fence. A
    False result requests another existing driver step; no budget is refunded.
    """
    execution = getattr(session, 'execution', None)
    if execution is None:
        return True
    manager = execution.manager
    with manager.transaction() as c:
        run = manager.read(c, execution.kind, execution.key)
        if (run.get('execution_token') != execution.token
                or run.get('execution_lease_until', 0) < time.time()
                or run.get('cancel_requested')):
            from cad_agent.execution import ExecutionCancelled
            raise ExecutionCancelled('任务取消或执行租约已失效。')
        if run.get('input_revision', 0) != getattr(session, 'input_revision', 0):
            return False
        if not run.get('input_closed'):
            run['input_closed'] = True
            manager.write(c, execution.kind, execution.key, run)
            manager.events.append_in(c, execution.key, 'input.closed',
                                     {'input_revision': getattr(session, 'input_revision', 0)})
    return True
