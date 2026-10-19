"""Frozen local real-DXF regression; no source edits, model calls or platform jobs."""
import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
import logging
from pathlib import Path
import shutil
import sqlite3

from shapely.geometry import Point, Polygon, LineString
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.components.contracts import RelationQuery
from cad_agent.components.service import ComponentService
from cad_agent.geometry.source import native_source, scoped_geometry_index
from scripts.validate_components import render_review

CASES=[
    dict(name='property-bath',sid='snap_77b384ae399a2c5c69f5a23a',dwg='(6审)D1-D3楼_t3.dwg',bbox=[14500,-6500,20500,5000]),
    dict(name='ligong-east',sid='snap_e316a1345c7a5db25e19458f',dwg='(3审)16-10#楼_t3(1)-理工院改后（建筑已修改完）.dwg',bbox=[112000,-184000,128000,-170000]),
]


def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def verify_graph(graph,source):
    """Read persisted output independently: resolve sources and recompute containment."""
    objects={x['object_id'] for key in ('rooms','walls','openings') for x in graph[key]}
    assert all(r['source_id'] in objects and r['target_id'] in objects for r in graph['relations'])
    assert all(set(x['entity_ids'])<=source['entities'].keys() for key in ('rooms','walls','openings') for x in graph[key])
    rooms={r['object_id']:r for r in graph['rooms']};checks=[]
    for opening in graph['openings']:
        if opening['passage_status']!='supported_door_between_two_spaces':continue
        assert opening['opening_type']=='door_candidate' and len(opening['room_ids'])==2
        assert len(opening['sides'])==2 and all(len(s['room_ids'])==1 for s in opening['sides'])
        assert opening['sides'][0]['room_ids']!=opening['sides'][1]['room_ids']
        side_checks=[]
        for side in opening['sides']:
            room=rooms[side['room_ids'][0]];p=Polygon(room['boundary'],room.get('internal_rings',[]))
            hits=[p.contains(Point(x)) for x in side['probe_points']]
            assert sum(hits)>=2
            side_checks.append(dict(room_id=room['object_id'],probe_containment=hits,source_entity_count=len(room['entity_ids'])))
        arcs=[]
        for evidence in opening['classification_evidence']['swing_symbols']:
            arc=source['entities'][evidence['arc_id']][0]
            assert arc.dxftype()=='ARC'
            center=list(arc.ocs().to_wcs(arc.dxf.center))[:2]
            assert max(abs(a-b) for a,b in zip(center,evidence['hinge']))<1e-6
            assert 80<=(arc.dxf.end_angle-arc.dxf.start_angle)%360<=100
            arcs.append(dict(entity_id=evidence['arc_id'],handle=source['entities'][evidence['arc_id']][1],center_wcs=center,radius=arc.dxf.radius))
        assert arcs
        checks.append(dict(opening_id=opening['object_id'],bbox=opening['bbox'],width_m=opening['width_m'],sides=side_checks,native_arcs=arcs))
    for o in graph['openings']:
        if o['opening_type'] in ('window_candidate','conflict','unknown'):
            assert o['passage_status']=='not_established'
            assert not any(r.get('opening_id')==o['object_id'] and r['kind']=='door_connects_candidate_spaces' for r in graph['relations'])
    return checks


