"""One bounded model revision, then local frozen-code regressions in a new workspace."""
import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from cad_agent.artifact_storage import copy_artifact, copy_artifact_tree
from cad_agent.config import ROOT, Settings
from cad_agent.service import Workspace
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.shadow import ShadowService, Policy, Target
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest, stable_id
from scripts.regress_adaptation import baseline, dump, sha

OUT=ROOT/'artifacts/dynamic-shadow-near-axis-20261016'
CROSS=ROOT/'artifacts/dynamic-cross-drawing-20261016'
PRIOR=ROOT/'artifacts/dynamic-adaptation-20261016'


def init():
    OUT.mkdir(exist_ok=True)
    data=OUT/'data'
    if not (data/'index.sqlite3').exists():
        data.mkdir(exist_ok=True)
        copy_artifact_tree(CROSS/'data/drawings',data/'drawings',dirs_exist_ok=True)
        src=sqlite3.connect(f'file:{CROSS}/data/index.sqlite3?mode=ro',uri=True)
        dst=sqlite3.connect(data/'index.sqlite3');src.backup(dst);src.close();dst.close()
        for name in ('adaptation-runtime.json','table-recovery-runtime.json'):
            copy_artifact(CROSS/'data'/name,data/name)
    w=Workspace(replace(Settings.load(),data_dir=data,max_upload_bytes=256*1024*1024))
    # Reuse the app's independent subscription in place; never copy credentials into this audit.
    w.siwc_account.close()
    w.siwc_account=SiwcAccount(ROOT/'artifacts/hybrid-retrieval-20261015/data')
    return w


def generate(w):
    source=json.loads((CROSS/'sample-60-33-table_grid-replay.json').read_text());r=source['run']
    assert w.siwc_account.status()['connected'], 'ChatGPT direct subscription is not connected'
    policy=ShadowService(w).create(Policy(idempotency_key='near-axis-generation-v1',model_profile='siwc_sol',
        targets=[Target(snapshot_id=r['snapshot_id'],table_id=r['request']['table_id'],diagnostic_run_id=r['run_id'])],
        mode='dynamic',max_runs=1,max_model_calls=3,max_reserved_tokens=96000,
        instruction='修正旧代码对近轴原生分隔线的识别：旧版以 tolerance*1e-6 判断水平或竖直，可能遗漏宿主 tolerance 内的近轴线。请基于原生图元和宿主容差生成通用新版本，保留原有轴线与精确边界，只补有原生支持的轴线。不要硬编码坐标、格数、样本名，不改输入、验证器或 sdk。存在无法消解的歧义时返回未解决。'))
    print('MODEL_SCOPE',policy['endpoint'],policy['model'],'sample-60-33 max 3 calls',flush=True)
    event=ShadowService(w).trial(policy['policy_id'],r['snapshot_id'],r['request']['table_id'],background=False)
    service=AdaptationService(w,r['snapshot_id']);run=service.get(event['run_id'])
    result=dict(source=source['source'],case_id='sample-60-33',baseline=source['baseline'],run=run,
                source_unchanged=sha(Path(source['source']['path']))==source['source']['sha256'],event=event)
    if run.get('accepted_attempt'):result['result']=service.result(run['run_id'])
    dump(OUT/'sample-60-33-table_grid-dynamic.json',result)
    dump(OUT/'generation-policy.json',policy)
    print('GENERATED',run['status'],run.get('error'),run['model_calls'],run['usage'],flush=True)


