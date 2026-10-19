"""Source review is produced by trusted rendering, never by generated code."""
from html import escape
import hashlib
import json
import tempfile
from pathlib import Path
import ezdxf
from ezdxf.addons import Importer
from cad_agent.cad.render import render_drawing, render_svg
from cad_agent.contracts import stable_id


def source_svg(service, run_id, overlay=False, *, local_origin=False):
    run = service.get(run_id)
    bundle = service.artifact('input',run['input_artifact_id']); service.source_check(bundle)
    result = service.result(run_id) if run.get('accepted_attempt') and run['status'] in ('awaiting_review','accepted','rejected') else {'validation':{}}
    source = service.w.store.artifact(service.sid, 'source.dxf')
    # Keep native root INSERTs and dependencies to avoid whole-drawing preview
    # clipping/dropout. This rendering subset is NEVER an algorithm input.
    doc = ezdxf.readfile(source)
    handles = sorted({v['entity_path'][0] for k in ('segments','texts','excluded_geometry') for v in bundle[k]})
    target = ezdxf.new(doc.dxfversion)
    importer = Importer(doc, target)
    importer.import_entities([doc.entitydb[h] for h in handles]); importer.finalize()
    # Keep display coordinates small: rasterizers lose thin overlays at large WCS
    # coordinates. Translate the rendering copy only, never the source or bundle.
    from ezdxf import transform
    from ezdxf.math import Matrix44
    a,b,c,d = bundle['bbox']; ox,oy = ((a+c)/2,(b+d)/2) if local_origin else (0,0)
    if local_origin:
        errors=transform.inplace(target.modelspace(),Matrix44.translate(-ox,-oy,0))
        if list(errors):raise ValueError('SOURCE_RENDER_TRANSFORM_FAILED')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'subset.dxf'; target.saveas(path)
        rendering = render_drawing(path, 'adaptation-review')
    pad = max(c-a,d-b)*.12
    svg = render_svg(rendering, stable_id('layout','adaptation-review','Model'), [a-ox-pad,b-oy-pad,c-ox+pad,d-oy+pad])
    if not rendering['items']:
        raise ValueError('SOURCE_RENDER_EMPTY')
    provenance = dict(source_sha256=bundle['source_sha256'], input_id=bundle['input_id'],
        root_handles=handles, render_diagnostics=rendering['diagnostics'], display_origin=[ox,oy], scope='source_root_subset_context_only')
    service.immutable('render_provenance', provenance)
    shapes = []
    if overlay:
        report = result['validation']
        for item in report.get('cells',[]) + report.get('items',[]):
            if 'vertices' in item:
                points=' '.join(f'{x-ox},{oy-y}' for x,y in item['vertices'])
                shapes.append(f'<polygon points="{points}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
            elif 'segments' in item:
                from shapely.geometry import LineString
                from shapely.ops import nearest_points
                lines=[]
                for segment in item['segments']:
                    p,q=segment['a'],segment['b'];lines.append(LineString([p,q]))
                    shapes.append(f'<path d="M {p[0]-ox} {oy-p[1]} L {q[0]-ox} {oy-q[1]}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
                p,q=nearest_points(*lines)
                shapes.append(f'<path d="M {p.x-ox} {oy-p.y} L {q.x-ox} {oy-q.y}" fill="none" stroke="#ed2789" stroke-width="2" vector-effect="non-scaling-stroke"/>')
            elif 'bbox' in item:
                x,y,z,w = item['bbox']
                shapes.append(f'<rect x="{x-ox}" y="{oy-w}" width="{z-x}" height="{w-y}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
            elif 'a' in item:
                p,q = item['a'], item['b']
                shapes.append(f'<path d="M {p[0]-ox} {oy-p[1]} L {q[0]-ox} {oy-q[1]}" fill="none" stroke="#ec7800" stroke-width="2" vector-effect="non-scaling-stroke"/>')
    service.source_check(bundle)
    return svg.replace('</svg>', ''.join(shapes)+'<desc>'+escape(json.dumps(provenance))+'</desc></svg>')


def page(service, run_id):
    run = service.get(run_id)
    prefix = f'/api/drawings/{service.sid}/adaptations/runs/{run_id}'
    return f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>动态方法复核</title>
<style>body{{font:16px system-ui;margin:24px}}.pair{{display:flex}}img{{width:49%;border:1px solid #ddd}}pre{{white-space:pre-wrap}}textarea{{width:90%;height:70px}}</style>
<h1>来源与候选结果复核</h1><p>自动验证只证明记录的结构/来源约束。请确认目标身份、分格或所选内容；工程含义仍需单独验收。</p>
<div class="pair"><img src="{prefix}/source.svg" alt="原图来源区域"><img src="{prefix}/overlay.svg" alt="候选叠加"></div>
<pre id="state">{escape(json.dumps(run,ensure_ascii=False,indent=2))}</pre>
<p>复核意见（必填）</p><textarea id="note"></textarea><p><button id="accept">确认当前范围</button> <button id="reject">拒绝</button> <button id="cancel">取消执行</button></p>
<p id="message"></p><script src="{prefix}/review.js"></script></html>'''


def script(service, run_id):
    run = service.get(run_id)
    from .contracts import SPECS
    prefix = f'/api/drawings/{service.sid}/adaptations/runs/{run_id}'
    config = json.dumps(dict(url=prefix, candidate=run.get('candidate_sha256'), scope=SPECS[run['request']['method']]['review_scope']))
    return f'''const c={config};
async function send(action,body){{const r=await fetch(c.url+'/'+action,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});document.getElementById('message').textContent=JSON.stringify(await r.json());}}
document.getElementById('accept').onclick=()=>send('reviews',{{candidate_sha256:c.candidate,decision:'accepted',scope:c.scope,note:document.getElementById('note').value}});
document.getElementById('reject').onclick=()=>send('reviews',{{candidate_sha256:c.candidate,decision:'rejected',scope:c.scope,note:document.getElementById('note').value}});
document.getElementById('cancel').onclick=()=>send('cancel',{{}});
'''
