import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.run_journal import public_value, read_section, record_wire_request
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from test_agent import call, final, scripted_turns


def wait_run(workspace, run_id):
    for _ in range(300):
        r = workspace.store.get('chat_run', run_id)
        if r['status'] not in ('queued', 'running'):
            return r
        time.sleep(.02)
    raise AssertionError('run did not finish')


def bind(session, run_id='chat_fixture'):
    session.journal.bind(run_id)
    session.progress = lambda **fields: session.w.store.put('chat_run', run_id, dict(
        id=run_id, snapshot_id=session.request.snapshot_id, request=session.request.model_dump(),
        task_id=session.task.data['task_id'], run_events=session.journal.snapshot(), **{
            k:v for k,v in fields.items() if k!='run_events'}))
    return session.journal


def full_section(w, run_id, event_id, name):
    chunks=[]; start=0
    while True:
        page=read_section(w,run_id,event_id,name,start)
        chunks.append(page['content'])
        if not page['has_more']:return ''.join(chunks)
        start=page['next_start']


def test_background_journal_keeps_real_tool_results_and_validation_repair(tmp_path, demo_bytes, monkeypatch):
    app=create_app(Settings(llm_query_intent=False, data_dir=tmp_path/'journal'))
    with TestClient(app) as client:
        w=app.state.workspace
        d=w.ingest('demo.dxf', demo_bytes)
        state={'n':0}
        def turn(messages, tools, timeout):
            state['n']+=1
            # Exercise the same adapter hook that records the actual wire body.
            record_wire_request({'messages':messages,'tools':tools,'model':'fixture','max_tokens':6000})
            if state['n']==1:return call('search_sources', {'query':'内墙'}), {'total_tokens':10}
            if state['n']==2:
                packet=json.loads(messages[-1]['content'])
                return call('read_source_context', {'region_id':packet['items'][0]['region_id']},2), {'total_tokens':10}
            if state['n']==3:
                packet=json.loads(messages[-1]['content'])
                state['eid']=packet['items'][0]['evidence']['evidence_id']
                return {'role':'assistant','content':final('unknown-evidence')}, {'total_tokens':10}
            return {'role':'assistant','content':final(state['eid'])}, {'total_tokens':10}
        monkeypatch.setattr(w.gateway,'chat_turn',turn)
        response=client.post('/api/chat-runs',json={'snapshot_id':d['id'],'question':'内墙材料是什么？','mode':'agent'})
        assert response.status_code==202
        r=wait_run(w,response.json()['id'])
        assert r['status']=='completed', r.get('error')
        events=r['run_events']
        assert [e['status'] for e in events if e['kind']=='validation']==['failed','completed']
        assert events[-1]['kind']=='delivery'
        tools=[e for e in events if e['kind']=='tool']
        assert [e['tool'] for e in tools]==['search_sources','read_source_context']
        tool=tools[-1]
        assert {'arguments','execution','model_result','full_result'} <= tool['sections'].keys()
        route=f"/api/chat-runs/{r['id']}/events/{tool['id']}/sections/model_result"
        packet=client.get(route).json()
        eid=r['result']['model_context_codec']['aliases'][state['eid']]
        assert eid in packet['content']
        assert client.get(route+'?start=1&length=5').json()['content']==packet['content'][1:6]
        assert client.get(route+'?length=12001').status_code==422
        assert client.get(route+'?start=999999999').status_code==400
        assert eid in tool['evidence_ids']
        source=client.get(f"/api/chat-runs/{r['id']}/journal-evidence/{eid}")
        assert source.status_code==200 and source.json()['snapshot_id']==d['id']
        requests=[e for e in events if e['kind']=='model']
        alias_section=json.loads(full_section(w,r['id'],requests[-1]['id'],'context_codec'))
        assert alias_section['aliases'][state['eid']]==eid
        assert all(e['capture_level']=='wire_payload' for e in requests)
        actual=full_section(w,r['id'],requests[-1]['id'],'request')
        assert '答案未通过服务端校验' in actual and 'unknown-evidence' in actual
        assert '6000' in actual
        # Large bodies never ride along with the poll record.
        assert 'max_tokens' not in json.dumps(events)
        history=client.get(f"/api/drawings/{d['id']}/chat-runs").json()
        assert history['items'][0]['id']==r['id']
        w.store.put('chat_run','chat_other',dict(id='chat_other',run_events=[]))
        assert client.get(route.replace(r['id'],'chat_other')).status_code==404
        assert client.get(f"/api/chat-runs/chat_other/journal-evidence/{eid}").status_code==404
        exported=client.get(f"/api/chat-runs/{r['id']}/journal-export")
        rows=[json.loads(line) for line in exported.text.splitlines()]
        assert rows[0]['id']==r['id'] and any(row['type']=='section' for row in rows)
        assert 'execution_token' not in exported.text


