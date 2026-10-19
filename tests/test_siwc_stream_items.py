import json

import httpx
import pytest

from cad_agent.adapters.model import ModelError
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from test_siwc import make_session, connected_lease, response, sse
from test_agent import final


def event(kind, **values):
    return 'data: ' + json.dumps({'type': kind, **values}) + '\n\n'


@pytest.mark.parametrize('terminal_output', [False, True])
def test_completed_items_drive_loop_once_and_preserve_reasoning(workspace, demo_bytes, monkeypatch, terminal_output):
    session = make_session(workspace, demo_bytes)
    connected_lease(workspace, monkeypatch)
    rid = workspace.regions(session.request.snapshot_id)[0].region_id
    requests = []
    reasoning = {'type': 'reasoning', 'id': 'r1', 'summary': [], 'encrypted_content': 'private-reasoning'}
    def handle(req):
        body = json.loads(req.content); requests.append(body)
        if len(requests) == 1:
            items = [reasoning, {'type': 'function_call', 'namespace': 'cad', 'name': 'read_source_context',
                'arguments': json.dumps({'region_id': rid}), 'call_id': 'call1'}]
        else:
            assert reasoning in body['input']
            assert sum(i.get('type') == 'function_call_output' for i in body['input']) == 1
            items = [{'type': 'message', 'content': [{'type': 'output_text', 'text': final(next(iter(session.evidence)))}]}]
        text = ''.join(event('response.output_item.done', output_index=i, item=item) for i, item in enumerate(items))
        return httpx.Response(200, content=(text + sse(response(items if terminal_output else []))).encode())
    monkeypatch.setattr(ResponsesTransport, 'client', lambda self, timeout: httpx.Client(transport=httpx.MockTransport(handle)))
    result = session.run()
    assert len(requests) == 2 and len(session.trace) == 1 and result['citations']
    assert 'private-reasoning' not in json.dumps(session.runtime_diagnostics)
    assert result['model_requests'][0]['transport']['completed_item_count'] == 2


@pytest.mark.parametrize('problem', ['conflict', 'gap', 'failed', 'truncated', 'partial_only'])
def test_never_execute_partial_or_conflicting_items(workspace, demo_bytes, monkeypatch, problem):
    session = make_session(workspace, demo_bytes); connected_lease(workspace, monkeypatch)
    item = {'type': 'function_call', 'namespace': 'cad', 'name': 'search_sources', 'arguments': '{}', 'call_id': 'c1'}
    text = event('response.output_item.added' if problem == 'partial_only' else 'response.output_item.done',
        output_index=1 if problem == 'gap' else 0, item=item)
    if problem == 'conflict':
        text += sse(response([item | {'call_id': 'different'}]))
    elif problem == 'failed':
        text += sse(response([], status='failed'), 'response.failed')
    elif problem != 'truncated':
        text += sse(response([]))
    monkeypatch.setattr(ResponsesTransport, 'client', lambda self, timeout: httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=text.encode()))))
    with pytest.raises(ModelError): session.run()
    assert not session.trace