def regress(w, source_id=None):
    generated=json.loads((OUT/'sample-60-33-table_grid-dynamic.json').read_text())
    origin=generated['run'];assert origin.get('accepted_attempt'),'No validated new code to replay'
    cases=[json.loads(p.read_text()) for p in sorted(CROSS.glob('*-table_grid-replay.json'))]
    cases += [json.loads((PRIOR/f'11816-{i}-table_grid-replay.json').read_text()) for i in (27,28,36)]
    policy=ShadowService(w).create(Policy(idempotency_key='near-axis-regression-v1',model_profile='siwc_sol',
        targets=[Target(snapshot_id=d['run']['snapshot_id'],table_id=d['run']['request']['table_id'],
                        diagnostic_run_id=d['run']['run_id'] if d['case_id']=='sample-60-33' else None) for d in cases],
        mode='replay',reuse_run_id=origin['run_id'],max_runs=19,agent_shadow_enabled=True,
        instruction='冻结新版本，同代码跨图纸旁路回归；不修改原组件，不自动采用结果。'))
    dump(OUT/'regression-policy.json',policy)
    if not (OUT/'agent-shadow-check.json').exists():
        case=next(d for d in cases if d['case_id']=='sample-60-33')
        sid=case['run']['snapshot_id'];tid=case['run']['request']['table_id']
        args=dict(snapshot_id=sid,question='读取该表格并保留来源',agent_policy='legacy')
        normal=CadAgentSession(w,ChatRequest(**args))
        sidecar=CadAgentSession(w,ChatRequest(**args,adaptation_policy_id=policy['policy_id']))
        wire=normal.invoke_tool('read_table',json.dumps(dict(table_id=tid)))
        shadow_wire=sidecar.invoke_tool('read_table',json.dumps(dict(table_id=tid)))
        assert wire==shadow_wire and normal.evidence==sidecar.evidence and normal.observations==sidecar.observations
        key=stable_id('adobserve',policy['policy_id'],sid,tid);deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            try:
                event=w.store.get('adaptation_shadow_observation',key)
                assert event['status']=='queued',event
                run=w.store.get('adaptation_run',event['run_id'])
                if run['status'] not in ('queued','running','cancelling'):break
            except KeyError:pass
            time.sleep(.2)
        else:raise AssertionError('Agent sidecar did not finish')
        assert event['initiator']=='agent_shadow' and run['model_calls']==0
        dump(OUT/'agent-shadow-check.json',dict(event=event,run_id=run['run_id'],
            unchanged_tool_response=True,unchanged_evidence=True,unchanged_observations=True,
            response_sha256=hashlib.sha256(wire.encode()).hexdigest(),
            agent_reasoning_model_calls=0,sidecar_model_calls=0,trigger='real CadAgentSession.invoke_tool read_table'))
        print('AGENT_SHADOW',run['status'],'ordinary tool response unchanged',flush=True)
    indices={}
    for d in cases:
        if source_id and d['source']['id']!=source_id:continue
        target=OUT/(d['case_id']+'-table_grid-replay.json')
        if target.exists():print('EXISTS',d['case_id'],flush=True);continue
        sid=d['run']['snapshot_id'];tid=d['run']['request']['table_id']
        if sid not in indices:indices={sid:DrawingIndex(w,sid)}
        idx=indices[sid];table=next(t for t in idx.tables if t['table_id']==tid);before=baseline(idx,table)
        event=ShadowService(w).trial(policy['policy_id'],sid,tid,background=False)
        service=AdaptationService(w,sid);run=service.get(event['run_id'])
        after=baseline(idx,table);assert before==after
        old={k:v for k,v in d['baseline'].items() if k not in ('index','table_id','bbox')}
        result=dict(source=d['source'],case_id=d['case_id'],baseline=before,prior_baseline=old,
            existing_component_unchanged=True,run=run,previous_run=d['run'],event=event,
            source_unchanged=sha(Path(d['source']['path']))==d['source']['sha256'])
        assert run['model_calls']==0
        if run.get('accepted_attempt'):result['result']=service.result(run['run_id'])
        dump(target,result)
        print('REPLAY',d['case_id'],run['status'],run.get('error'),len(result.get('result',{}).get('validation',{}).get('cells',[])),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate',action='store_true');parser.add_argument('--source')
    args=parser.parse_args();logging.getLogger('ezdxf').setLevel(logging.ERROR)
    w=init()
    try:
        if args.generate:generate(w)
        else:regress(w,args.source)
    finally:w.close()


if __name__=='__main__':main()
