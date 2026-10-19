from pathlib import Path
import sys,json,sqlite3,logging
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.service import Workspace
from cad_agent.config import Settings
from cad_agent.geometry.discovery_review import DiscoveryReviewService,PrepareDiscoveryReview
from regress_agent_workflow import ROOT
logging.getLogger('ezdxf').setLevel(logging.ERROR)
OUT=ROOT/'artifacts/spatial-repair-20261016-probe'
w=Workspace(replace(Settings.load(),data_dir=OUT/'data'))
try:
 with sqlite3.connect('file:'+str(ROOT/'artifacts/complex-repair-final-20261016-v4/data/index.sqlite3')+'?mode=ro',uri=True) as c:
  records=[json.loads(v) for v, in c.execute("select data from records where kind='measurement'")]
 r=next(r for r in records if r.get('algorithm_version')=='bounded-discovery-tiles-v1')
 w.store.put('measurement',r['measurement_id'],r)
 out=DiscoveryReviewService(w,r['snapshot_id']).prepare(PrepareDiscoveryReview(discovery_id=r['measurement_id']))
 (OUT/'discovery.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(out['review_id'],[i['image_id'] for i in out['images']],flush=True)
finally:w.close()
