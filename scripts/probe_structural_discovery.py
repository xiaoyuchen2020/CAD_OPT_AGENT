from pathlib import Path
import sys,json,sqlite3,logging
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.service import Workspace
from cad_agent.config import Settings
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import RoomDiscover
from cad_agent.geometry.tiled_discovery import discover_with_subdivision
from cad_agent.geometry.discovery_review import DiscoveryReviewService,PrepareDiscoveryReview
from regress_agent_workflow import ROOT
logging.getLogger('ezdxf').setLevel(logging.ERROR)
OUT=ROOT/'artifacts/spatial-repair-20261016-probe'
w=Workspace(replace(Settings.load(),data_dir=OUT/'data'))
try:
 old=json.loads((OUT/'discovery.json').read_text());source=w.store.get('measurement',old['discovery_id'])
 geometry=GeometryService(w,source['snapshot_id'])
 print('START',source['request'],flush=True)
 result=discover_with_subdivision(geometry,RoomDiscover.model_validate(source['request']))
 print('DISCOVERED',len(result['candidates']),[(c['candidate_shell_area_m2'],c.get('linework_basis')) for c in result['candidates'][:18]],flush=True)
 review=DiscoveryReviewService(w,source['snapshot_id']).prepare(PrepareDiscoveryReview(discovery_id=result['measurement_id']))
 (OUT/'discovery-structural.json').write_text(json.dumps(review,ensure_ascii=False,indent=2));print([i['image_id'] for i in review['images']],flush=True)
finally:w.close()
