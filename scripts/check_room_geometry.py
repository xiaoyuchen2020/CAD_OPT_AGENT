"""Offline pilot artifacts. Optional real input is read-only; no model/network use."""
import argparse
import io
import json
import math
from hashlib import sha256
from pathlib import Path

import ezdxf
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.geometry.contracts import RoomMeasure
from cad_agent.geometry.service import GeometryService


def make_pilot(path):
    doc=ezdxf.new('R2018');doc.units=4;m=doc.modelspace()
    # Independent expected shell: 6 x 4 m. A one-metre doorway requires jambs.
    for x,y,w,h in [(0,0,6000,4000),(-200,-200,6400,4400)]:
        for a,b in [((x,y),(2000,y)),((3000,y),(x+w,y)),((x+w,y),(x+w,y+h)),
                    ((x+w,y+h),(x,y+h)),((x,y+h),(x,y))]:
            m.add_line(a,b)
    m.add_line((2000,0),(2000,-200));m.add_line((3000,0),(3000,-200))
    m.add_text('试点房间 A',dxfattribs={'insert':(700,2000),'height':200})
    m.add_text('6 × 4 m · 门洞闭合试点',dxfattribs={'insert':(0,5000),'height':200})
    # Second room: 5 x 3 m, with an unidentified interior closed object.
    for pts in [[(9000,0),(14000,0),(14000,3000),(9000,3000)],
                [(8800,-200),(14200,-200),(14200,3200),(8800,3200)],
                [(12000,1000),(12500,1000),(12500,1500),(12000,1500)]]:
        m.add_lwpolyline(pts,close=True)
    m.add_text('试点房间 B',dxfattribs={'insert':(9500,2000),'height':200})
    m.add_text('内部对象待确认',dxfattribs={'insert':(9000,4000),'height':200})
    doc.saveas(path)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',default='artifacts/room-geometry-pilot');ap.add_argument('--live',action='store_true');ap.add_argument('--dxf');ap.add_argument('--keyword',default='电井')
    args=ap.parse_args();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    source=Path(args.dxf).resolve() if args.dxf else out/'room-pilot.dxf'
    if not args.dxf and not source.exists():make_pilot(source)
    content=source.read_bytes();source_hash=sha256(content).hexdigest()
    w=Workspace(Settings(data_dir=out/'data'))
    try:
        drawing=w.ingest(source.name,content,synthetic=not bool(args.dxf));service=GeometryService(w,drawing['id'])
        regions=[r for r in w.regions(drawing['id']) if (args.keyword if args.dxf else '试点房间') in r.text]
        results=[]
        for region in regions[:5]:
            try:
                r=service.room(RoomMeasure(region_id=region.region_id))
                (out/(r['measurement_id']+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2))
                (out/(r['measurement_id']+'.svg')).write_text(service.svg(r['measurement_id']))
                results.append(service.summary(r))
            except ValueError as exc:
                results.append({'region_id':region.region_id,'error':str(exc),'status':'input_unresolved'})
        assert sha256(source.read_bytes()).hexdigest()==source_hash
        report=dict(source=str(source),source_sha256=source_hash,snapshot_id=drawing['id'],synthetic=not bool(args.dxf),
                    evidence_class='local_geometry_pilot_not_engineering_acceptance',
                    geometry_complete=service.index.data['complete'],geometry_diagnostics=service.index.data['diagnostics'][:30],
                    matching_label_regions=len(regions),tested_label_regions=len(results),results=results)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:report[k] for k in ('snapshot_id','matching_label_regions','tested_label_regions','geometry_complete')}))
        print(json.dumps([{'status':r['status'],'area_m2':r.get('area_m2'),'diagnostics':r.get('diagnostics',r.get('error'))} for r in results],ensure_ascii=False))
        if not args.dxf:
            by_label={r.get('label'):r for r in results}
            assert by_label['试点房间 A']['status']=='measured' and by_label['试点房间 A']['area_m2']==24
            assert by_label['试点房间 B']['status']=='needs_review' and by_label['试点房间 B']['area_m2'] is None
        if args.live:
            if args.dxf:
                raise ValueError("Live checks accept synthetic input only.")
            from dataclasses import replace
            from cad_agent.analysis.agent import AgentRunner
            from cad_agent.contracts import ChatRequest
            from cad_agent.adapters.model import ModelError
            live=Workspace(replace(Settings.load(),data_dir=out/'data'))
            try:
                runner=AgentRunner(live,ChatRequest(snapshot_id=drawing['id'],question=f'已选中的房间名称为试点房间 A，来源区域 ID 为 {next(r.region_id for r in regions if r.text=="试点房间 A")}。请调用房间测量工具分析净边界与净面积，说明门洞处理方式，并引用测量观测依据。',mode='agent'))
                original_finish=runner.finish
                rejected=[]
                def checked_finish(content):
                    try:return original_finish(content)
                    except ValueError as exc:
                        rejected.append({'candidate':content,'error':str(exc)})
                        raise
                runner.finish=checked_finish
                try:
                    result=runner.run()
                    live_report={'status':'completed','synthetic':True,'selected_region_context':True,'result':result,'rejected':rejected}
                    live_report['measurement_tool_called']=any(t['tool']=='measure_room' and t['status']=='completed' for t in result['trace'])
                    live_report['measurement_observation_cited']=any(o['tool'] in ('measure_room','get_measurement_evidence') for o in result.get('tool_observations',[]))
                    print(json.dumps({'live_status':'completed','answer':result['answer']},ensure_ascii=False))
                except ModelError as exc:
                    live_report={'status':'failed','error_code':exc.code,'synthetic':True,'trace':runner.trace,'rejected':rejected}
                    print(json.dumps({'live_status':'failed','error_code':exc.code}))
                (out/'live-model.json').write_text(json.dumps(live_report,ensure_ascii=False,indent=2))
            finally:live.close()
    finally:w.close()

if __name__=='__main__':main()
