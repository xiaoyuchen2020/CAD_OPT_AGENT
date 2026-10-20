"""Seed a new regression with an immutable, completed real parent conversation.

The fixture is marked explicitly and excluded from new-run/token counts. Never
opens a Workspace on the source or changes the original records.
"""
from pathlib import Path
import sys,sqlite3,json,shutil
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cad_agent.artifact_storage import copy_artifact_tree
source,target=map(Path,sys.argv[1:3]);case=sys.argv[3]
assert not (target/'data/index.sqlite3').exists(),'Refuse to replace an existing regression database'
reports=json.loads((source/'results.json').read_text());parent=next(r for r in reversed(reports) if r['case']==case and r['status']=='completed')
a=sqlite3.connect('file:'+str(source/'data/index.sqlite3')+'?mode=ro',uri=True)
rows=a.execute('select id,kind,data,created from records').fetchall()
assert any(k=='chat_run' and key==parent['run_id'] and json.loads(d)['status']=='completed' for key,k,d,_ in rows)
(target/'data').mkdir(parents=True,exist_ok=True)
b=sqlite3.connect(target/'data/index.sqlite3');b.execute('create table records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,created TEXT NOT NULL)')
rows=[r for r in rows if r[1] not in ('chat_run','agent_task') or (r[1]=='chat_run' and r[0]==parent['run_id']) or (r[1]=='agent_task' and r[0]==parent['task_id'])]
b.executemany('insert into records values (?,?,?,?)',rows);b.commit();b.close();a.close()
copy_artifact_tree(source/'data/drawings',target/'data/drawings')
parent=parent|dict(fixture_only=True,origin_directory=str(source.resolve()))
(target/'results.json').write_text(json.dumps([parent],ensure_ascii=False,indent=2))
(target/'inherited-fixture.json').write_text(json.dumps(parent,ensure_ascii=False,indent=2))
print('Copied completed real parent:',parent['run_id'])
