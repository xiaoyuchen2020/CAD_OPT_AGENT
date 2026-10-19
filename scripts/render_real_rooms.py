"""Fresh source-DXF overlays and independent area checks for every replay point."""
import argparse, json, logging, os, math, time
from pathlib import Path
from hashlib import sha256
from collections import Counter
import ezdxf
from ezdxf import bbox
from cad_agent.cad.reader import walk_entities
from ezdxf.addons.drawing import RenderContext, Frontend, config
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Polygon as Patch
from shapely.geometry import Polygon
logging.getLogger('ezdxf').setLevel(logging.ERROR)
FONT='/System/Library/Fonts/STHeiti Medium.ttc'
FP=FontProperties(fname=FONT)

def shoelace(ring):
    if not ring:return 0.
    x,y=ring[0];p=[(a-x,b-y) for a,b in ring];return abs(math.fsum(a*d-c*b for (a,b),(c,d) in zip(p,p[1:]+p[:1])))/2

def audit(row, report):
    r=report['record'];c=r.get('selected');old=row['measurement'];out=dict(key=row['key'],paper_id=row['paper_id'],dwg_name=row['source']['name'],status=r['status'],diagnostics=r['diagnostics'],historical_status=row['review']['status'],historical_area_m2=old['areaM2'])
    if c:
        independent=(shoelace(c['boundary'])-sum(shoelace(h) for h in c['holes']))*1e-6
        new=Polygon(c['boundary'],c['holes']);prev=Polygon(old['boundary'],old['holes'])
        out.update(candidate_area_m2=c['candidate_area_m2'],independent_area_m2=independent,area_error_m2=abs(independent-c['candidate_area_m2']),
            symmetric_difference_m2=new.symmetric_difference(prev).area*1e-6,hausdorff_m=new.hausdorff_distance(prev)*.001,
            polygon_valid=new.is_valid,edge_count=len(c['edge_evidence']),closure_count=len(c['inferred_closures']),
            source_entity_count=len(c['entity_ids']),historical_area_delta_m2=c['candidate_area_m2']-old['areaM2'])
        out['comparison']='consistent_contour' if out['symmetric_difference_m2']<=max(.003,new.length*.001*.0015) else 'different_contour'
    else:out['comparison']='no_candidate'
    return out

