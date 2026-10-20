"""Frozen real-DXF, local-only B05 replay; historical files remain read-only."""
import json
import shutil
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from cad_agent.artifact_storage import copy_artifact
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.agent import AgentRunner
from cad_agent.analysis.tasks import TaskPlan, PlanStep

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'artifacts/designer-facts-repaired-20261019'
OUT=ROOT/'artifacts/bottleneck-repairs-20261019'
SID='snap_fa5c416d0ba39b836b0eeb8b'


def invoke(s,name,args):
    tick=time.monotonic();print('start',name,args.get('window_id',''),flush=True)
    result=json.loads(s.invoke_tool(name,args|{'task_ids':['q1']}))
    print('done',name,round(time.monotonic()-tick,3),flush=True)
    assert 'error' not in result,(name,result)
    return result


def replay():
    start=time.monotonic();OUT.mkdir(parents=True,exist_ok=True)
    source=FROZEN/'inputs'/SID
    paths=[source/n for n in ('drawing.json','source.dxf','regions.json','scene.json','entities.json')]
    paths += [FROZEN/'review.json']+[FROZEN/'review-inputs'/f'{q}-A.json' for q in ('Q11','Q14','Q15')]
    before={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in paths}
    drawing=json.loads((source/'drawing.json').read_text())
    assert before[str((source/'source.dxf').relative_to(ROOT))]==drawing['sha256']
    reports=[]
    with tempfile.TemporaryDirectory(prefix='b05-replay-') as tmp:
        w=Workspace(Settings(data_dir=Path(tmp)/'data',provider='responses_subscription'))
        try:
            w.store.put('drawing',SID,drawing)
            for n in ('source.dxf','regions.json','scene.json','entities.json'):
                dst=w.store.artifact(SID,n);dst.parent.mkdir(parents=True,exist_ok=True);copy_artifact(source/n,dst)
            for case,names,question in [
                ('Q11',['机动车坡道'],'测量地下一层平面图机动车坡道净宽。'),
                ('Q14',['集水坑','停车位','门扇'],'核对地下一层平面图集水坑、停车位、门扇的实际几何。'),
                ('Q15',[],'核对地下一层平面图建筑外轮廓与边界。')]:
                old=json.loads((FROZEN/'review-inputs'/f'{case}-A.json').read_text())
                s=AgentRunner(w,ChatRequest(snapshot_id=SID,question=question,mode='agent',runtime='responses_subscription'))
                s.task.plan(TaskPlan(steps=[PlanStep(requirement_id=p['id'],kind='measure',acceptance='局部源图几何候选及保留未确认项。',operations=['geometry']) for p in s.task.data['subtasks']]))
                resolved=invoke(s,'resolve_drawing_scope',dict(floor_title='地下一层平面图',names=names))
                scope=resolved['selected_scope'];assert scope['frame_id']=='frame_19e0c1f200dc3fb1c32048ed'
                calls=resolved['next_calls'];pages=[]
                if calls[0]['arguments']['window_id']:
                    for c in calls[:2]:pages.append(invoke(s,c['tool'],c['arguments']))
                else:
                    catalog=invoke(s,'inspect_scope_objects',dict(scope_id=scope['scope_id']))
                    # Two interior overlapping tiles. These remain investigation
                    # windows, never invented object identity or complete scope.
                    windows=[x for x in s.task.data['drawing_scopes'][scope['scope_id']]['object_workflow']['windows'] if x['role']=='bounded_tile' and x['depth']==0]
                    for wi in (5,10):
                        pages.append(invoke(s,'inspect_scope_objects',dict(scope_id=scope['scope_id'],window_id=windows[wi]['window_id'])))
                for p in pages:
                    assert p['unit']=='unknown' and p['items']
                    assert p['window']['bbox']!=scope['bbox']
                    assert all(not r['fulfilled'] for r in p['operation_receipts'])
                    assert all(i['identity_status']=='unconfirmed' for i in p['items'])
                selected=pages[0]
                # Follow complexity-driven subdivision once, rather than render
                # or measure the overlarge parent window.
                if selected['subdivision_windows']:
                    child=selected['subdivision_windows'][0]
                    selected=invoke(s,'inspect_scope_objects',dict(scope_id=scope['scope_id'],window_id=child['window_id']))
                    pages.append(selected)
                read_call=next(c for c in selected['review_calls'] if c['tool']=='read_geometry')
                raw=invoke(s,read_call['tool'],read_call['arguments'])
                entity=raw['items'][0]['entity_id']
                measurement=invoke(s,'measure_geometry',dict(operation='length',entity_ids=[entity]))
                local_image=None
                render_call=next((c for c in selected['review_calls'] if c['tool']=='render_region'),None)
                if case=='Q11' and render_call:
                    local_image=invoke(s,'render_region',render_call['arguments'])
                    from cad_agent.geometry.vision import VisionService
                    png=VisionService(w,SID).path(local_image['image_id'])
                    copy_artifact(png,OUT/'b05-q11-local-source.png')
                    local_image['offline_copy']=str(OUT/'b05-q11-local-source.png')
                assert measurement['status']=='unresolved' and measurement['value'] is None
                assert 'unknown_units' in measurement['diagnostics']
                assert not any(r['fulfilled'] and r['operation']=='geometry' for r in s.task.data['subtasks'][0]['operation_receipts'])
                report=dict(case=case,frozen_question=old['question'],replay_question=question,
                    source_sha256=drawing['sha256'],scope_id=scope['scope_id'],frame_id=scope['frame_id'],
                    selected_scope=scope,resolution=resolved,object_pages=pages,raw_geometry=raw,
                    attempted_measurement=measurement,local_image=local_image,tool_calls=s.task.data['subtasks'][0]['calls'],
                    tool_trace=s.trace,operation_receipts=s.task.data['subtasks'][0]['operation_receipts'],
                    acceptance='workflow_and_source_binding_verified_object_semantics_unconfirmed',
                    remaining=['实际对象身份未核定','米制单位未知','局部窗未覆盖整层','未确认净宽/建筑外边界/全量对象数量'])
                reports.append(report)
                (OUT/f'b05-{case.lower()}-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
                print(case,'windows',len(pages),'candidates',[p['total'] for p in pages],'calls',report['tool_calls'],flush=True)
        finally:w.close()
    after={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in paths}
    assert before==after
    summary=dict(evidence_class='frozen_real_dxf_offline_workflow_replay',model_calls=0,deployments=0,
        source_snapshot_id=SID,source_sha256=drawing['sha256'],historical_inputs_unchanged=before==after,
        input_hashes=before,cases=[dict(case=r['case'],windows=len(r['object_pages']),
            candidate_counts=[p['total'] for p in r['object_pages']],source_record_counts=[p['source_record_count'] for p in r['object_pages']],
            tool_calls=r['tool_calls'],measurement_status=r['attempted_measurement']['status'],
            object_identity_confirmed=False) for r in reports],elapsed_seconds=round(time.monotonic()-start,3),
        limitations=['No live model or published answer rerun.','No full-plan object census or final outer contour.','Unknown units remain unknown; no metres inferred.'])
    (OUT/'b05-validation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary['cases'],ensure_ascii=False))


if __name__=='__main__':replay()
