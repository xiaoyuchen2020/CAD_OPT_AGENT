"""Synthetic-only GPT-6.1 Sol generation, real sandbox, and frozen method holdouts."""
import argparse
import hashlib
import json
import math
import shutil
from dataclasses import replace
from pathlib import Path
import ezdxf
from cad_agent.config import ROOT, Settings
from cad_agent.service import Workspace
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.shadow import ShadowService, Policy, Target
from cad_agent.extensions.adaptation.validation import validate
from scripts.regress_adaptation import dump, sha

OUT=ROOT/'artifacts/dynamic-multimethod-20261016'
METHODS=('text_spans','segment_measurements','segment_distances','closed_contour')
INSTRUCTIONS={
 'text_spans':'返回选定局部中每个非空原生文本的完整连续原文区间，保留实体 ID。不改写、不翻译、不拼接文字，不硬编码文本、ID或条数。',
 'segment_measurements':'返回选定局部中全部原生直线段的完整欧氏长度，保留 segment_id，使用端点计算。不换算单位，不硬编码坐标、长度或条数。',
 'segment_distances':'计算输入原生直线段所有无序不同两两组合之间的最短欧氏距离。必须处理内部交叉、端点相接、共线重叠和非轴向线段；交叉或重叠为0，其余取点到有限线段的最短距离。不把无限延长线距离当结果，不硬编码坐标、ID、距离或数量。',
 'closed_contour':'从原生线段构建唯一简单闭合环，按连接顺序取顶点并计算面积。不限于轴对齐；使用局部原点消除大坐标相消，不取 bbox 面积、不硬编码坐标/ID/数量。若不是唯一简单闭合轮廓则 abstain。仅返回原生 segment_ids 和面积，不作房间或净面积声明。',
}


def fixture(w,name,vertices,texts,closed=True):
    path=OUT/(name+'.dxf')
    if not path.exists():
        doc=ezdxf.new('R2018');doc.units=0;m=doc.modelspace()
        edges=list(zip(vertices,vertices[1:]+vertices[:1])) if closed else vertices
        for a,b in edges:m.add_line(a,b)
        for text,pt in texts:m.add_text(text,dxfattribs={'insert':pt,'height':.4})
        doc.saveas(path)
    drawing=w.ingest(name+'.dxf',path.read_bytes());idx=DrawingIndex(w,drawing['id'])
    points=vertices if closed else [p for e in vertices for p in e]
    bbox=[min(p[0] for p in points)-1,min(p[1] for p in points)-1,max(p[0] for p in points)+1,max(p[1] for p in points)+1]
    return dict(snapshot_id=drawing['id'],layout_id=drawing['layouts'][0]['layout_id'],bbox=bbox,
                synthetic=True,source_path=str(path),source_sha256=drawing['sha256'])


def init():
    OUT.mkdir(exist_ok=True);data=OUT/'data';data.mkdir(exist_ok=True)
    for name in ('adaptation-runtime.json','table-recovery-runtime.json'):
        shutil.copy2(ROOT/'artifacts/dynamic-shadow-near-axis-20261016/data'/name,data/name)
    w=Workspace(replace(Settings.load(),data_dir=data))
    w.siwc_account.close();w.siwc_account=SiwcAccount(ROOT/'artifacts/hybrid-retrieval-20261015/data')
    a=fixture(w,'synthetic-rectangle-A',[(0,0),(10,0),(10,6),(0,6)],[('SYNTHETIC A',(2,2)),('SIZE 10 x 6',(2,4))])
    # Rotated rectangle far from origin, analytically area 48 (8 by 6).
    b=fixture(w,'synthetic-rotated-B',[(1000000,2000000),(1000006.4,2000004.8),(1000002.8,2000009.6),(999996.4,2000004.8)],
        [('SYNTHETIC B',(1000000,2000004)),('UNITS UNKNOWN',(1000000,2000005))])
    x=fixture(w,'synthetic-crossing-C',[((0,0),(6,6)),((0,6),(6,0)),((8,2),(10,2)),((8,5),(10,5))],[],closed=False)
    fixtures=dict(A=a,B=b,C=x);dump(OUT/'fixtures.json',fixtures)
    return w,fixtures


def target(scope,method,**kw):
    return Target(**{k:scope[k] for k in ('snapshot_id','layout_id','bbox')},method=method,
                  instruction=INSTRUCTIONS[method],**kw)


def export(w,event,label):
    service=AdaptationService(w,event['snapshot_id']);run=service.get(event['run_id'])
    record=dict(event=event,run=run,synthetic=True)
    if run.get('accepted_attempt'):
        record['result']=service.result(run['run_id'])
        a=run['attempts'][run['accepted_attempt']-1]
        record['code']=service.artifact('code',a['code_artifact_id'])
        record['execution']=service.artifact('execution',a['execution_id'])
    dump(OUT/(label+'.json'),record)
    print(label,run['status'],run.get('error'),run['model_calls'],flush=True)
    return record


