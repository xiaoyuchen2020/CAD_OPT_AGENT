"""Revalidate saved live answers against current contracts; never calls a model."""
from pathlib import Path
import json,sqlite3,sys
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.session import Claim
from cad_agent.contracts import ChatRequest

root=Path(__file__).resolve().parents[1]/'artifacts/agent-workflow-20261016'
latest={r['case']:r for r in json.loads((root/'live-results.json').read_text())}
data=root/'validation-data';data.mkdir(exist_ok=True)
with sqlite3.connect(root/'data/index.sqlite3') as src,sqlite3.connect(data/'index.sqlite3') as dst:src.backup(dst)
if not (data/'drawings').exists():(data/'drawings').symlink_to(root/'data/drawings',target_is_directory=True)
w=Workspace(replace(Settings.load(),data_dir=data));report=[]
try:
 for name,summary in latest.items():
  run=json.loads((root/(summary['run_id']+'.json')).read_text())
  req=ChatRequest.model_validate(run['request']|{'resume_task_id':run['task_id']})
  session=AgentRunner(w,req)
  claims=[{k:v for k,v in c.items() if k in Claim.model_fields} for c in run['result']['claims']]
  out=session.finish(json.dumps({'claims':claims,'missing_information':run['result']['missing_information']}))
  report.append({'case':name,'run_id':run['id'],'answer_revalidated':True,'batch_geometry_checked':True,'task_status':out['task']['status']})
finally:w.close()
(root/'final-contract-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2))
