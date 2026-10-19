"""Independent regressions for execution boundaries; synthetic files, no model."""
from types import SimpleNamespace
import pytest
from cad_agent.adapters.model import ModelError
from cad_agent.adapters.store import now
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.execution import ExecutionContext, ExecutionCancelled
from test_room_discovery import plan


def test_cancel_after_tool_work_records_completed_checkpoint(workspace, monkeypatch):
    _, _, drawing = plan(workspace, ['会议室'])
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取图纸布局'))
    run=dict(id='cancel-review',snapshot_id=drawing['id'],task_id=session.task.data['task_id'],status='running',created_at=now())
    workspace.execution.claim('chat_run',run,session.task.data)
    session.execution=ExecutionContext(workspace.execution,'chat_run',run['id'],run['execution_token'])
    session.progress=lambda **fields: workspace.execution.update('chat_run',run['id'],run['execution_token'],fields)
    real_execute=session.execute
    def execute_then_cancel(*args,**kwargs):
        result=real_execute(*args,**kwargs)
        workspace.execution.cancel(run['id'])
        return result
    monkeypatch.setattr(session,'execute',execute_then_cancel)
    with pytest.raises(ExecutionCancelled):
        session.invoke_tool('get_drawing_overview',{'view':'layouts'})
    task=workspace.store.get('agent_task',session.task.data['task_id'])
    assert task['observations'], 'The tool result was durably saved before cancellation surfaced.'
    saved=workspace.store.get('chat_run',run['id'])
    assert saved['cancel_requested']
    assert saved['tool_checkpoint']['status']=='completed', 'Persisted successful work must not retain a started-only checkpoint.'


def test_native_fallback_keeps_task_and_uses_native_capabilities(workspace, monkeypatch):
    from cad_agent.runtimes import registry
    _, _, drawing = plan(workspace, ['会议室'])
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取图纸布局',runtime='deepseek_harness',allow_fallback=True))
    original_task_id=session.task.data['task_id']
    def unavailable(*args,**kwargs): raise ModelError('UNAVAILABLE','fixture transport failure')
    monkeypatch.setattr(registry,'get_runtime',lambda name:SimpleNamespace(run=unavailable))
    captured={}
    def local_native(self,fallback,history=None):
        captured.update(task_id=fallback.task.data['task_id'],runtime=fallback.request.runtime)
        fallback.evidence['fallback-evidence']={'snapshot_id':drawing['id'],'quote':'会议室'}
        return {'usage':[],'claims':[],'citations':[]}
    monkeypatch.setattr(registry.NativeRuntime,'run',local_native)
    result=registry.run_agent(session)
    assert result['runtime_used']=='native' and result['fallback_used'] is True
    assert captured['task_id']==original_task_id, 'Fallback must continue the claimed task, not create an orphan task.'
    assert captured['runtime']=='native', 'Runtime capability gating must reflect the runtime actually executing.'
    assert 'fallback-evidence' in session.evidence, 'Publication uses the owning session and needs fallback evidence.'


def test_resume_task_rejects_changed_object_algorithm_context(workspace, monkeypatch):
    from cad_agent.analysis import object_registry
    _, args, drawing = plan(workspace, ['会议室'])
    request=ChatRequest(snapshot_id=drawing['id'],question='读取空间候选')
    session=CadAgentSession(workspace,request)
    session.invoke_tool('discover_rooms',args.model_dump())
    assert session.task.data['object_registry']['objects']
    session.task.data['status']='partial'
    session.task.save()
    monkeypatch.setattr(object_registry,'VERSION','object-registry-new-algorithm')
    with pytest.raises(ValueError,match='版本|算法|单位'):
        CadAgentSession(workspace,request.model_copy(update={'resume_task_id':session.task.data['task_id']}))
