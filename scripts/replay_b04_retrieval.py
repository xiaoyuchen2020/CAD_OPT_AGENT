"""Bounded offline replay on frozen real DXFs. No model or baseline writes."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from cad_agent.artifact_storage import copy_artifact
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.tasks import TaskPlan, PlanStep, CoverageTarget
from cad_agent.analysis.recall import pending_calls
from cad_agent.config import Settings
from cad_agent.contracts import ChatRequest
from cad_agent.service import Workspace

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'artifacts/designer-facts-repaired-20261019'
OUT=ROOT/'artifacts/bottleneck-repairs-20261019'


def replay():
    specs=[('Q03-A','逐项读取管井、女儿墙的材料、厚度。',[('管井','材料'),('管井','厚度'),('女儿墙','材料'),('女儿墙','厚度')]),
           ('Q03-B','请核对女儿墙与管井的厚度及材料原文。',[('女儿墙','厚度'),('管井','材料')]),
           ('Q12-B','读取防火分区的面积、停车数。',[('防火分区','面积'),('防火分区','停车数')]),
           ('Q20-B','核对防火卷帘的型号、材质、耐火时间及性能级别。',[('防火卷帘','型号'),('防火卷帘','材质'),('防火卷帘','耐火时间'),('防火卷帘','性能级别')]),
           ('Q18-A','查询设备房、水泵房的位置原文。',[('设备房','位置'),('水泵房','位置')])]
    paths=[FROZEN/'review.json']
    for case,_,_ in specs:
        paths += [FROZEN/'attempts'/case/'0001'/'task.json', FROZEN/'review-inputs'/f'{case}.json']
    before={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in paths}
    records=[]
    with tempfile.TemporaryDirectory(prefix='b04-replay-') as temp:
        w=Workspace(Settings(data_dir=Path(temp)/'data'))
        try:
            for case,question,targets in specs:
                old=json.loads((FROZEN/'attempts'/case/'0001'/'task.json').read_text())
                sid=old['snapshot_id']; src=FROZEN/'inputs'/sid
                drawing=json.loads((src/'drawing.json').read_text())
                w.store.put('drawing',sid,drawing)
                for name in ('source.dxf','regions.json','entities.json','scene.json','render.json'):
                    p=src/name
                    if name=='render.json' and not p.exists():continue
                    before[str(p.relative_to(ROOT))]=sha256(p.read_bytes()).hexdigest()
                    dest=w.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True);copy_artifact(p,dest)
                s=CadAgentSession(w,ChatRequest(snapshot_id=sid,question=question))
                s.task.plan(TaskPlan(steps=[PlanStep(requirement_id='q1',kind='lookup',acceptance='逐对象保留未读、未命中和归属缺口。',
                    coverage=[CoverageTarget(object=o,field=f) for o,f in targets])]))
                calls=[]
                for _ in range(8):
                    choices=pending_calls(s.task.data)
                    if not choices:break
                    call=choices[0]
                    args=call['arguments']|{'task_ids':call['task_ids']}
                    result=json.loads(s.invoke_tool(call['tool'],args))
                    assert not result.get('error'),result
                    calls.append(dict(tool=call['tool'],arguments=args,priority=call['priority'],reason=call['reason'],
                                      total=result.get('total'),receipt=result.get('recall_receipt'),
                                      source_evidence_ids=list(s.evidence),coverage=deepcopy(s.task.coverage_summary())))
                assert s.calls<=8
                assert not s.task.coverage_ready()
                assert not any(c['state']=='addressed' for step in s.task.data['subtasks'] for c in step['coverage'])
                records.append(dict(case=case,question=question,snapshot_id=sid,source_sha256=drawing['sha256'],
                    calls=calls,tool_calls=s.calls,coverage=s.task.coverage_summary(),pending=pending_calls(s.task.data),
                    engineering_completion=False))
            # Q18 pagination: explicit query, no field inference or broad scan.
            s=CadAgentSession(w,ChatRequest(snapshot_id=sid,question='读取设备房名称候选原文。'))
            first=json.loads(s.invoke_tool('query_entities',dict(query='设备房',alternatives=['机房','电房','配电'],limit=1)))
            assert not first.get('error') and first['has_more']
            choices=pending_calls(s.task.data)
            assert choices[0]['tool']=='read_source_context'
            records.append(dict(case='Q18-pagination',snapshot_id=sid,total=first['total'],
                                first_unread_offset=first['recall_receipt']['first_unread_offset'],pending=choices,
                                engineering_room_count=first['engineering_room_count']))
        finally:w.close()
    assert all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in before.items())
    output=dict(evidence_class='frozen_real_dxf_deterministic_replay',model_calls=0,embedding_calls=0,
        historical_sources_unchanged=True,input_sha256=before,records=records,
        verified=['explicit object-field gaps persist','known source precedes candidate page','bounded calls within existing budget','candidate never becomes addressed'],
        not_verified=['live model choice and end-to-end answer quality','engineering object identity','exhaustive fact recovery','Q12 complete area/count pairing','Q20 manufacturer performance declaration'])
    OUT.mkdir(exist_ok=True)
    (OUT/'b04-replay.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(dict(records=len(records),snapshots=len({r['snapshot_id'] for r in records}),model_calls=0,unchanged=True),ensure_ascii=False))

if __name__=='__main__':replay()
