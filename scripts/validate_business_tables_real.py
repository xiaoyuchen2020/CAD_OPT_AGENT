"""Frozen real-DWG validation, using the product's configured conversion/subscription.

Never copies credentials. Outputs raw sources, tool results and source/code hashes.
"""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/business-tables-real-e2e-20261026'
sys.path.insert(0,str(ROOT))
from cad_agent.config import Settings as OriginalSettings
BASE=OriginalSettings.load()


def save(name,data):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def setup():
    OUT.mkdir(exist_ok=True)
    frozen=OUT/'product'
    if not frozen.exists():
        shutil.copytree(ROOT/'cad_agent',frozen/'cad_agent',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        save('code-manifest.json',{str(p.relative_to(frozen)):sha(p) for p in frozen.rglob('*') if p.is_file()})
    for key in list(sys.modules):
        if key=='cad_agent' or key.startswith('cad_agent.'):del sys.modules[key]
    sys.path.insert(0,str(frozen));os.environ['PYTHONPATH']=str(frozen)
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    settings=replace(Settings(**asdict(BASE)),data_dir=OUT/'data')
    if OUT.name.endswith('-schedule'):
        # Archived DXF is 91 MiB. Keep all source entities unchanged; only raise
        # this isolated workspace's upload-size guard, not extraction settings.
        settings=replace(settings,max_upload_bytes=128*1024*1024)
        save('validation-config.json',{'max_upload_bytes':settings.max_upload_bytes,'default_max_upload_bytes':BASE.max_upload_bytes,'reason':'Full archived DXF exceeds 32 MiB; no source cropping or business parser changes.'})
    w=Workspace(settings)
    return w


def prepare(w,path):
    print('IMPORT',path.name,flush=True)
    drawing=w.ingest(path.name,path.read_bytes())
    save('input.json',dict(original_path=str(path),original_sha256=sha(path),drawing=drawing,
                           dxf_sha256=sha(w.store.artifact(drawing['id'],'source.dxf'))))
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.analysis.business_tables import extract_planning_metrics,extract_building_schedule
    from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest,BuildingScheduleRequest
    index=DrawingIndex(w,drawing['id'])
    save('raw-texts.json',index.scene['texts'])
    save('raw-tables.json',index.tables)
    for kind,method,schema in [('planning_metrics',extract_planning_metrics,PlanningMetricsRequest),
                              ('building_schedule',extract_building_schedule,BuildingScheduleRequest)]:
        result=method(w,drawing['id'],schema(limit=12),index)
        save(kind+'/discovery.json',result)
        print(kind,'candidates',result['total'],flush=True)
        for candidate in result['candidates']:
            tid=candidate['table_id'];pages=[];args=schema(table_id=tid)
            while True:
                page=method(w,drawing['id'],args,index);pages.append(page)
                if not page.get('has_more'):break
                args=schema(**page['next_actions'][0]['arguments'])
            save(kind+'/'+tid+'.json',pages)
            print(kind,tid,'pages',len(pages),'fields',sum(len(p.get('fields',[])) for p in pages),
                  'records',sum(len(p.get('records',[])) for p in pages),'diagnostics',pages[0].get('diagnostics'),flush=True)
    print('READY',drawing['id'],'tables',len(index.tables),flush=True)


def render(w):
    from cad_agent.cad.render import render_svg
    import resvg_py
    sid=json.loads((OUT/'input.json').read_text())['drawing']['id']
    drawing=w.store.get('drawing',sid)
    packet=w.rendering(sid)
    # Native geometry/text only: does not render business extractor output.
    if (OUT/'crops.json').exists():
        crops=json.loads((OUT/'crops.json').read_text())
    else:
        crops={'metrics':[296247,81030,296314,81123]}
    for name,box in crops.items():
        layout=drawing['layouts'][0]['layout_id']
        items=[i for i in packet['items'] if i['layout_id']==layout and i['bbox'][0]<=box[2] and i['bbox'][2]>=box[0] and i['bbox'][1]<=box[3] and i['bbox'][3]>=box[1]]
        scale=2000/max(box[2]-box[0],box[3]-box[1])
        svg=render_svg(packet|{'items':items},layout,box,width=round((box[2]-box[0])*scale),height=round((box[3]-box[1])*scale),raster=True)
        (OUT/(name+'.svg')).write_text(svg)
        (OUT/(name+'.png')).write_bytes(resvg_py.svg_to_bytes(svg_string=svg))
        print('RENDERED',name,len(items),flush=True)


def live(w,kind):
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.contracts import ChatRequest
    w.siwc_account.close()
    w.siwc_account=SiwcAccount(BASE.data_dir,BASE.siwc_max_concurrency)
    sid=json.loads((OUT/'input.json').read_text())['drawing']['id']
    questions={
      'planning_metrics':'请使用 extract_planning_metrics 通用入口提取当前图纸的用地面积、总建筑面积、计容和不计容建筑面积、绿地面积、建筑基底面积、容积率、建筑密度、绿地率及停车位。读取所有相关分页，保留原始单位及停车分类，列明来源、缺项或冲突。不要按比例计算图纸未明示的值。',
      'building_schedule':'请使用 extract_building_schedule 通用入口提取当前图纸建（构）筑物一览表的全部数据行，包括序号、名称、使用性质、层数、建筑高度、总建筑面积及计容、不计容、基底面积。读取所有分页，保留原始单位、合并单元格和来源；缺项不得推断或补齐。'}
    request=ChatRequest(snapshot_id=sid,question=questions[kind],mode='agent',runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False,source_read_batching=False)
    save('live/'+kind+'-request.json',request.model_dump())
    record=w.start_chat(request)
    print('STARTED',kind,record['id'],flush=True)
    prior=None; deadline=time.monotonic()+1200
    while True:
        record=w.store.get('chat_run',record['id'])
        progress=(record['status'],record.get('stage'),len(record.get('trace',[])))
        if progress!=prior:print('PROGRESS',kind,progress,flush=True);prior=progress
        if record['status'] not in ('queued','running'):break
        if time.monotonic()>deadline:w.cancel_chat(record['id']);deadline=float('inf')
        time.sleep(3)
    # Product run records contain requests/results/usage, never account credentials.
    save('live/'+kind+'-run.json',record)
    save('live/'+kind+'-task.json',w.store.get('agent_task',record['task_id']))
    print('FINISHED',kind,record['status'],record.get('error'),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path,nargs='?')
    parser.add_argument('--sample',choices=['primary','hospital','schedule'],default='primary')
    parser.add_argument('--render',action='store_true')
    parser.add_argument('--live',choices=['planning_metrics','building_schedule'])
    args=parser.parse_args()
    if args.sample!='primary':
        primary=OUT;OUT=OUT.with_name(OUT.name+'-'+args.sample);OUT.mkdir(exist_ok=True)
        if not (OUT/'product').exists():(OUT/'product').symlink_to(primary/'product',target_is_directory=True)
    w=setup()
    try:
        if args.source:prepare(w,args.source)
        if args.render:render(w)
        if args.live:live(w,args.live)
    finally:w.close()