def generate(w,fixtures,resume_after_busy=False):
    assert w.siwc_account.status()['connected'],'ChatGPT subscription is not connected'
    # Probe availability without sending a model request. Preserve a busy run;
    # only explicitly selected busy-only experiments may create one replacement.
    with w.siwc_account.lease():pass
    suffix='v1'
    if resume_after_busy:
        busy=OUT/'provider-unavailable';busy.mkdir(exist_ok=True)
        for method in METHODS:
            path=OUT/(method+'-generated.json');old=json.loads(path.read_text())
            if not (busy/path.name).exists():
                assert old['run'].get('error')=='SIWC_BUSY' and not old['run']['attempts'] and not old['run']['usage']
                dump(busy/path.name,old)
        suffix='after-busy-v1'
    # Different method requests on one region have different target/run IDs and shared caps.
    p=ShadowService(w).create(Policy(idempotency_key='multimethod-synthetic-generation-'+suffix,mode='dynamic',
        targets=[target(fixtures['A'],m,trigger='authorized_read') for m in METHODS],
        model_profile='siwc_sol',instruction='仅调试人工构造的本地方法合约。',max_runs=4,
        max_model_calls=12,max_reserved_tokens=384000,agent_shadow_enabled=True))
    dump(OUT/'generation-policy.json',p)
    for t in p['targets']:
        event=ShadowService(w).trial(p['policy_id'],t['snapshot_id'],target_id=t['target_id'],background=False)
        record=export(w,event,t['method']+'-generated')
        if record['run'].get('error')=='SIWC_BUSY':break


def replay(w,fixtures):
    generated={m:json.loads((OUT/(m+'-generated.json')).read_text()) for m in METHODS}
    assert all(d['run'].get('accepted_attempt') for d in generated.values()),'Generation not validated for every method'
    targets=[target(fixtures['B'],m,reuse_run_id=generated[m]['run']['run_id'],trigger='authorized_read') for m in METHODS]
    targets.append(target(fixtures['C'],'segment_distances',reuse_run_id=generated['segment_distances']['run']['run_id'],trigger='authorized_read'))
    p=ShadowService(w).create(Policy(idempotency_key='multimethod-synthetic-replay-v1',mode='replay',targets=targets,
        instruction='同代码独立构造样本重放',model_profile='siwc_sol',max_runs=5,agent_shadow_enabled=True))
    dump(OUT/'replay-policy.json',p);wire_checks=[]
    for name in ('B','C'):
        scope=fixtures[name];sid=scope['snapshot_id'];q=dict(layout_id=scope['layout_id'],bbox=scope['bbox'],limit=5)
        request=dict(snapshot_id=sid,question='Read synthetic region',agent_policy='legacy')
        for tool in (('query_entities','query_spatial') if name=='B' else ('query_spatial',)):
            normal=CadAgentSession(w,ChatRequest(**request))
            before=normal.invoke_tool(tool,json.dumps(q))
            sidecar=CadAgentSession(w,ChatRequest(**request,adaptation_policy_id=p['policy_id']))
            after=sidecar.invoke_tool(tool,json.dumps(q))
            assert before==after and normal.evidence==sidecar.evidence and normal.observations==sidecar.observations
            # Drain this read before the next; don't race two scopes for the single active sandbox.
            import time
            from cad_agent.extensions.adaptation.capabilities import TRIGGERS
            from cad_agent.extensions.adaptation.shadow import run_key
            expected=[t for t in p['targets'] if t['snapshot_id']==sid and tool in TRIGGERS[t['method']]]
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                try:
                    events=[w.store.get('adaptation_shadow_event',run_key(p['policy_id'],t)) for t in expected]
                    runs=[w.store.get('adaptation_run',e['run_id']) for e in events]
                    if all(r['status'] not in ('queued','running','cancelling') for r in runs):break
                except KeyError:pass
                time.sleep(.1)
            else:raise AssertionError('Sidecar replay timed out')
            wire_checks.append(dict(fixture=name,tool=tool,tool_response_unchanged=True,evidence_unchanged=True,
                observations_unchanged=True,response_sha256=hashlib.sha256(before.encode()).hexdigest(),
                events=events))
    dump(OUT/'agent-read-checks.json',wire_checks)
    for t in p['targets']:
        event=ShadowService(w).trial(p['policy_id'],t['snapshot_id'],target_id=t['target_id'],background=False)
        label='B' if t['snapshot_id']==fixtures['B']['snapshot_id'] else 'C'
        export(w,event,t['method']+'-replay-'+label)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--generate',action='store_true');parser.add_argument('--resume-after-busy',action='store_true');args=parser.parse_args()
    w,fixtures=init()
    try:
        if args.generate:generate(w,fixtures,args.resume_after_busy)
        else:replay(w,fixtures)
    finally:w.close()

if __name__=='__main__':main()
