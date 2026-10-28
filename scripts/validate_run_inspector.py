"""Offline UI acceptance fixture: real CAD tools + scripted model, no network model calls."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.demo import create_demo


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--port',type=int,default=8768)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    # This fixture scripts the simple source-read loop, not query-intent planning
    # or deterministic source/table shortcuts.
    app=create_app(Settings(data_dir=args.output/'data',model='local-scripted-fixture',
                            llm_query_intent=False,source_record_workflow=False))
    w=app.state.workspace
    drawing_file=args.output/'synthetic-demo.dxf';create_demo(drawing_file)
    drawing=w.ingest('本地验证 · 合成示例.dxf',drawing_file.read_bytes())
    state={'n':0}
    def tool(name,arguments):
        return {'role':'assistant','content':None,'tool_calls':[{'id':f"call_{state['n']}",
            'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}]}
    def turn(messages,tools,timeout):
        state['n']+=1
        if state['n']==1:return tool('search_sources',{'query':'内墙'}),{'total_tokens':10}
        if state['n']==2:
            packet=json.loads(messages[-1]['content'])
            return tool('read_source_context',{'region_id':packet['items'][0]['region_id']}),{'total_tokens':10}
        if state['n']==3:
            packet=json.loads(messages[-1]['content']);state['evidence']=dict(packet['items'][0]['evidence'])
            # The model projection may group exact quotes separately from IDs.
            if 'quote' not in state['evidence']:
                eid=state['evidence']['evidence_id']
                state['evidence']['quote']=next(json.loads(line.split('] ',1)[1])
                    for group in packet.get('source_groups',[]) for line in group['text'].splitlines()
                    if line.startswith('['+eid+' '))
            evidence_id='unread-fixture-evidence'
        else:evidence_id=state['evidence']['evidence_id']
        return {'role':'assistant','content':json.dumps({'claims':[{
            'text':state['evidence']['quote'],'state':'observed','evidence_ids':[evidence_id]}],
            'missing_information':[]},ensure_ascii=False)},{'total_tokens':10}
    w.gateway.chat_turn=turn
    record=w.start_chat(ChatRequest(snapshot_id=drawing['id'],
        question='内墙材料是什么？',mode='agent'))
    for _ in range(500):
        run=w.store.get('chat_run',record['id'])
        if run['status'] not in ('queued','running'):break
        time.sleep(.02)
    checks=dict(status=run['status'],snapshot_id=drawing['id'],run_id=run['id'],
        event_count=len(run['run_events']),model='scripted_fixture',real_model=False,
        actual_cad_tools=True,paid_api_fallback=False,
        validation_states=[e['status'] for e in run['run_events'] if e['kind']=='validation'])
    (args.output/'validation.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    print(json.dumps(checks,ensure_ascii=False),flush=True)
    if checks['status']!='completed':
        w.close();raise SystemExit(1)
    if args.serve:
        import uvicorn
        uvicorn.run(app,host='127.0.0.1',port=args.port)
    else:w.close()


if __name__=='__main__':main()
