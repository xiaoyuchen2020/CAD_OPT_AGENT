"""Isolated UI fixture: real local CAD operations, scripted model, no paid calls.

Choose `默认 · offline-ui-fixture` in the UI before testing Agent questions.
Questions containing 模拟慢速 or 模拟失败 exercise cancellation/error states.
"""
import argparse
import json
import re
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest, ExtractionRequest
from cad_agent.adapters.model import ModelError, ModelGateway
from cad_agent.demo import create_demo
from scripts.check_room_geometry import make_pilot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8781)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=args.output/'data', model='offline-ui-fixture',
        api_base='http://127.0.0.1:1', api_key='offline-fixture', protocol='openai',
        llm_query_intent=False, source_record_workflow=False)
    app = create_app(settings)
    w = app.state.workspace

    def blocked(*args, **kwargs):
        raise ModelError('OFFLINE_FIXTURE', '离线验收不调用外部模型。')
    ModelGateway.chat_turn = blocked
    ModelGateway.propose = blocked

    def turn(messages, tools, timeout):
        current = next((m.get('content', '') for m in reversed(messages)
                        if m['role'] == 'user'), '')
        if isinstance(current, str) and '模拟失败' in current:
            raise ModelError('UI_TEST_FAILURE', '离线验收：模拟模型服务失败。')
        if isinstance(current, str) and '模拟慢速' in current:
            time.sleep(12)
        packets = [m for m in messages if m['role'] == 'tool']
        def call(name, arguments):
            return {'role':'assistant', 'content':None, 'tool_calls':[{
                'id':f'fixture-{len(packets)}', 'type':'function',
                'function':{'name':name,'arguments':json.dumps(arguments)}}]}, {'total_tokens':10}
        if not packets:
            return call('search_sources', {'query':'内墙'})
        packet = json.loads(packets[-1]['content'])
        (args.output/'last-tool-packet.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2))
        items = packet.get('items', [])
        if items and items[0].get('evidence'):
            evidence = items[0]['evidence']
            quote = evidence.get('quote')
            if quote is None:
                for group in packet.get('source_groups', []):
                    match = re.search(r'\[' + re.escape(evidence['evidence_id']) + r' [^\]]+\] (".*")', group['text'])
                    if match:
                        quote = json.loads(match.group(1))
                        break
            if quote is None:
                raise ModelError('UI_FIXTURE_SOURCE', '未找到已读取的原文。')
            return {'role':'assistant','content':json.dumps({'claims':[{
                'text':quote, 'state':'observed',
                'evidence_ids':[evidence['evidence_id']]}],
                'missing_information':[]}, ensure_ascii=False)}, {'total_tokens':10}
        if items and items[0].get('region_id'):
            return call('read_source_context', {'region_id':items[0]['region_id']})
        raise ModelError('UI_FIXTURE_PACKET', '脚本模型未识别本次工具结果。')

    w.gateway.chat_turn = turn
    demo_file = args.output/'ui-demo.dxf'
    create_demo(demo_file)
    drawing = w.ingest('验收 · 建筑设计说明.dxf', demo_file.read_bytes(), synthetic=True)
    room_file = args.output/'ui-rooms.dxf'
    make_pilot(room_file)
    rooms = w.ingest('验收 · 房间平面.dxf', room_file.read_bytes(), synthetic=True)
    job = w.start_extract(ExtractionRequest(snapshot_id=drawing['id']))
    for _ in range(300):
        job = w.store.get('job', job['id'])
        if job['status'] not in ('queued', 'running'): break
        time.sleep(.05)
    run = w.start_chat(ChatRequest(snapshot_id=drawing['id'], question='内墙材料是什么？',
                                  mode='agent', model_profile='default', source_record_mode='agent'))
    for _ in range(300):
        run = w.store.get('chat_run', run['id'])
        if run['status'] not in ('queued', 'running'): break
        time.sleep(.05)
    result = {'status':run['status'], 'run_id':run['id'], 'snapshot_id':drawing['id'],
              'room_snapshot_id':rooms['id'], 'real_model':False,
              'actual_cad_tools':True, 'paid_api_fallback':False,
              'citations':len((run.get('result') or {}).get('citations', [])),
              'error':run.get('error')}
    (args.output/'fixture.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if run['status'] != 'completed':
        w.close()
        raise SystemExit(1)
    if args.serve:
        import uvicorn
        uvicorn.run(app, host='127.0.0.1', port=args.port)
    else:
        w.close()


if __name__ == '__main__':
    main()
