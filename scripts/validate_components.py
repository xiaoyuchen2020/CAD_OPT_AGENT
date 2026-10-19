"""Read archived real DXFs; write only a new isolated validation workspace.

python -m scripts.validate_components --output artifacts/components-validation-20261015
"""
import argparse
from collections import Counter
from hashlib import sha256
from html import escape
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import time

from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.components.service import ComponentService
from cad_agent.components.contracts import ContextQuery, DimensionQuery, RelationQuery
from cad_agent.geometry.source import native_source

CASES = [
    (11794,"snap_e316a1345c7a5db25e19458f",[112000,-184000,128000,-170000]),
    (11805,"snap_77b384ae399a2c5c69f5a23a",[26000,-4000,36000,6000]),
    (11809,"snap_535fa8a4f077da8bf760182e",[456000,-8000,467000,3000]),
]
NEGATIVE_SCOPES={11805:("section",[81000,-663500,90000,-655000]),11809:("table",[541000,8000,551000,19000])}


def dump(path, data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")


def render_review(workspace, sid, bounds, record, destination):
    """Render actual source linework with a separate relation overlay."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as Patch
    from cad_agent.geometry.source import scoped_geometry_index
    idx=scoped_geometry_index(workspace,sid,record["layout_id"],bounds,record["applied_unit"])
    fig,axes=plt.subplots(1,2,figsize=(16,9),dpi=120)
    for ax in axes:
        for e in idx.query(record["layout_id"],bounds):
            for points in e["paths"]:
                xs,ys=zip(*points);ax.plot(xs,ys,color="#777777",linewidth=.35)
        ax.set_xlim(bounds[0],bounds[2]);ax.set_ylim(bounds[1],bounds[3]);ax.set_aspect("equal");ax.tick_params(labelsize=7)
    # Use native text anchors from the original scene, not generated labels.
    scene=json.loads(workspace.store.artifact(sid,"scene.json").read_text())
    for t in scene["texts"]:
        x,y=t["origin"]
        if t["layout_id"]==record["layout_id"] and bounds[0]<=x<=bounds[2] and bounds[1]<=y<=bounds[3]:
            for ax in axes:ax.text(x,y,t["text"][:30],fontsize=5,fontfamily="Heiti TC",rotation=t["rotation"])
    for i,room in enumerate(record["rooms"][:40],1):
        axes[1].add_patch(Patch(room["boundary"],fill=False,edgecolor="#009b72",linewidth=1))
        axes[1].text(*room["seed"],f"R{i}",fontsize=7,color="#006e52")
    for i,opening in enumerate(record["openings"][:80],1):
        axes[1].add_patch(Patch(opening["boundary"],fill=True,facecolor="#f59e0b",edgecolor="#be6000",alpha=.65))
        axes[1].text(*opening["boundary"][0],f"O{i}",fontsize=6,color="#a44800")
    axes[0].set_title("Native calculation linework + text anchors (not complete CAD rendering)")
    axes[1].set_title("Candidates only: rooms green / openings orange; first 40 / 80")
    fig.suptitle(f"{sid} | {len(record['rooms'])} room candidates, {len(record['openings'])} opening candidates")
    fig.text(.02,.02,"WCS / "+record["applied_unit"]+" | SHA256 "+record["source_sha256"],fontsize=8)
    fig.tight_layout(rect=[0,.04,1,.95]);fig.savefig(destination);plt.close(fig)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--archive",type=Path,default=Path("artifacts/ar030-real-room-validation-20261015"))
    parser.add_argument("--output",type=Path,default=Path("artifacts/components-validation-20261015"))
    args=parser.parse_args();out=args.output.resolve();archive=args.archive.resolve()
    out.mkdir(parents=True,exist_ok=True)
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    inputs={x["paper_id"]:x for x in json.loads((archive/"input_manifest.json").read_text())}
    with sqlite3.connect(f"file:{archive/'data/index.sqlite3'}?mode=ro",uri=True) as c:
        drawings={d["id"]:d for raw, in c.execute("SELECT data FROM records WHERE kind='drawing'") if (d:=json.loads(raw))}
    summary=[]
    for paper,sid,bounds in CASES:
        started=time.monotonic();d=drawings[sid];meta=inputs[paper]
        origin=archive/"data/drawings"/sid
        with_source=sha256((origin/"source.dxf").read_bytes()).hexdigest()
        assert with_source==d["sha256"]==meta["dxf_sha256"]
        w=Workspace(Settings(data_dir=out/"data"))
        try:
            folder=w.store.artifact_dir(sid)
            for filename in ("source.dxf","scene.json","entities.json","regions.json"):
                shutil.copy2(origin/filename,folder/filename)
            w.store.put("drawing",sid,d)
            lid=next(l["layout_id"] for l in d["layouts"] if l["name"]=="Model")
            service=ComponentService(w,sid)
            print(paper,"context",flush=True)
            context=service.build("context",ContextQuery(layout_id=lid))
            dump(out/f"{paper}-context.json",context)
            print(paper,"dimensions",flush=True)
            dimensions=service.build("dimensions",DimensionQuery(layout_id=lid,bbox=bounds))
            dump(out/f"{paper}-dimensions.json",dimensions)
            print(paper,"relations",flush=True)
            graph=service.build("relations",RelationQuery(layout_id=lid,bbox=bounds,view_kind="floor_plan"))
            dump(out/f"{paper}-relations.json",graph)
            negative=None
            if paper in NEGATIVE_SCOPES:
                view,negative_bounds=NEGATIVE_SCOPES[paper]
                negative=service.build("relations",RelationQuery(layout_id=lid,bbox=negative_bounds,view_kind=view))
                assert not negative["rooms"] and not negative["openings"] and "non_plan_view" in negative["diagnostics"]
                dump(out/f"{paper}-non-plan-control.json",negative)
            source=native_source(w,sid)
            # Independent source checks: references resolve, both original text
            # and native definition points agree; geometry numbers stay separate.
            source_checks=[]
            for dim in dimensions["dimensions"]:
                entity=source["entities"][dim["object_id"]][0]
                assert dim["text_override"]==entity.dxf.get("text","")
                source_checks.append(dict(entity_id=dim["object_id"],text_matches=True,
                    native_value=entity.get_measurement() if dim["dimension_type"]!="ordinate" else None,
                    recomputed=dim["geometry_recomputed"],status=dim["status"]))
            object_ids={x["object_id"] for key in ("rooms","walls","openings") for x in graph[key]}
            assert all(r["source_id"] in object_ids and r["target_id"] in object_ids for r in graph["relations"])
            assert all(set(x["entity_ids"])<=source["entities"].keys() for key in ("rooms","walls","openings") for x in graph[key])
            text_ids={x["entity_id"] for x in json.loads(w.store.artifact(sid,"scene.json").read_text())["texts"]}
            assert all(s["title_source"]["entity_id"] in text_ids for s in context["subframes"])
            assert all(r["area_m2"] is None for r in graph["rooms"])
            assert all(o["door_type"] is None for o in graph["openings"])
            dump(out/f"{paper}-source-checks.json",source_checks)
            print(paper,"render",flush=True)
            render_review(w,sid,bounds,graph,out/f"{paper}-review.png")
            assert sha256((origin/"source.dxf").read_bytes()).hexdigest()==with_source
            row=dict(paper_id=paper,dwg_name=meta["source"]["name"],snapshot_id=sid,dxf_sha256=with_source,
                unit=d["unit"],scope_bbox=bounds,subframe_candidates=len(context["subframes"]),
                bounded_subframes=sum(s["bbox"] is not None for s in context["subframes"]),
                dimensions=len(dimensions["dimensions"]),dimension_conflicts=sum(x["status"]=="conflict" for x in dimensions["dimensions"]),
                room_candidates=len(graph["rooms"]),wall_candidates=len(graph["walls"]),opening_candidates=len(graph["openings"]),
                two_sided_candidates=sum(o["connection_status"]=="two_space_candidates" for o in graph["openings"]),
                relations=len(graph["relations"]),diagnostics=graph["diagnostics"],
                linework_basis=graph["linework_basis"],view_kind_basis="manually_selected_plan_crop; not_automatic_classification",
                non_plan_control_rejected=negative is not None,
                source_references_checked=True,source_unchanged=True,elapsed_seconds=round(time.monotonic()-started,2),
                context_coverage=context["coverage"],dimension_coverage=dimensions["coverage"])
            summary.append(row);dump(out/"summary.json",summary);print(json.dumps(row,ensure_ascii=False),flush=True)
        finally:w.close()
    html=['<!doctype html><html lang="zh"><meta charset="utf-8"><title>CAD 公共组件真实图纸验证</title><style>body{font:16px system-ui;max-width:1450px;margin:40px auto;background:#fafafa;color:#172b36}img{width:100%}pre{white-space:pre-wrap;background:#eef1f4;padding:20px}section{margin:36px 0}a{margin-right:20px}</style><h1>CAD 公共组件 · 真实图纸验证</h1><p>本地冻结代码与历史原始 DXF 回放。上下文覆盖 Model 布局；尺寸与关系只覆盖指定局部。以下数量均为候选，不代表识别准确率、工程验收或优化建议数。</p>']
    for row in summary:
        n=row['paper_id'];html.append(f'<section><h2>{escape(row["dwg_name"])}</h2><pre>{escape(json.dumps(row,ensure_ascii=False,indent=2))}</pre><img src="{n}-review.png" alt="原始计算线形与候选关系叠加">')
        for kind in ('context','dimensions','relations','source-checks'):html.append(f'<a href="{n}-{kind}.json">{kind} JSON</a>')
        html.append('</section>')
    (out/'review.html').write_text(''.join(html)+'</html>',encoding='utf-8')


if __name__=="__main__":main()
