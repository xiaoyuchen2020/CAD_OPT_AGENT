"""Replay generic discovery on synthetic spaces and non-AR030 room labels.

Labels select validation crops only. The recognition API receives layout+bbox,
never a room name, historical boundary or interior seed.
"""
import json, logging, runpy
from pathlib import Path
from hashlib import sha256
from collections import Counter
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import RoomDiscover

ROOT=Path('artifacts/general-room-discovery-20261015')
ARCHIVE=Path('artifacts/ar030-real-room-validation-20261015')
CASES=[
 ('residential','snap_e316a1345c7a5db25e19458f',[112000,-184000,128000,-170000]),
 ('bedroom_washroom','snap_77b384ae399a2c5c69f5a23a',[81000,-663500,90000,-655000]),
 ('stairs_washroom','snap_535fa8a4f077da8bf760182e',[541000,8000,551000,19000]),
]

def dump(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2))

def render(workspace,sid,record,path,*,contour_mode=False):
 import ezdxf,numpy as np
 from ezdxf import bbox
 from cad_agent.cad.reader import walk_entities
 from ezdxf.addons.drawing import RenderContext,Frontend,config
 from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 from matplotlib.font_manager import FontProperties
 from matplotlib.patches import Polygon as Patch
 font='/System/Library/Fonts/STHeiti Medium.ttc';fp=FontProperties(fname=font)
 doc=ezdxf.readfile(workspace.store.artifact(sid,'source.dxf'))
 for style in doc.styles:style.dxf.font=font;style.dxf.bigfont=''
 bounds=record['scope_bbox'];entities=[];issues=[]
 for e,_ in walk_entities(doc.modelspace(),issues,'Model',entity_limit=1000000):
  try:
   if e.dxftype() in ('TEXT','MTEXT','ATTRIB'):
    x,y=e.dxf.insert.x,e.dxf.insert.y
    if bounds[0]-1000<x<bounds[2]+1000 and bounds[1]-1000<y<bounds[3]+1000:entities.append(e)
   else:
    ext=bbox.extents([e],fast=True)
    if ext.has_data and ext.extmin.x<=bounds[2] and ext.extmax.x>=bounds[0] and ext.extmin.y<=bounds[3] and ext.extmax.y>=bounds[1]:entities.append(e)
  except (ValueError,TypeError,AttributeError,ZeroDivisionError):pass
 context=RenderContext(doc);cfg=config.Configuration(background_policy=config.BackgroundPolicy.WHITE,color_policy=config.ColorPolicy.BLACK,lineweight_scaling=.4,min_lineweight=.025)
 fig,axes=plt.subplots(1,2,figsize=(16,8),dpi=110)
 for ax in axes:
  backend=MatplotlibBackend(ax);Frontend(context,backend,cfg).draw_entities(entities);backend.finalize()
  ax.set_xlim(bounds[0],bounds[2]);ax.set_ylim(bounds[1],bounds[3]);ax.set_aspect('equal');ax.set_axis_off()
 axes[0].set_title('原图局部（字体替代）',fontproperties=fp)
 axes[1].set_title('轮廓叠加：绿色整体轮廓 / 橙色内部轮廓；用途均待确认' if contour_mode else '候选叠加：绿色有双面支持 / 橙色支持不足；均待确认',fontproperties=fp)
 for i,c in enumerate(record['candidates'][:50]):
  color='#087d65' if c['candidate_role']=='room_candidate' else '#d47710'
  axes[1].add_patch(Patch(c['boundary'],closed=True,fill=False,edgecolor=color,linewidth=1,alpha=.65))
  axes[1].text(*c['seed'],str(i+1),fontsize=6,color=color)
 title=(f"轮廓结构复核 | 1 个整体轮廓 + {len(record['candidates'])-1} 个内部轮廓 | 房间身份和净面积未确认" if contour_mode else f"通用围合识别 | {len(record['candidates'])} 个候选（前50个叠加） | 房间身份和净面积未确认")
 fig.suptitle(title,fontproperties=fp)
 fig.text(.02,.02,f"source SHA256 {record['source_sha256']} | {record['applied_unit']} / {record['unit_basis']} | " + ', '.join(record['diagnostics'])[:120],fontsize=7)
 fig.subplots_adjust(top=.89,bottom=.05,left=.01,right=.99,wspace=.03);fig.set_size_inches(16,8);fig.savefig(path);plt.close(fig)
 return dict(image=path.name,sha256=sha256(path.read_bytes()).hexdigest(),source_entity_count=len(entities),render_diagnostics=issues[:10])

def main():
 logging.getLogger('ezdxf').setLevel(logging.ERROR);ROOT.mkdir(exist_ok=True)
 summary=[]
 with_workspace=Workspace(Settings(data_dir=ROOT/'data'))
 try:
  helper=runpy.run_path('tests/test_room_discovery.py')['plan']
  s,a,d=helper(with_workspace,['卧室','客厅','教室','办公室','卫生间','档案室','走廊','X-203',None],gap=True)
  a=a.model_copy(update={'bbox':[-1.,-1.,23.,17.]})
  r=s.discover(a);dump(ROOT/'synthetic.json',r)
  (ROOT/'synthetic.dxf').write_bytes(with_workspace.store.artifact(d['id'],'source.dxf').read_bytes())
  image=render(with_workspace,d['id'],r,ROOT/'synthetic.png')
  summary.append(dict(case='synthetic',snapshot_id=d['id'],candidates=len(r['candidates']),supported=sum(c['candidate_role']=='room_candidate' for c in r['candidates']),diagnostics=r['diagnostics'],**image))
 finally:with_workspace.close()
 w=Workspace(Settings(data_dir=ARCHIVE/'data'))
 try:
  for key,sid,bounds in CASES:
   s=GeometryService(w,sid);d=s.drawing
   assert d['unit']=='mm'
   source=w.store.artifact(sid,'source.dxf');assert sha256(source.read_bytes()).hexdigest()==d['sha256']
   lid=next(x['layout_id'] for x in d['layouts'] if x['name']=='Model')
   r=s.discover(RoomDiscover(layout_id=lid,bbox=bounds));dump(ROOT/f'{key}.json',r)
   image=render(w,sid,r,ROOT/f'{key}.png')
   summary.append(dict(case=key,snapshot_id=sid,source_sha256=d['sha256'],source_name=d['name'],candidates=len(r['candidates']),supported=sum(c['candidate_role']=='room_candidate' for c in r['candidates']),diagnostics=r['diagnostics'],**image))
   print(key,summary[-1],flush=True)
 finally:w.close()
 dump(ROOT/'summary.json',summary)
 refs=json.loads((ARCHIVE/'reference-hashes.json').read_text())
 checks={p:sha256(Path(p).read_bytes()).hexdigest()==h for p,h in refs.items()};assert all(checks.values());dump(ROOT/'reference-verification.json',checks)
 print('done',flush=True)
if __name__=='__main__':main()
