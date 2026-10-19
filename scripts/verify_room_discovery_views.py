"""Read-back after original-overlay review; view type is explicit review context."""
import json,logging
from collections import Counter
from shapely.geometry import Polygon
from shapely.strtree import STRtree
from pathlib import Path
from hashlib import sha256
from cad_agent.service import Workspace
from cad_agent.config import Settings
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import RoomDiscover
from scripts.validate_room_discovery import ROOT,ARCHIVE,dump

def main():
 logging.getLogger('ezdxf').setLevel(logging.ERROR)
 contexts={'synthetic':'floor_plan','residential':'floor_plan','bedroom_washroom':'section','stairs_washroom':'table'}
 notes={
 'synthetic':'构造的九个独立空间，八个任意名称和一个未命名空间；9 个较高支持候选全部对应构造空间。',
 'residential':'住宅平面包含厨房、餐厅、客厅、卧室、书房、卫生间与阳台。发现卧室候选，但门窗、家具与房间合并仍有误选；不能把5个较高支持候选等同5个房间。',
 'bedroom_washroom':'原图为带卧室、卫生间文字的剖面负例，非平面房间样本。视图类型明确后应拒绝平面识别。',
 'stairs_washroom':'原图主要为降板标高对照表及签字栏，表内包含楼梯间、卫生间等房间词。作为非平面负例，不能依据名称认房间。'}
 summary=[]
 for key,view in contexts.items():
  before=json.loads((ROOT/f'{key}.json').read_text())
  w=Workspace(Settings(data_dir=(ROOT if key=='synthetic' else ARCHIVE)/'data'))
  try:
   args=RoomDiscover.model_validate(before['request']).model_copy(update={'view_kind':view})
   r=GeometryService(w,before['snapshot_id']).discover(args);dump(ROOT/f'final-{key}.json',r)
   # Versioned IDs can reorder equivalent faces with redundant collinear
   # vertices. Compare actual outlines, not serialization or candidate order.
   def difference(left,right):
    target=[Polygon(c['boundary']) for c in right['candidates']];tree=STRtree(target)
    maximum=0.
    for c in left['candidates']:
     shape=Polygon(c['boundary']);indices=tree.query(shape)
     if not len(indices):return float('inf')
     best=min((target[int(i)] for i in indices),key=lambda p:shape.symmetric_difference(p).area)
     if shape.hausdorff_distance(best)>1e-6:return float('inf')
     maximum=max(maximum,shape.symmetric_difference(best).area)
    return maximum
   delta=max(difference(r,before),difference(before,r)) if view=='floor_plan' else None
   same=delta<1e-8 if delta is not None else None
   if view=='floor_plan':assert same
   else:assert r['diagnostics']==['non_plan_view'] and not r['candidates']
   summary.append(dict(case=key,view_kind=view,view_basis='original_overlay_review_or_constructed_fixture_not_automatic_classification',
    image=f'{key}.png',image_sha256=sha256((ROOT/f'{key}.png').read_bytes()).hexdigest(),
    overlay_algorithm_version=before['algorithm_version'],final_algorithm_version=r['algorithm_version'],
    final_record=f'final-{key}.json',source_sha256=r['source_sha256'],
    final_candidate_count=len(r['candidates']),room_candidate_count=sum(c['candidate_role']=='room_candidate' for c in r['candidates']),
    unchanged_geometry_verified=same,maximum_symmetric_difference_drawing_units_squared=delta,
    before_candidate_count=len(before['candidates']),note=notes[key]))
   print(key,summary[-1]['final_candidate_count'],flush=True)
  finally:w.close()
 dump(ROOT/'final-review.json',summary)
if __name__=='__main__':main()
