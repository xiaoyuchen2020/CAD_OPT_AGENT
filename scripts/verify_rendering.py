"""Rebuild only display caches for the four fixed real DXFs and compare evidence.

Run: .venv/bin/python -m scripts.verify_rendering
Outputs stay in artifacts/render-light-20261015 (or --output). No model calls.
"""
from pathlib import Path
from hashlib import sha256
import argparse
import json
import shutil
import sqlite3
import time

from cad_agent.config import Settings, ROOT
from cad_agent.service import Workspace
from cad_agent.cad.render import render_svg,RENDER_VERSION
from cad_agent.analysis.general import DrawingIndex
import importlib.util

OUT=ROOT/'artifacts/render-light-20261015'
SOURCE_DATA=ROOT/'artifacts/general-tools-v31-20261014/data'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    samples=json.loads((ROOT/'artifacts/real-validation-20261014/samples.json').read_text())
    connection=sqlite3.connect(f'file:{SOURCE_DATA}/index.sqlite3?mode=ro',uri=True)
    drawings=[json.loads(row[0]) for row in connection.execute("select data from records where kind='drawing'")]
    connection.close()
    ws=Workspace(Settings(data_dir=OUT/'data'))
    results=[]
    for sample in samples:
        d=next(d for d in drawings if d['sha256']==sample['sha256'])
        sid=d['id'];source=Path(sample['source_path'])
        assert sha256(source.read_bytes()).hexdigest()==sample['sha256']
        dest=ws.store.artifact_dir(sid)
        before={}
        for name in ('source.dxf','scene.json','entities.json','regions.json'):
            p=SOURCE_DATA/'drawings'/sid/name
            shutil.copy2(p,dest/name)
            before[name]=sha256((dest/name).read_bytes()).hexdigest()
        ws.store.put('drawing',sid,d)
        t=time.monotonic();scene=ws.preview_scene(sid);elapsed=time.monotonic()-t
        render=scene['render']
        assert render['version']==RENDER_VERSION
        assert render['complete'],render['diagnostics']
        assert before=={name:sha256((dest/name).read_bytes()).hexdigest() for name in before}
        assert ws.store.get('drawing',sid)==d
        # Every modeled DIMENSION/HATCH/SOLID in this sample is now encountered
        # by the drawing engine; composite virtual children may add more SOLIDs.
        baseline=json.loads((ROOT/'artifacts/render-audit-20261015/real-samples.json').read_text())
        old=next(r for r in baseline if r['sample']==sample['sample_id'])
        for kind in ('DIMENSION','HATCH','SOLID'):
            assert render['entered_types'].get(kind,0)>=old['expanded_counts'].get(kind,0)
        folder=OUT/sample['sample_id'];folder.mkdir(exist_ok=True)
        layout=next(l for l in render['layouts'] if l['name']=='Model')
        for kind in ('DIMENSION','HATCH','SOLID'):
            assert layout['entered_types'].get(kind,0)==old['expanded_counts'].get(kind,0)
        (folder/'overview.svg').write_text(render_svg(render,layout['layout_id'],layout['bbox']))
        texts=[i for i in render['items'] if i['type']=='text' and i['layout_id']==layout['layout_id']]
        index=DrawingIndex(ws,sid)
        spec=importlib.util.spec_from_file_location('legacy_render_index',ROOT/'artifacts/render-fix-20261015/before/general.py')
        legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
        old_index=legacy.DrawingIndex(ws,sid)
        regions=ws.regions(sid)
        selected=[]
        for name,kind in [('table','table_row'),('text','paragraph')]:
            candidates=[r for r in regions if r.kind==kind and r.layout_id==layout['layout_id'] and len(r.text)>(10 if kind=='table_row' else 40)]
            if not candidates:
                candidates=[r for r in regions if r.layout_id==layout['layout_id'] and len(r.text)>40]
            region=max(candidates,key=lambda r:(sum(weight for w,weight in [('砌体',10),('砂浆',5),('混凝土',3),('材料',1)] if w in r.text),min(len(r.text),700)))
            (folder/(name+'-before.svg')).write_text(old_index.svg(region.region_id,padding=.2))
            (folder/(name+'-after.svg')).write_text(index.svg(region.region_id,padding=.2))
            selected.append(dict(name=name,region_id=region.region_id,bbox=region.bbox,text=region.text[:300]))
        spacing=[]
        if sample['sample_id'] in ('sample-61','sample-62'):
            runs={i['handle']:i for i in texts if i['handle'] in ('FE6','FE7','FE8')}
            for left,right in [('FE6','FE7'),('FE7','FE8')]:
                gap=runs[right]['bbox'][0]-runs[left]['bbox'][2]
                assert gap>=0, (left,right,gap)
                spacing.append(dict(left=left,right=right,advance_gap=gap))
        result=dict(sample=sample['sample_id'],snapshot_id=sid,sha256=sample['sha256'],elapsed_seconds=round(elapsed,3),
                    scene_file_bytes=(dest/'render.json').stat().st_size,draw_items=len(render['items']),
                    text_runs=len(texts),glyphs=len(render.get('glyphs',{})),entered_types=render['entered_types'],
                    diagnostics=render['diagnostics'],unchanged_semantic_files=before,
                    model_entered_types=layout['entered_types'],regions=selected,authored_text_gaps=spacing)
        results.append(result)
        print(json.dumps({k:result[k] for k in ('sample','elapsed_seconds','draw_items','text_runs','glyphs','diagnostics')},ensure_ascii=False),flush=True)
    fixture=ROOT/'artifacts/render-audit-20261015/fixture.dxf'
    d=ws.ingest('render-audit-fixture.dxf',fixture.read_bytes())
    r=ws.rendering(d['id']);l=r['layouts'][0]
    (OUT/'fixture-after.svg').write_text(render_svg(r,l['layout_id'],l['bbox']))
    ws.close()
    (OUT/'verification.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=OUT)
    OUT=parser.parse_args().output.resolve()
    main()
