import json
from types import SimpleNamespace
import pytest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.tool_registry import TOOL_SPECS, capability_manifest, runtime_supports, tool_definitions
from cad_agent.analysis.tool_metadata import GROUPS, READS, CONTROL, OPERATION_TOOLS
from cad_agent.analysis.telemetry import PhaseMetrics
from cad_agent.contracts import ChatRequest
from cad_agent.execution import ExecutionCancelled


def test_capability_registry_has_no_dangling_groups_or_operations():
    names=set(TOOL_SPECS)
    assert set().union(*GROUPS.values(), *OPERATION_TOOLS.values(), READS, CONTROL) <= names
    manifest=capability_manifest()
    assert {r['name'] for r in manifest} == names
    assert all(r['input_schema']['type']=='object' for r in manifest)
    assert not next(r for r in manifest if r['name']=='render_region')['supported']
    assert runtime_supports('render_region','codex','responses_subscription')
    assert not runtime_supports('render_region','deepseek_harness','responses_subscription')
    assert not runtime_supports('finish_analysis','codex','responses_subscription')


def test_tool_schemas_filter_unsupported_and_are_fresh():
    s=SimpleNamespace(request=SimpleNamespace(runtime='native'),w=SimpleNamespace(settings=SimpleNamespace(provider='deepseek')),
                      focused_enabled=True,task=SimpleNamespace(complex=False))
    definitions=tool_definitions(True,s)
    names={x['function']['name'] for x in definitions}
    assert 'render_region' not in names and 'finish_analysis' in names
    definitions[0]['function']['parameters'].clear()
    assert tool_definitions(True,s)[0]['function']['parameters']['type']=='object'


def test_unsupported_control_is_denied_before_task_mutation(workspace,demo_bytes):
    d=workspace.ingest('demo.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='读取图纸',mode='agent',runtime='codex'))
    before=json.dumps(s.task.data,sort_keys=True)
    result=json.loads(s.invoke_tool('request_toolset',{'family':'sources','gap':'需要补充当前图纸的适用范围'}))
    assert result['error']['code']=='INVALID_TOOL_INPUT'
    assert json.dumps(s.task.data,sort_keys=True)==before


def test_timings_record_failure_without_payload():
    ticks=iter([0.,1.,3.,4.,7.,8.])
    m=PhaseMetrics(lambda:next(ticks))
    with m.measure('model_wait'):pass
    with pytest.raises(ValueError):
        with m.measure('answer_validation'):raise ValueError('private source text')
    report=m.snapshot()
    assert report['phases']['model_wait']['seconds']==2
    assert report['phases']['answer_validation']['failures']==1
    assert 'private' not in json.dumps(report)


def test_session_honors_cancellation_before_tool(workspace,demo_bytes):
    d=workspace.ingest('demo.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='读取图纸',mode='agent'))
    class Cancel:
        def check_cancelled(self):raise ExecutionCancelled('cancelled')
        def before_tool(self,*_):self.check_cancelled()
    s.execution=Cancel()
    with pytest.raises(ExecutionCancelled):s.invoke_tool('get_drawing_overview',{})
    assert not s.trace and not s.evidence
