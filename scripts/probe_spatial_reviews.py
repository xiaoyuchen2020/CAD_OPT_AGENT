from pathlib import Path
import sys,json,sqlite3,logging
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.service import Workspace
from cad_agent.config import Settings
from cad_agent.geometry.connection_review import ConnectionReviewService,PrepareConnections
from regress_agent_workflow import prepare,SOURCES,ROOT
logging.getLogger('ezdxf').setLevel(logging.ERROR)
OUT=ROOT/'artifacts/spatial-repair-20261016-probe'
PAIRS={2:[('region_6ae927d6eb6dfc8fdc4cf733','region_3b8f5657dd44703b76f61aad')],1:[('region_9ce2a7976d566cc9bd52f83c','region_c1b4ba027020e25929c40aad'),('region_6ac7b3933febab051c694009','region_0dbc3cd91df626edb7806264'),('region_7be1c3452cf2319424137b85','region_62cf392502b942b80f9737d4')]}
w=Workspace(replace(Settings.load(),data_dir=OUT/'data'))
try:
 prepare(w)
 for idx,pairs in PAIRS.items():
  sid=SOURCES[idx][1];lid=w.store.get('drawing',sid)['layouts'][0]['layout_id']
  r=ConnectionReviewService(w,sid).prepare(PrepareConnections(layout_id=lid,unit_override='mm',pairs=[dict(source_region_id=a,target_region_id=b) for a,b in pairs]))
  (OUT/f'connections-{idx}.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
  print(idx,[(len(i['barriers']),len(i['openings']),i['geometry_complete']) for i in r['items']],[(i['image_id']) for i in r['images']],flush=True)
finally:w.close()