def detail(workspace,graph,path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as Patch
    opening=next(o for o in graph['openings'] if o['passage_status']=='supported_door_between_two_spaces')
    bounds=[15400,-1000,19800,4500]
    records=scoped_geometry_index(workspace,graph['snapshot_id'],graph['layout_id'],bounds,'mm').query(graph['layout_id'],bounds)
    fig,ax=plt.subplots(figsize=(8,10),dpi=150)
    for r in records:
        for p in r['paths']:ax.plot(*zip(*p),color='#858585',linewidth=.55)
    colors=['#0b8b74','#4674d2']
    rooms={r['object_id']:r for r in graph['rooms']}
    for i,side in enumerate(opening['sides']):
        room=rooms[side['room_ids'][0]]
        ax.add_patch(Patch(room['boundary'],fill=True,facecolor=colors[i],alpha=.12,edgecolor=colors[i],linewidth=2))
        for p in side['probe_points']:ax.scatter(*p,color=colors[i],s=18,zorder=8)
        ax.plot(*zip(*side['face_points']),color=colors[i],linewidth=3)
    ids={eid for s in opening['classification_evidence']['swing_symbols'] for eid in s['entity_ids']}
    for r in records:
        if r['entity_id'] in ids:
            for p in r['paths']:ax.plot(*zip(*p),color='#e07116',linewidth=2)
    ax.annotate('R1: sanitary fixtures (visual review)',(16400,3100),fontsize=10)
    ax.annotate('R2: property management (native label)',(16400,-450),fontsize=10)
    ax.annotate('D1: 1.15 m wall-gap candidate\narc + leaf + distinct spaces on both sides',(16600,2250),xytext=(17600,1400),arrowprops={'arrowstyle':'->'},fontsize=9)
    ax.set_xlim(bounds[0],bounds[2]);ax.set_ylim(bounds[1],bounds[3]);ax.set_aspect('equal')
    ax.set_title('Real DXF: door evidence and opposite-side probes\nCandidate topology; not a net-width or room-area approval',fontsize=11)
    fig.tight_layout();fig.savefig(path);plt.close(fig)


def main():
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,default=Path('artifacts/components-validation-current-20261015'));p.add_argument('--output',type=Path,default=Path('artifacts/opening-relations-final-20261015'));args=p.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True);archive=args.archive.resolve()
    logging.getLogger('ezdxf').setLevel(logging.ERROR)
    with sqlite3.connect(f"file:{archive/'data/index.sqlite3'}?mode=ro",uri=True) as db:
        drawings={r['id']:r for row, in db.execute("select data from records where kind='drawing'") if (r:=json.loads(row))}
    summary=[]
    for case in CASES:
        sid=case['sid'];d=drawings[sid];source_dir=archive/'data/drawings'/sid
        before=sha256((source_dir/'source.dxf').read_bytes()).hexdigest();assert before==d['sha256']
        w=Workspace(Settings(data_dir=out/'data'))
        try:
            for fn in ('source.dxf','scene.json','entities.json','regions.json'):shutil.copy2(source_dir/fn,w.store.artifact(sid,fn))
            w.store.put('drawing',sid,d);lid=next(l['layout_id'] for l in d['layouts'] if l['name']=='Model')
            options=RelationQuery(layout_id=lid,bbox=case['bbox'],view_kind='floor_plan')
            print(case['name'],'build',flush=True)
            graph=ComponentService(w,sid).build('relations',options)
            dump(out/f"{case['name']}.json",graph)
            graph=json.loads((out/f"{case['name']}.json").read_text())
            source=native_source(w,sid);checks=verify_graph(graph,source)
            dump(out/f"{case['name']}-checks.json",checks)
            render_review(w,sid,case['bbox'],graph,out/f"{case['name']}.png")
            if case['name']=='property-bath':
                assert len(checks)==1 and abs(checks[0]['width_m']-1.15)<.001
                detail(w,graph,out/'door-detail.png')
                # Keep the door, crop out most of the lower room: it must lose its passage.
                crop=ComponentService(w,sid).build('relations',options.model_copy(update={'bbox':[14500,1000,20500,5000]}))
                assert crop['openings'] and not any(o['passage_status']=='supported_door_between_two_spaces' for o in crop['openings'])
                dump(out/'clipped-room-negative.json',crop)
            elif case['name']=='ligong-east':
                assert any(o['opening_type']=='window_candidate' for o in graph['openings'])
            assert sha256((source_dir/'source.dxf').read_bytes()).hexdigest()==before
            row=case|dict(source_sha256=before,source_unchanged=True,scope='manually_selected_floor_plan',rooms=len(graph['rooms']),openings=len(graph['openings']),types=dict(Counter(o['opening_type'] for o in graph['openings'])),two_sided_door_connections=len(checks),diagnostics=graph['diagnostics'])
            summary.append(row);dump(out/'summary.json',summary);print(json.dumps(row,ensure_ascii=False),flush=True)
        finally:w.close()
    html=['<!doctype html><meta charset="utf-8"><title>门窗洞口与双侧空间验证</title><style>body{font:16px system-ui;max-width:1400px;margin:32px auto;color:#233640}img{max-width:100%}pre{white-space:pre-wrap;background:#f2f4f5;padding:20px}a{margin-right:20px}</style><h1>门窗洞口与双侧空间 · 真实图纸验证</h1><p>历史真实 DXF、冻结代码、本地回放。空间边界与门窗身份仍为候选；数量不代表识别准确率。源图未修改，不运行生产平台或模型问答。</p><h2>明确连接两个空间的门</h2><p>D1–D3：物业管理与上侧卫生间。卫生间用途依据原图洁具布置目视判断，原生文字未标注该用途。门开启弧和门扇、两侧墙面及探测点共同支持连接。1.15 m 为墙面开口候选宽，不能作为规范净宽。</p><img src="door-detail.png" alt="真实门洞的开启弧、门扇及双侧探测点"><p><a href="clipped-room-negative.json">截断房间负例</a><a href="code-manifest.json">冻结代码</a><a href="full-tests.txt">完整测试</a></p>']
    for row in summary:
        name=row['name'];html.append(f'<h2>{escape(row["dwg"])}</h2><pre>{escape(json.dumps(row,ensure_ascii=False,indent=2))}</pre><img src="{name}.png"><p><a href="{name}.json">完整对象与关系</a><a href="{name}-checks.json">独立来源及双侧核对</a></p>')
    (out/'review.html').write_text(''.join(html))


if __name__=='__main__':main()
