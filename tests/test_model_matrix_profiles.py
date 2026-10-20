"""Matrix variants change only the allowlisted model and reasoning effort."""
import json
import httpx
import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from test_siwc import connected_lease, response, sse
from test_agent import final
from test_controlled_batch import batch


@pytest.mark.parametrize('profile,model,effort', [
    ('siwc_sol','gpt-6.1-sol','low'), ('siwc_sol_high','gpt-6.1-sol','high'),
    ('siwc_luna_high','gpt-6-luna','high')])
def test_matrix_payload_and_report(workspace, demo_bytes, monkeypatch, profile, model, effort):
    d=workspace.ingest('matrix.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='内墙材料是什么？',mode='agent',
        runtime='responses_subscription',model_profile=profile))
    connected_lease(workspace,monkeypatch)
    bodies=[]
    def handle(req):
        body=json.loads(req.content);bodies.append(body)
        assert body['model']==model and body['reasoning']['effort']==effort
        assert not any(t['name']=='batch_read_sources' for ns in body['tools'] for t in ns['tools'])
        if not s.evidence:
            output=[dict(type='function_call',namespace='cad',name='read_source_context',call_id='read',
                arguments=json.dumps({'region_id':s.index.regions[0].region_id,'neighbors':0}))]
        else:
            output=[dict(type='message',role='assistant',content=[dict(type='output_text',text=final(next(iter(s.evidence))))])]
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=sse(response(output,model=model)))
    monkeypatch.setattr(ResponsesTransport,'client',lambda self,timeout:httpx.Client(transport=httpx.MockTransport(handle)))
    result=s.run()
    assert result['citations'] and len(bodies)==2
    assert result['reasoning_effort']==effort and result['model']==model
    assert all(r['model']==model and r['reasoning_effort']==effort for r in result['model_requests'])
    config=batch.case_configuration({'model':'gpt-6.1-sol','model_profile':'siwc_sol'},
        dict(model_profile=profile,model=model,reasoning_effort=effort))
    assert config==dict(model_profile=profile,model=model,reasoning_effort=effort,source_read_batching=False)


@pytest.mark.parametrize('override',[{'model':'gpt-6-luna'},{'reasoning_effort':'high'},
    {'model_profile':'untrusted'},{'source_read_batching':'false'}])
def test_runner_rejects_mislabeled_variant(override):
    with pytest.raises(ValueError):
        batch.case_configuration(dict(model='gpt-6.1-sol',model_profile='siwc_sol'),override)
