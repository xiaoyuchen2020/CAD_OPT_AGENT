"""Isolated source overview: real DXF viewer, synthetic archives, no model calls.

Run with --output /tmp/cad-results-preview; open http://127.0.0.1:8878/source-overview/.
The fixture-only password is preview. Never point --output at a production data directory.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ezdxf
import uvicorn
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.analysis.general import DrawingIndex
from cad_agent.adapters.model import ModelGateway, ModelError
from cad_agent.fleet.app import create_app as fleet_app, FleetSettings
from source_overview_fixture import FixtureWorkflow, fixture_package
from cad_agent.fleet.source_overview_service import create_app as source_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8898)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output/'centre').exists():
        raise SystemExit('Use a new empty output directory for each fixture run.')
    def blocked(*a, **kw):
        raise ModelError('OFFLINE_FIXTURE', 'This fixture never calls a model.')
    ModelGateway.chat_turn = blocked
    ModelGateway.propose = blocked
    node = create_app(Settings(data_dir=args.output/'node', api_key='', api_base=''))
    workspace = node.state.workspace
    drawings, citations = [], []
    rows = [
        ('建筑设计总说明', ['垫层150mm，材料C15。', '地上内墙采用B06级A3.5蒸压加气混凝土砌块。', '外墙保温材料厚度80mm，燃烧性能A级。']),
        ('地下室建筑构造', ['地下室地面垫层100mm，材料C15。', '防水层采用两道3mm厚改性沥青防水卷材。', '施工要求及适用范围详各层平面图。']),
    ]
    for i, (name, lines) in enumerate(rows):
        doc = ezdxf.new('R2018'); doc.units = 4; m = doc.modelspace()
        m.add_lwpolyline([(0,0),(420,0),(420,297),(0,297)],close=True)
        m.add_text(name, dxfattribs={'insert':(25,265),'height':7})
        m.add_text('合成演示 · 不作为工程设计依据',dxfattribs={'insert':(25,250),'height':3})
        for index, line in enumerate(lines):
            m.add_text(line,dxfattribs={'insert':(25,220-index*40),'height':4})
            m.add_line((25,210-index*40),(390,210-index*40))
        path=args.output/f'drawing-{i}.dxf';doc.saveas(path)
        drawing=workspace.ingest(name+'（合成演示）.dxf',path.read_bytes(),synthetic=True);drawings.append(drawing)
        index=DrawingIndex(workspace,drawing['id'])
        for line in lines:
            region=next(r for r in index.regions if line in r.text)
            start=region.text.index(line);citations.append(index.evidence(region,start,start+len(line)))
    os.environ['CAD_FLEET_AGENT_A_URL']=f'http://127.0.0.1:{args.port-1}'
    centre=fleet_app(FleetSettings(args.output/'centre','preview-worker-token-xxxxxxxxxxxxxxxx',password='preview'))
    # This stub only supplies the isolated fixture's project association.
    with centre.state.exploration.connection(True) as db:
        db.execute('INSERT INTO exploration_runs VALUES(?,?,?,?,?)',('preview-exp','preview','source-preview','fixture',json.dumps(dict(project_id='preview',label='来源总览隔离演示'))))
    centre.state.exploration.get_run=lambda ident: {'project_id':'preview'}
    fleet=centre.state.fleet_store
    upload=fleet.add_upload('合成说明.dxf',(args.output/'drawing-0.dxf').read_bytes())
    job=fleet.create_job(dict(upload_ids=[upload['id']],question='提取墙体与垫层数据，复核设计做法和规则参考的差异',mode='facts'))
    with fleet.connection(True) as db:
        payload=json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
        payload['exploration_run_id']='preview-exp'
        db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),job['id']))
    active=fleet.claim('a')
    fleet.heartbeat(job['id'],'a',active['attempt_id'],snapshot_id=drawings[0]['id'])
    result=dict(answer='已提取两张图纸中的垫层、墙体与防水材料原文。一般做法与地下室做法分别保留来源，适用范围尚需确认。',
        citations=citations,claims=[dict(text=e['quote'],state='observed',evidence_ids=[e['evidence_id']]) for e in citations],
        missing_information=['尚未确认规则参考做法对本项目垫层的适用范围。'],task={'status':'partial'})
    content=json.dumps({'evidence':citations},ensure_ascii=False)
    fleet.finish(job['id'],'a',active['attempt_id'],result=result,delivery_status='partial',
        artifacts=dict(drawings=drawings,run={'id':'preview-run','status':'completed'},
            evidence_records=[dict(content=content,ref={'sha256':sha256(content.encode()).hexdigest()},sources={d['id']:d['sha256'] for d in drawings})]))
    archive=json.loads(fleet.archive(job['id']).read_text());package=fixture_package(archive,'preview')
    rule=dict(rule_id='DEMO-01',library_id='demo',version='1')
    store=FixtureWorkflow(args.output/'centre'/'rule-workflow')
    store.put_object('preview','fact_package',package,package['package_id'])
    store.create('preview','fixture',dict(package=package,rule=rule))
    (args.output/'fixture.json').write_text(json.dumps(dict(job_id=job['id'],attempt_id=active['attempt_id'],drawings=drawings),ensure_ascii=False,indent=2))
    thread=threading.Thread(target=lambda:uvicorn.run(node,host='127.0.0.1',port=args.port-1,log_level='warning'),daemon=True);thread.start()
    threading.Thread(target=lambda:uvicorn.run(source_app(args.output/'centre'),host='127.0.0.1',port=args.port+1,log_level='warning'),daemon=True).start()
    print(f'Source overview: http://127.0.0.1:{args.port+1}/source-overview/?job={job["id"]}',flush=True)
    print(f'Login: http://127.0.0.1:{args.port}/ (password: preview)',flush=True)
    uvicorn.run(centre,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':
    main()
