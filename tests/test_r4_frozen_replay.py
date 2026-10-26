"""Offline replay against the latest frozen Q11/Q14/Q15 B sources.

Run directly to write auditable report; pytest uses a temporary output directory.
"""
import json
import shutil
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.analysis.scope_workflow import record_scope_failure
from cad_agent.geometry.vision import VisionService
from test_remediation_scope import session, invoke

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'artifacts/designer-facts-b-final-20261019'
SID='snap_fa5c416d0ba39b836b0eeb8b'


def replay(out, case_ids=('Q11','Q14','Q15')):
    out.mkdir(parents=True,exist_ok=True); start=time.monotonic()
    source=FROZEN/'inputs'/SID
    paths=[source/n for n in ('drawing.json','source.dxf','scene.json','regions.json','entities.json')]
    paths += [FROZEN/'review.json']
    for q in ('Q11','Q14','Q15'):
        paths += [FROZEN/'attempts'/f'{q}-B'/'0001'/n for n in ('run.json','task.json')]
    hashes={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in paths}
    drawing=json.loads((source/'drawing.json').read_text()); assert drawing['sha256']==sha256((source/'source.dxf').read_bytes()).hexdigest()
    reviews=json.loads((FROZEN/'review.json').read_text()); reports=[]
    with tempfile.TemporaryDirectory(prefix='r4-frozen-') as tmp:
        # Offline source replay performs cold parsing repeatedly under test
        # contention; this is not evidence of delivery within production SLA.
        w=Workspace(Settings(llm_query_intent=False, data_dir=Path(tmp)/'data',provider='responses_subscription',agent_timeout_seconds=600))
        try:
            w.store.put('drawing',SID,drawing)
            for name in ('source.dxf','scene.json','regions.json','entities.json'):
                dest=w.store.artifact(SID,name);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,dest)
            for q in case_ids:
                old=json.loads((FROZEN/'attempts'/f'{q}-B'/'0001'/'run.json').read_text())
                # Preserve actual user question, including explicit mm assumption.
                s=session(w,drawing,old['request']['question'])
                trace=[]
                def call(name,args):
                    result=invoke(s,name,args|{'task_ids':['q1']})
                    trace.append(dict(tool=name,arguments=args,result=result))
                    print(q,name,'error' if 'error' in result else 'ok',flush=True)
                    if result.get('result_excerpt'):
                        # The public transport now archives large results.
                        # Audit the full record through the same task-scoped
                        # readback implementation, without pretending this
                        # exhaustive offline audit is a model's chosen reads.
                        # Real model budgets and the production counters stay
                        # unchanged; this test proves source-method progress,
                        # not a complete answer within a model budget.
                        from cad_agent.analysis.evidence_store import read as read_record, EvidenceRecordRead
                        ref=result['evidence_record'];chunks=[];cursor=0
                        counters=(s.calls,s.policy.response_chars)
                        audit=[]
                        while True:
                            page=read_record(s,EvidenceRecordRead(
                                record_id=ref['record_id'],start=cursor,length=12000))
                            assert 'error' not in page,page
                            chunks.append(page['content'])
                            audit.append(dict(record_id=ref['record_id'],start=cursor,end=page['end']))
                            if not page['has_more']:break
                            cursor=page['next_start']
                        assert counters==(s.calls,s.policy.response_chars)
                        trace[-1]['validation_only_archive_readback']=audit
                        text=''.join(chunks)
                        assert sha256(text.encode()).hexdigest()==ref['sha256']
                        result=json.loads(text)
                    return result
                resolved=call('resolve_drawing_scope',dict(floor_title='地下一层'))
                scope=resolved['selected_scope']; full=s.task.data['drawing_scopes'][scope['scope_id']]
                assert scope['frame_id']=='frame_19e0c1f200dc3fb1c32048ed'
                recovery=None; targets=[]; rendered=None
                if q in ('Q11','Q14'):
                    query=next(t['arguments'] for t in old['trace'] if t['tool']=='query_entities')
                    found=call('query_entities',query)
                    assert found['target_window_count'] > 0
                    targets=full['object_workflow']['anchors']
                    if q=='Q11':
                        motor=[a for a in targets if '机动车坡道' in a['text'] and '非机动车' not in a['text']]
                        assert len(motor)==2
                        selected=motor[0]['window_id']
                    else:
                        assert all('施工前' not in a['text'] for a in targets)
                        assert any(a['text']=='消火栓' for a in targets)
                        selected=next(a['window_id'] for a in targets if a['text']=='消火栓')
                    page=call('inspect_scope_objects',dict(scope_id=scope['scope_id'],window_id=selected))
                else:
                    failed=next(t for t in old['trace'] if t['tool']=='render_region' and 'SCOPE_BOUNDS' in str(t.get('error')))
                    failure=call('render_region',failed['arguments'])
                    assert 'SCOPE_BOUNDS' in failure['error']['message']
                    # Allows independent replay before the parent's executor hook lands.
                    if 'recovery' not in failure:record_scope_failure(s,'render_region',failed['arguments'],failure)
                    recovery=failure['recovery']; step=recovery['next_calls'][0]
                    page=call(step['tool'],step['arguments'])
                pages=[page]
                if page.get('reference_context'):
                    # A legend sample is not a placed hydrant/door/parking
                    # object. Continue outside its proven native enclosure.
                    step=page['next_calls'][0]
                    page=call(step['tool'],step['arguments'])
                    pages.append(page)
                for _ in range(3):
                    if not page.get('subdivision_windows'):break
                    child=page['subdivision_windows'][0]
                    page=call('inspect_scope_objects',dict(scope_id=scope['scope_id'],window_id=child['window_id']))
                    pages.append(page)
                assert 'error' not in page and page['items']
                assert page['candidate_pairs'],page
                step=next(c for c in page['review_calls'] if c['tool']=='read_geometry')
                raw=call(step['tool'],step['arguments']); assert raw['items']
                image_call=next((c for c in page['review_calls'] if c['tool']=='render_region'),None)
                if image_call:
                    rendered=call(image_call['tool'],image_call['arguments'])
                    if 'error' not in rendered:
                        image=VisionService(w,SID).path(rendered['image_id'])
                        shutil.copy2(image,out/f'r4-{q.lower()}-local.png')
                pair=page['candidate_pairs'][0]
                # Raw native geometry calculation is executable independently of
                # future model identity review and does not assert a clear width.
                measured=call(pair['measure_call']['tool'],pair['measure_call']['arguments'])
                assert measured.get('raw_value') is not None or measured.get('value') is not None,measured
                assert all(p['identity_status']=='unconfirmed' for p in page['candidate_pairs'])
                result=dict(case=q,frozen_run_id=old['id'],frozen_review=next(r for r in reviews if r['id']==q+'-B'),
                    source_sha256=drawing['sha256'],selected_scope=scope,actual_target_anchors=targets,
                    pages=pages,source_geometry=raw,render=rendered,measurement=measured,recovery=recovery,
                    tool_trace=trace,object_identity_confirmed=False,
                    acceptance='deterministic_source_bound_progress_only_not_semantic_answer_acceptance')
                reports.append(result)
                (out/f'r4-{q.lower()}-replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        finally:w.close()
    assert hashes=={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in paths}
    report=dict(evidence_class='frozen_latest_B_real_dxf_offline_replay',model_calls=0,source_read_batching_default=False,
        offline_agent_timeout_seconds=600,production_default_timeout_changed=False,
        source_snapshot_id=SID,source_sha256=drawing['sha256'],historical_inputs_unchanged=True,input_hashes=hashes,
        elapsed_seconds=round(time.monotonic()-start,3),cases=[dict(case=r['case'],target_anchors=len(r['actual_target_anchors']),
            legend_reference_detected=any(p.get('reference_context') for p in r['pages']),
            local_pages=len(r['pages']),native_pairs=len(r['pages'][-1]['candidate_pairs']),
            source_records=len(r['source_geometry']['items']),render_succeeded=bool(r['render'] and 'error' not in r['render']),
            measurement_status=r['measurement']['status'],raw_value=r['measurement'].get('raw_value'),value=r['measurement'].get('value'),
            recovery_calls=len((r['recovery'] or {}).get('next_calls',[])),object_identity_confirmed=False) for r in reports],
        root_causes=['Late query_entities labels were not connected to scope windows.',
          'Catalog pagination and first candidate page did not progress to local source-path pairing and review.',
          'Render failure had no exact in-scope subdivision calls; rounded bbox exceeded bounds.',
          'Unknown units discarded raw geometry values rather than retaining reviewable candidates.'],
        limitations=['No model calls, new answers, or business/engineering acceptance.',
          'Native-path pairs are not confirmed ramp sides, hydrant/parking pairs, door swings or main outer boundaries.',
          'Local samples are not full-plan enumeration; unsupported identity remains unconfirmed.'])
    (out/'r4-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def test_latest_frozen_scope_progress(tmp_path):
    report=replay(tmp_path/'r4')
    assert report['historical_inputs_unchanged']
    assert len(report['cases'])==3
    assert all(case['native_pairs'] for case in report['cases'])


if __name__=='__main__':
    print(json.dumps(replay(ROOT/'artifacts/bottleneck-round2-20261019'),ensure_ascii=False,indent=2))
