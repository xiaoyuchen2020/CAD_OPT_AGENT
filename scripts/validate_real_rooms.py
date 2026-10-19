"""Replay selected historical room anchors against this agent, never production rules.

Reference contours are used only AFTER measuring, for comparison and rendering.
"""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import time
import logging
logging.getLogger("ezdxf").setLevel(logging.ERROR)

from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.geometry.contracts import RoomMeasure
from cad_agent.geometry.service import GeometryService


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def run(root, phase, papers):
    rows=json.loads((root/'review_points.json').read_text())
    manifest=json.loads((root/'input_manifest.json').read_text())
    if papers: rows=[r for r in rows if r['paper_id'] in papers]
    out=root/phase;out.mkdir(exist_ok=True)
    # Offline batch explicitly accepts historical files up to 256 MB. Web defaults remain bounded.
    w=Workspace(Settings(data_dir=root/'data', max_upload_bytes=256*1024*1024))
    results=[]
    try:
        for pid in sorted({r['paper_id'] for r in rows}):
            pending=[r for r in rows if r['paper_id']==pid and not (out/(r['key']+'.json')).exists()]
            if not pending:continue
            start=time.monotonic();source=root/'input/dxf'/f'{pid}.dxf';content=source.read_bytes()
            expected=next(m for m in manifest if m['paper_id']==pid)
            digest=sha256(content).hexdigest();assert digest==expected['dxf_sha256']
            drawing=w.ingest(source.name,content);del content
            try:
                service=GeometryService(w,drawing['id']);service_error=None
            except Exception as exc:
                service=None;service_error=f'{type(exc).__name__}: {exc}'
            layout=next(l for l in drawing['layouts'] if l['name']=='Model')
            print('drawing',pid,'loaded',round(time.monotonic()-start,1),flush=True)
            for row in pending:
                target=out/(row['key']+'.json')
                opts=RoomMeasure(layout_id=layout['layout_id'],seed=row['measurement']['label'],unit_override='mm')
                try:
                    if service_error:raise RuntimeError(service_error)
                    record=service.room(opts)
                except Exception as exc:
                    record=dict(status='error',area_m2=None,candidate_area_m2=None,diagnostics=[f'{type(exc).__name__}: {exc}'],source_sha256=digest)
                report=dict(key=row['key'],paper_id=pid,dwg_name=row['source']['name'],source_sha256=digest,
                    historical_area_m2=row['measurement']['areaM2'],historical_review=row['review'],
                    unit_evidence='Historical measurement unitToM=0.001; explicit replay assumption, not inferred from header.',
                    record=record)
                dump(target,report);results.append(report)
                print(row['key'],record['status'],record.get('candidate_area_m2'),record['diagnostics'],flush=True)
            print('drawing_done',pid,round(time.monotonic()-start,1),flush=True)
        results=[json.loads((out/(r['key']+'.json')).read_text()) for r in rows]
        dump(out/'summary.json',dict(points=len(results),drawings=len({r['paper_id'] for r in results}),
            statuses=dict(Counter(r['record']['status'] for r in results)),
            rows=[dict(key=r['key'],paper_id=r['paper_id'],status=r['record']['status'],area_m2=r['record']['area_m2'],candidate_area_m2=r['record']['candidate_area_m2']) for r in results]))
    finally:w.close()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,default=Path('artifacts/ar030-real-room-validation-20261015'));ap.add_argument('--phase',default='after');ap.add_argument('--papers',type=int,nargs='*');a=ap.parse_args();run(a.root.resolve(),a.phase,a.papers)
