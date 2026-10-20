"""Manual batch opt-in must apply to exposure, advice, and actual dispatch."""
import json

import pytest
from pydantic import ValidationError

from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.focus import ToolsetRequest
from cad_agent.contracts import ChatRequest


@pytest.mark.parametrize('runtime', ['native', 'responses_subscription', 'codex_subscription', 'deepseek_harness', 'codex'])
@pytest.mark.parametrize('policy', ['efficient', 'legacy'])
@pytest.mark.parametrize('enabled', [False, True])
def test_exposure_and_guidance(workspace, demo_bytes, runtime, policy, enabled):
    d = workspace.ingest('option.dxf', demo_bytes)
    request = ChatRequest(snapshot_id=d['id'], question='内墙材料是什么？', mode='agent', runtime=runtime,
                          agent_policy=policy, **({'source_read_batching': True} if enabled else {}))
    assert request.source_read_batching is enabled
    s = AgentRunner(workspace, request)
    s.task.auto_plan()
    for focused in (False, True):
        names = {t['function']['name'] for t in s.offered_tools(planning_only=focused)}
        assert ('batch_read_sources' in names) is enabled
        assert 'read_source_context' in names
    if policy == 'efficient':
        assert ('batch_read_sources' in s.system_prompt) is enabled
    else:
        assert 'batch_read_sources' not in s.system_prompt


def test_disabled_dispatch_cannot_read_or_enable_itself(workspace, demo_bytes):
    d = workspace.ingest('option.dxf', demo_bytes)
    s = AgentRunner(workspace, ChatRequest(snapshot_id=d['id'], question='内墙材料是什么？', mode='agent'))
    s.task.auto_plan()
    before = json.dumps(s.task.data, sort_keys=True)
    rid = s.index.regions[0].region_id
    result = json.loads(s.invoke_tool('batch_read_sources', {'reads': [
        {'tool': 'read_source_context', 'arguments': {'region_id': rid}, 'task_ids': [s.task.parts[0]['id']]}
    ]}))
    assert result['error']['code'] == 'SOURCE_READ_BATCHING_DISABLED'
    assert s.calls == 1 and len(s.trace) == 1 and s.trace[0]['status'] == 'failed'
    assert not s.evidence and not s.observations and not getattr(s, 'source_read_batches', [])
    assert json.dumps(s.task.data, sort_keys=True) == before
    with pytest.raises(ValueError, match='批量读取已关闭'):
        s.focus.expand(ToolsetRequest(family='sources', tool_name='batch_read_sources', gap='需要读取两个独立的材料来源'), [s.task.parts[0]['id']])
    # The normal source reader still works after rejection.
    result = json.loads(s.invoke_tool('read_source_context', {'region_id': rid, 'neighbors': 0}))
    assert 'error' not in result and s.evidence


def test_option_is_explicit_boolean_and_survives_request_serialization():
    for value in ('false', 'true', 1):
        with pytest.raises(ValidationError):
            ChatRequest(snapshot_id='s', question='材料？', source_read_batching=value)
    for value in (False, True):
        request = ChatRequest(snapshot_id='s', question='材料？', source_read_batching=value)
        assert ChatRequest.model_validate_json(request.model_dump_json()).source_read_batching is value