def run(root,phase,papers,wait=False):
    rows=json.loads((root/'review_points.json').read_text());out=root/phase/'figures';out.mkdir(exist_ok=True)
    audits=[]
    for pid in sorted({r['paper_id'] for r in rows}):
        if papers and pid not in papers:continue
        expected=[r for r in rows if r['paper_id']==pid]
        deadline=time.monotonic()+1200
        while wait and any(not (root/phase/(r['key']+'.json')).exists() for r in expected):
            if time.monotonic()>deadline:raise TimeoutError(f"Missing measurement results for {pid}")
            time.sleep(3)
        selected=[r for r in expected if (root/phase/(r['key']+'.json')).exists()]
        if selected and all((out/(r['key']+'.json')).exists() for r in selected):
            audits.extend(json.loads((out/(r['key']+'.json')).read_text()) for r in selected)
            continue
        if not selected:continue
        doc=ezdxf.readfile(root/'input/dxf'/f'{pid}.dxf')
        for style in doc.styles:style.dxf.font=FONT;style.dxf.bigfont=''
        ents=[];boxes=[];diagnostics=[]
        for e,path in walk_entities(doc.modelspace(),diagnostics,'Model',entity_limit=1000000):
            try:
                if e.dxftype() in ('TEXT','MTEXT','ATTRIB'):
                    text=e.plain_text() if hasattr(e,'plain_text') else e.dxf.text
                    x,y=e.dxf.insert.x,e.dxf.insert.y
                    height=e.dxf.get('char_height',250) if e.dxftype()=='MTEXT' else e.dxf.get('height',250)
                    pad=max(1000,len(text)*height*1.5)
                    b=[x-pad,y-pad,x+pad,y+pad]
                else:
                    ext=bbox.extents([e],fast=True)
                    if not ext.has_data:continue
                    b=[ext.extmin.x,ext.extmin.y,ext.extmax.x,ext.extmax.y]
                ents.append(e);boxes.append(b)
            except (ValueError,TypeError,AttributeError,ZeroDivisionError):continue
        arr=np.asarray(boxes);context=RenderContext(doc)
        cfg=config.Configuration(background_policy=config.BackgroundPolicy.WHITE,color_policy=config.ColorPolicy.BLACK,lineweight_scaling=.4,min_lineweight=.025,hatch_policy=config.HatchPolicy.SHOW_OUTLINE,max_flattening_distance=.5)
        for row in selected:
            key=row['key'];report=json.loads((root/phase/(key+'.json')).read_text());r=report['record'];c=r.get('selected');a=audit(row,report)
            old=row['measurement'];points=old['boundary']+(c['boundary'] if c else [])
            xs,ys=zip(*points);cx=(min(xs)+max(xs))/2;cy=(min(ys)+max(ys))/2
            width=max(max(xs)-min(xs),1600)*1.5;height=max(max(ys)-min(ys),1600)*1.5;width=max(width,height*1.15);height=max(height,width/1.15)
            b=[cx-width/2,cy-height/2,cx+width/2,cy+height/2]
            mask=(arr[:,0]<=b[2])&(arr[:,2]>=b[0])&(arr[:,1]<=b[3])&(arr[:,3]>=b[1]);indices=np.flatnonzero(mask)
            fig,axes=plt.subplots(1,2,figsize=(14,7),dpi=110)
            for ax in axes:
                backend=MatplotlibBackend(ax);frontend=Frontend(context,backend,cfg)
                frontend.draw_entities(ents[i] for i in indices);backend.finalize()
                ax.set_xlim(b[0],b[2]);ax.set_ylim(b[1],b[3]);ax.set_aspect('equal');ax.set_axis_off()
                ax.plot(*old['label'],'+',color='#2255cc',markersize=9,zorder=30)
            axes[0].set_title('原图局部（字体替代；蓝十字为原定位点）',fontproperties=FP,fontsize=10)
            axes[1].set_title('绿线：当前候选；紫虚线：历史对照；橙线：推断闭口',fontproperties=FP,fontsize=10)
            ax=axes[1]
            for ring in [old['boundary']]+old['holes']:ax.add_patch(Patch(ring,closed=True,fill=False,edgecolor='#b04bbb',linestyle='--',linewidth=1.2,zorder=21))
            if c:
                for ring in [c['boundary']]+c['holes']:ax.add_patch(Patch(ring,closed=True,fill=False,edgecolor='#008c6a',linewidth=1.7,zorder=22))
                for bridge in c['inferred_closures']:
                    x,y=zip(*bridge['points']);ax.plot(x,y,color='#e58019',linewidth=2,zorder=23)
            fig.suptitle(f"{key} | {row['source']['name']}\n{row['title']} | {old['roomType']} | {r['status']} | 候选 {r.get('candidate_area_m2')} m² / 历史 {old['areaM2']:.4f} m²",fontproperties=FP,fontsize=10)
            note=', '.join(r['diagnostics']) or 'geometry-supported candidate; engineering identity requires review'
            fig.text(.02,.04,note[:175],fontsize=7)
            fig.text(.02,.015,f"DXF SHA256 {r['source_sha256']} | explicit mm assumption | fresh source render; comparison is not ground truth",fontsize=6)
            fig.subplots_adjust(top=.85,bottom=.08,left=.01,right=.99,wspace=.04);fig.set_size_inches(14,7);dest=out/(key+'.png');fig.savefig(dest,facecolor='white');plt.close(fig)
            a.update(image='figures/'+key+'.png',image_sha256=sha256(dest.read_bytes()).hexdigest(),render_entity_count=len(indices),render_bounds=b)
            (out/(key+'.json')).write_text(json.dumps(a,ensure_ascii=False,indent=2));audits.append(a)
        print(pid,'rendered',len(selected),flush=True)
    (root/phase/'audit.json').write_text(json.dumps(audits,ensure_ascii=False,indent=2))
    print(Counter(a['comparison'] for a in audits),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('artifacts/ar030-real-room-validation-20261015'));p.add_argument('--phase',default='after');p.add_argument('--papers',type=int,nargs='*');p.add_argument('--wait',action='store_true');a=p.parse_args();run(a.root.resolve(),a.phase,a.papers,a.wait)