def test_private_reasoning_images_and_credentials_never_persist(workspace, demo_bytes):
    d=workspace.ingest('demo.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='图名',mode='agent'))
    j=bind(s)
    payload={'input':[{'type':'reasoning','encrypted_content':'PRIVATE_CIPHER',
                      'content':'PRIVATE_THOUGHT','summary':[{'type':'summary_text','text':'公开说明'}]},
                     {'type':'input_image','image_url':'data:image/png;base64,PRIVATE_PIXELS'}],
             'authorization':'Bearer PRIVATE_TOKEN', 'arguments':json.dumps({'api_key':'PRIVATE_KEY'}),
             'url':'https://example.test/?token=PRIVATE_QUERY'}
    assert j.model_call(payload, lambda:{'reasoning_content':'PRIVATE_RESPONSE','content':'公开返回'})['content']=='公开返回'
    records=workspace.store.list('run_section')
    text=json.dumps(records,ensure_ascii=False)
    assert 'PRIVATE_' not in text
    assert '公开说明' in text and '公开返回' in text
    # Logging does not mutate what is sent to the provider.
    assert payload['input'][0]['encrypted_content']=='PRIVATE_CIPHER'


def test_live_cancel_keeps_request_and_marks_unfinished_event(workspace, demo_bytes, monkeypatch):
    d=workspace.ingest('demo.dxf',demo_bytes)
    entered, release=threading.Event(),threading.Event()
    def turn(messages, tools, timeout):
        entered.set();release.wait(5)
        return {'role':'assistant','content':'{}'}, {'total_tokens':1}
    monkeypatch.setattr(workspace.gateway,'chat_turn',turn)
    r=workspace.start_chat(ChatRequest(snapshot_id=d['id'],question='图名',mode='agent'))
    try:
        assert entered.wait(5)
        live=workspace.store.get('chat_run',r['id'])
        model=next(e for e in live['run_events'] if e['kind']=='model')
        assert model['status']=='running' and model['sections']['request']
        workspace.cancel_chat(r['id'])
    finally:release.set()
    ended=wait_run(workspace,r['id'])
    assert ended['status']=='cancelled'
    assert ended['run_events'] and not any(e['status']=='running' for e in ended['run_events'])


def test_section_pagination_integrity_and_explicit_truncation(workspace, demo_bytes, monkeypatch):
    import cad_agent.analysis.run_journal as module
    d=workspace.ingest('demo.dxf',demo_bytes)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='图名',mode='agent'))
    j=bind(s)
    monkeypatch.setattr(module,'MAX_SECTION_CHARS',30)
    e=j.start('input','测试');j.section(e,'context','汉字'*40,'测试正文');j.end(e)
    page=read_section(workspace,j.run_id,e['id'],'context',0,13)
    assert page['truncated'] and page['original_chars']==80
    assert page['content']=='汉字汉字汉字汉字汉字汉字汉' and page['next_start']==13
    ref=e['sections']['context'];record=workspace.store.get('run_section',ref['id'])
    workspace.store.put('run_section',ref['id'],record|{'text':'tampered'})
    with pytest.raises(ValueError,match='完整性'):read_section(workspace,j.run_id,e['id'],'context')


def test_subscription_records_final_wire_input_and_public_output(workspace, demo_bytes, monkeypatch):
    from test_siwc import make_session, connected_lease, response
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    s=make_session(workspace,demo_bytes);connected_lease(workspace,monkeypatch);j=bind(s);sent=[]
    def request(self,session,payload):
        sent.append(public_value(payload))
        if not session.trace:
            return response([{'type':'function_call','namespace':'cad','name':'get_drawing_overview',
                'call_id':'c1','arguments':'{}'}, {'type':'reasoning','encrypted_content':'HIDDEN_PAYLOAD'}])
        return response([{'type':'message','content':[{'type':'output_text','text':json.dumps({
            'claims':[{'text':'未取得所需图名原文。','state':'unknown','evidence_ids':[]}],
            'missing_information':['图名原文']},ensure_ascii=False)}]}])
    monkeypatch.setattr(ResponsesTransport,'request',request)
    s.run()
    models=[e for e in j.events if e['kind']=='model']
    assert len(models)==2 and all(e['capture_level']=='wire_payload' for e in models)
    assert 'HIDDEN_PAYLOAD' not in json.dumps(workspace.store.list('run_section'))
    wire=json.loads(full_section(workspace,j.run_id,models[0]['id'],'request'))
    assert wire==sent[0] and wire['model']==s.w.settings.model
