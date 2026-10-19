from pathlib import Path
import sys,json,sqlite3,logging,shutil,os
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.service import Workspace
from cad_agent.config import Settings
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import RoomDiscover
from cad_agent.geometry.tiled_discovery import discover_with_subdivision
from cad_agent.geometry.discovery_review import DiscoveryReviewService,PrepareDiscoveryReview
ROOT=Path(__file__).resolve().parents[1];os.environ['CAD_RENDER_FONT_DIR']=str(ROOT/'.data/render-fonts')
logging.getLogger('ezdxf').setLevel(logging.ERROR)
base=ROOT/'artifacts/spatial-repair-live-20261016-spatial5/data';out=ROOT/'artifacts/spatial-viewport-probe-20261016'
sid='snap_3dad7f8e24f6b0d90a08acfd';db=sqlite3.connect('file:'+str(base/'index.sqlite3')+'?mode=ro',uri=True)
records=[(k,key,json.loads(data)) for k,key,data in db.execute('select kind,id,data from records')]
w=Workspace(replace(Settings.load(),data_dir=out/'data'))
try:
 for kind,key,r in records:
  if (kind=='drawing' and key==sid) or (kind in ('measurement','visual_image') and r.get('snapshot_id')==sid):w.store.put(kind,key,r)
 shutil.copytree(base/'drawings'/sid,out/'data/drawings'/sid,dirs_exist_ok=True)
 source=next(r for k,key,r in records if k=='measurement' and r.get('kind')=='room_discovery' and r.get('snapshot_id')==sid and r['scope_bbox']==[320000.,-3730000.,370000.,-3690000.])
 args=RoomDiscover.model_validate(source['request'])
 if '--full' in sys.argv:args=args.model_copy(update={'bbox':[275957.02750723425,-3756320.9793645972,460812.0052178508,-3666010.9793645963]})
 from cad_agent.geometry.source import scoped_geometry_index,native_source
 from cad_agent.components.openings import effective_layer
 from collections import Counter
 idx=scoped_geometry_index(w,sid,args.layout_id,args.bbox,'mm');native=native_source(w,sid)
 print('layers',Counter(effective_layer(i,native) for i in idx.data['entities']),flush=True)
 r=discover_with_subdivision(GeometryService(w,sid),args)
 print('candidates',len(r['candidates']),[(v['candidate_shell_area_m2'],v.get('linework_basis')) for v in r['candidates'][:12]],flush=True)
 review=DiscoveryReviewService(w,sid).prepare(PrepareDiscoveryReview(discovery_id=r['measurement_id']))
 (out/('review-full.json' if '--full' in sys.argv else 'review.json')).write_text(json.dumps(review,ensure_ascii=False,indent=2));print([i['image_id'] for i in review['images']],flush=True)
finally:w.close()
