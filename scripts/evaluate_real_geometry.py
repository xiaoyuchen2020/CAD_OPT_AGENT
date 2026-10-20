"""Fresh real DXF HTTP ingestion and deterministic geometry-tool delivery replay.

This is geometry_pipeline_e2e, never a live LLM accuracy evaluation. Credentials,
production data and historical derived artifacts are not loaded into the workspace.
"""
from __future__ import annotations
import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import logging
from pathlib import Path
import platform
import statistics
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from shapely.geometry import Point, Polygon
from cad_agent.artifact_storage import copy_artifact
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.compute import LocalGeometryExecutor, GeometryExecutor

logging.getLogger('ezdxf').setLevel(logging.ERROR)

MANIFEST=ROOT/'tests/fixtures/real_geometry_tasks.json'


@dataclass(frozen=True)
class OfflineSettings(Settings):
    def model_catalog(self):
        # Do not call Settings.model_catalog: it reads the user's .env.
        return {'default':self}


def record_event(executor,event):
    executor.events.append(event)
    print('operation',json.dumps(event),flush=True)


class MeasuredLocalExecutor(LocalGeometryExecutor):
    def __init__(self): self.events=[]
    def compute(self,operation,payload,timeout=None):
        start=time.perf_counter()
        try:return super().compute(operation,payload,timeout)
        finally:record_event(self,dict(operation=operation,elapsed_seconds=time.perf_counter()-start,worker='in_process'))
    def close(self):pass


class MeasuredSpawnExecutor(GeometryExecutor):
    def __init__(self):
        super().__init__();self.events=[]
    def compute(self,operation,payload,timeout=None):
        start=time.perf_counter();self.last_timing=None
        try:return super().compute(operation,payload,timeout)
        finally:record_event(self,dict(operation=operation,elapsed_seconds=time.perf_counter()-start,timing=self.last_timing))


def percentile(values,q):
    return sorted(values)[max(0,math.ceil(len(values)*q)-1)] if values else None


def summarize(rows):
    seconds=[r['seconds'] for r in rows]
    return dict(attempts=len(rows),complete=sum(r['complete'] for r in rows),geometry_complete=sum(r.get('geometry_complete',False) for r in rows),delivery_complete=sum(r.get('delivery_complete',False) for r in rows),
                contract_pass=sum(r['contract_pass'] for r in rows),
                failures=sum(not r['contract_pass'] for r in rows),
                p50_seconds=statistics.median(seconds) if seconds else None,p95_seconds=percentile(seconds,.95))


def score_annotations(openings,samples,distance=180):
    """One-to-one spatial assignment; no duplicate can earn multiple true positives.

    Recall denominator is only the frozen annotated set. Precision denominator is
    typed predictions spatially inside the annotated neighborhoods. Unmatched
    predictions outside annotation coverage stay explicit, not labelled false.
    """
    pairs=[]
    for j,sample in enumerate(samples):
        for i,opening in enumerate(openings):
            d=Polygon(opening['boundary']).distance(Point(sample['anchor']))
            if d<=distance:pairs.append((d,j,i))
    assigned,used={},set()
    for d,j,i in sorted(pairs):
        if j not in assigned and i not in used:
            assigned[j]=i;used.add(i)
    eligible={i for _,_,i in pairs}
    typed={i for i,o in enumerate(openings) if o.get('opening_type') in ('door_candidate','window_candidate')}
    correct=0;rows=[];operation_total=operation_correct=0
    for j,sample in enumerate(samples):
        i=assigned.get(j);opening=openings[i] if i is not None else None
        match=bool(opening and opening.get('opening_type') == sample['expected_identity']+'_candidate')
        correct+=int(match)
        if sample.get('expected_operation') is not None:
            operation_total+=1
            operation_correct+=int(bool(match and opening.get('operation_type')==sample['expected_operation']))
        rows.append(dict(sample_id=sample['id'],opening_id=opening.get('object_id') if opening else None,
                         expected_identity=sample['expected_identity'],observed_identity=opening.get('opening_type') if opening else None,
                         identity_match=match))
    denominator=len(typed & eligible)
    return dict(reference_kind='frozen_agent_review_consistency_not_independent_human_accuracy',
                annotated_objects=len(samples),matched_identity=correct,typed_predictions_in_annotation_coverage=denominator,
                reference_precision=correct/denominator if denominator else None,
                reference_recall=correct/len(samples) if samples else None,
                operation_matches=operation_correct,operation_annotated=operation_total,
                unscored_typed_predictions_outside_annotation_coverage=len(typed-eligible),samples=rows)


def normalize(value):
    if isinstance(value,dict):
        return {k:normalize(v) for k,v in value.items() if k not in {'created_at','source_stamp'}}
    if isinstance(value,list):return [normalize(v) for v in value]
    return value


def digest_result(graph):
    return sha256(json.dumps(normalize(graph),sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def clean_derived(workspace,cold):
    # This database is under this runner's temporary directory only. Removing
    # derived records ensures every timed attempt actually calls geometry workers.
    with workspace.store.connect() as conn:
        conn.execute("DELETE FROM records WHERE kind IN ('component','measurement')")
    if cold:
        from cad_agent.geometry import source
        from cad_agent.cache import BoundedCache
        if hasattr(source, '_WORKER_NATIVE'):
            source._WORKER_NATIVE = BoundedCache(max_entries=4)
        for name in ('_reading_cache','_scene_cache','_native_geometry','_geometry_cache'):
            if hasattr(workspace,name):delattr(workspace,name)


def next_page_offset(page, current_offset):
    """Honor server pagination; never infer progress from the requested limit."""
    value=page.get('next_offset')
    if value is None:value=current_offset+len(page.get('items',[]))
    if page.get('has_more') and (not isinstance(value,int) or value<=current_offset):
        raise ValueError('Pagination did not advance')
    return value


def tool_delivery(client,workspace,drawing,params,graph,requested_limit,out,label,policy='legacy',require_full_objects=False):
    start=time.perf_counter();events_before=len(workspace.geometry_executor.events)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='核对这些洞口候选',mode='agent',agent_policy=policy))
    row=dict(policy=policy,requested_limit=requested_limit,complete=False,contract_pass=False)
    tool_pages=[];tool_items=[];offset=0;requests=[]
    params=dict(params);params.pop('limit',None)
    if requested_limit!='default':params['limit']=int(requested_limit)
    try:
        while True:
            tool='query_building_relations' if not tool_pages else 'get_component_evidence'
            request=params|{'offset':offset} if not tool_pages else dict(record_id=graph['record_id'],view='openings',offset=offset)
            if tool_pages and requested_limit!='default':request['limit']=int(requested_limit)
            raw=session.invoke_tool(tool,request)
            observation=json.loads(raw)
            requests.append(dict(tool=tool,request=request,response_characters=len(raw),response_bytes=len(raw.encode()),
                effective_limit=observation.get('effective_limit'),safe_limit=observation.get('safe_limit'),pagination_budget=observation.get('pagination_budget'),next_offset=observation.get('next_offset'),
                has_more=observation.get('has_more'),returned_items=len(observation.get('items',[]))))
            if 'error' in observation:raise ValueError(str(observation['error']))
            tool_pages.append(observation);tool_items.extend(observation['items'])
            if not observation['has_more']:break
            offset=next_page_offset(observation,offset)
            if len(tool_pages)>100:raise ValueError('Unbounded pagination')
        payload={'claims':[{'text':'这些为选定范围内的洞口候选，身份和房间关系仍需复核。',
                           'state':'derived','evidence_basis':'tool_observation',
                           'observation_ids':[item['observation_id'] for item in tool_pages[:4]]}]}
        delivery=session.finish(json.dumps(payload,ensure_ascii=False))
        task=client.get('/api/tasks/'+delivery['task']['task_id']);task.raise_for_status()
        checks=dict(same_http_tool_record=all(o['record_id']==graph['record_id'] for o in tool_pages),
                    tool_pagination_complete=len(tool_items)==len(graph['openings']) and {o['object_id'] for o in tool_items}=={o['object_id'] for o in graph['openings']},
                    task_readback=task.json()['task_id']==delivery['task']['task_id'],
                    observation_cited=bool(delivery.get('tool_observations')),task_completed=delivery['task']['status']=='completed')
        row['full_object_content_and_order_equal']=normalize(tool_items)==normalize(graph['openings'])
        if require_full_objects:checks['full_object_content_and_order_equal']=row['full_object_content_and_order_equal']
        (out/(label+'-delivery.json')).write_text(json.dumps(delivery,ensure_ascii=False,indent=2))
        row.update(complete=True,contract_pass=all(checks.values()),checks=checks,delivery_file=label+'-delivery.json',task_status=delivery['task']['status'])
    except Exception as exc:row.update(error=type(exc).__name__,message=str(exc))
    (out/(label+'-tool-pages.json')).write_text(json.dumps(tool_pages,ensure_ascii=False,indent=2))
    row.update(seconds=time.perf_counter()-start,requests=requests,tool_calls=len(requests),objects=len(tool_items),
               unknown_objects=sum(i.get('opening_type') not in ('door_candidate','window_candidate') for i in tool_items),
               session_metrics=session.metrics.snapshot(),geometry_calls=len(workspace.geometry_executor.events)-events_before)
    return row


def attempt(client,workspace,drawing,case,backend,phase,repeat,out,pagination_limits=('1',),policies=('legacy',),require_full_objects=False):
    started=time.perf_counter();stage={};executor=workspace.geometry_executor
    event_start=len(executor.events)
    base=f'/api/drawings/{drawing["id"]}'
    row=dict(case=case['case'],backend=backend,phase=phase,repeat=repeat,complete=False,contract_pass=False)
    failure_stage='http_geometry'
    try:
        lid=next(l['layout_id'] for l in drawing['layouts'] if l['name']=='Model')
        params=dict(layout_id=lid,bbox=case['bbox'],max_opening_m=case.get('max_opening_m',3),unit_override=case.get('unit_override','mm'),view='openings',limit=1)
        tick=time.perf_counter()
        response=client.post(base+'/components/relations',json=params)
        stage['http_geometry_seconds']=time.perf_counter()-tick
        response.raise_for_status();page=response.json()
        readback=client.get(page['evidence_url']);readback.raise_for_status();graph=readback.json()
        label=f'{backend}-{phase}-{repeat}-{case["case"]}'
        (out/(label+'-graph.json')).write_text(json.dumps(graph,ensure_ascii=False,indent=2))
        row.update(geometry_complete=True,graph_hash=digest_result(graph),graph_file=label+'-graph.json',
                   annotations=score_annotations(graph['openings'],case.get('annotation_samples',[])))
        failure_stage='http_pagination'
        pages=list(page['items']);offset=0;http_requests=[]
        while True:
            http_requests.append(dict(offset=offset,requested_limit=1,effective_limit=page.get('effective_limit'),next_offset=page.get('next_offset'),items=len(page['items'])))
            if not page['has_more']:break
            offset=next_page_offset(page,offset)
            response=client.post(base+'/components/relations',json=params|{'offset':offset})
            response.raise_for_status();page=response.json()
            if not page['items']:raise ValueError('Pagination did not advance')
            pages.extend(page['items'])
        failure_stage='tool_pagination_and_delivery';tick=time.perf_counter()
        variants=[tool_delivery(client,workspace,drawing,params,graph,limit,out,label+'-'+policy+'-limit-'+str(limit),policy,require_full_objects) for policy in policies for limit in pagination_limits]
        stage['tool_and_delivery_seconds']=time.perf_counter()-tick
        checks=dict(source_hash=graph['source_sha256']==case['sha256'],
                    pagination_complete=len(pages)==len(graph['openings'])==page['total'] and {p['object_id'] for p in pages}=={p['object_id'] for p in graph['openings']},
                    all_tool_variants_pass=all(v['contract_pass'] for v in variants),
                    geometry_executed=len(executor.events)>event_start,
                    no_delivery_geometry=all(v['geometry_calls']==0 for v in variants))
        delivered=all(v['complete'] for v in variants)
        row.update(complete=delivered,delivery_complete=delivered,contract_pass=all(checks.values()),checks=checks,
                   http_pagination=http_requests,pagination_variants=variants)
        if not delivered:row.update(failure_stage=failure_stage,message='One or more requested pagination variants failed; retained in pagination_variants')
    except Exception as exc:
        row.update(error=type(exc).__name__,message=str(exc),failure_stage=failure_stage)
    events=executor.events[event_start:]
    row.update(seconds=time.perf_counter()-started,stages=stage,geometry_events=events,
               geometry_operation_counts={op:sum(e['operation']==op for e in events) for op in sorted({e['operation'] for e in events})})
    return row


def reuse_imports(source_root, target_store, cases):
    """Copy only this run's verified parsed inputs, never geometry results."""
    with sqlite3.connect(f"file:{source_root / 'index.sqlite3'}?mode=ro",uri=True) as conn:
        drawings=[json.loads(raw) for raw, in conn.execute("SELECT data FROM records WHERE kind='drawing'")]
    for case in cases:
        drawing=next(d for d in drawings if d['sha256']==case['sha256'])
        origin=source_root/'drawings'/drawing['id']
        if sha256((origin/'source.dxf').read_bytes()).hexdigest()!=case['sha256']:
            raise ValueError('Reused source hash mismatch')
        for name in ('source.dxf','scene.json','regions.json','entities.json','render.json'):
            path=origin/name
            if path.exists():copy_artifact(path,target_store.artifact(drawing['id'],name))
        target_store.put('drawing',drawing['id'],drawing)


def evaluate(manifest_path,output,repeats=1,backends=('local','spawn'),phases=('cold','warm','alternating'),retain_data=False,resume=False,reuse_import_backend=None,reuse_import_root=None,provenance_report=None,pagination_limits=('1',),baseline_report=None,policies=('legacy',),require_full_objects=False):
    manifest_path=Path(manifest_path);out=Path(output);out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads(manifest_path.read_text());cases=manifest['cases'];rows=[];imports=[]
    code={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'cad_agent').rglob('*.py'))}
    report=dict(runtime_mode='geometry_pipeline_e2e',driver='deterministic_tools_no_llm',model_calls=0,
        independent_engineering_accuracy={'precision':None,'recall':None,'area_error':None,'reason':'No independent human labels or room area truth; frozen agent review consistency reported separately.'},
        annotation_provenance={k:manifest[k] for k in ('annotation_source','evidence_class','sampling')},
        require_full_objects=require_full_objects,policies=list(policies),pagination_limits=list(pagination_limits),repeats=repeats,access_order_note='With repeats=1 warm and alternating orders coincide; these are repeated-access rounds, not an independent cache-thrashing test.' if repeats==1 else 'Grouped versus alternating repeated access.',
        settings={'max_upload_bytes':256*1024*1024,'production_default_upload_limit_tested':False,'cache_budgets':'unchanged production defaults'},
        phases_definition={'cold':'memory caches reset and spawn worker restarted; OS file cache not reset',
            'warm':'grouped same drawing; derived component/measurement records removed each attempt',
            'alternating':'alternate two drawings; derived results removed each attempt'},
        environment={'python':platform.python_version(),'platform':platform.platform(),'source_root':str(ROOT),'cad_agent_module_file':str(Path(sys.modules['cad_agent'].__file__).resolve())},code_hashes=code,
        manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),runner_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),imports=imports,observations=rows)
    if resume:
        if not retain_data:raise ValueError('Resume requires retained data')
        previous=json.loads((out/'evaluation.json').read_text())
        if previous['manifest_sha256'] != report['manifest_sha256']:
            raise ValueError('Resume requires identical manifest')
        parser_files=('cad_agent/service.py','cad_agent/cad/reader.py','cad_agent/cad/regions.py','cad_agent/contracts.py')
        if previous['code_hashes'] != code:
            if previous['observations'] or any(previous['code_hashes'].get(k)!=code.get(k) for k in parser_files):
                raise ValueError('Changed code can resume import-only run only when parser/contracts/service are unchanged')
            report['prior_import_code_hashes']=previous['code_hashes']
            report['parser_unchanged_across_freeze']=True
        for entry in previous['imports']:
            entry.setdefault('code_sha256',sha256(json.dumps(previous['code_hashes'],sort_keys=True).encode()).hexdigest())
        rows.extend(previous['observations']);imports.extend(previous['imports'])
        report['resume_history']=previous.get('resume_history',[])+[dict(previous_runner_sha256=previous['runner_sha256'],repeats=repeats,reason='resume retains all completed observations and original HTTP import timings')]
    if provenance_report:
        origin=json.loads(Path(provenance_report).read_text())
        report['prior_revision']=dict(path=str(provenance_report),imports=origin['imports'],summary=origin['summary'],observations=origin['observations'])
        interruption=Path(provenance_report).parent/'revision1-control-interruption.json'
        if interruption.exists():report['prior_revision']['control_interruption']=json.loads(interruption.read_text())
    def save():
        report['summary']=summarize(rows)
        prior=report.get('prior_revision',{}).get('observations',[])
        report['all_revision_attempt_audit']=dict(completed_attempts=len(rows)+len(prior),
            completed_failures=sum(not r['contract_pass'] for r in [*prior,*rows]),
            supervisor_interruptions=int('control_interruption' in report.get('prior_revision',{})),
            note='Old request-size failure is retained; explicit supervisor interruption is not a product geometry failure. Reference object counts are not multiplied by repetitions/revisions.')
        report['groups']={f'{b}/{p}':summarize([r for r in rows if r['backend']==b and r['phase']==p]) for b in backends for p in phases}
        (out/'evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for backend in backends:
        retained=out/'data'/backend
        if retain_data:
            if retained.exists() and any(retained.iterdir()) and not resume:
                raise ValueError('Retained workspace must be empty: '+str(retained))
            retained.mkdir(parents=True,exist_ok=True)
        context=nullcontext(str(retained)) if retain_data else tempfile.TemporaryDirectory(prefix='cad-real-e2e-')
        with context as temporary:
            settings=OfflineSettings(data_dir=Path(temporary),max_upload_bytes=256*1024*1024,embedding_model_dir=Path(temporary)/'unused-models')
            app=create_app(settings);workspace=app.state.workspace
            workspace.geometry_executor.close()
            workspace.geometry_executor=MeasuredLocalExecutor() if backend=='local' else MeasuredSpawnExecutor()
            drawings={}
            if reuse_import_root:
                reuse_imports(Path(reuse_import_root),workspace.store,cases)
            if reuse_import_backend and backend!=reuse_import_backend:
                reuse_imports(out/'data'/reuse_import_backend,workspace.store,cases)
            with TestClient(app) as client:
                for case in cases:
                    prior=next((i for i in imports if i['backend']==backend and i['case']==case['case'] and i['success']),None)
                    if resume and prior:
                        drawing=workspace.store.get('drawing',prior['snapshot_id'])
                        source=workspace.store.artifact(drawing['id'],'source.dxf')
                        if sha256(source.read_bytes()).hexdigest()!=case['sha256']:
                            raise ValueError('Retained source hash mismatch')
                        drawings[case['case']]=drawing
                        print('resume_import',backend,case['case'],'verified original source hash',flush=True)
                        continue
                    tick=time.perf_counter();entry=dict(backend=backend,case=case['case'],source=case['source'],success=False)
                    try:
                        path=ROOT/case['source'];content=path.read_bytes();digest=sha256(content).hexdigest()
                        if digest!=case['sha256']:raise ValueError('Source hash mismatch')
                        entry.update(source_sha256=digest,source_bytes=len(content))
                        response=client.post('/api/drawings',files={'file':(path.name,content,'application/octet-stream')})
                        del content
                        response.raise_for_status();drawings[case['case']]=response.json()
                        entry.update(success=True,snapshot_id=response.json()['id'],unit=response.json()['unit'],retained_workspace=str(Path(temporary).resolve()) if retain_data else None)
                    except Exception as exc:entry.update(error=type(exc).__name__,message=str(exc))
                    entry['seconds']=time.perf_counter()-tick
                    entry['import_mode']='reuse_this_run_import' if reuse_import_root or (reuse_import_backend and backend!=reuse_import_backend) else 'fresh_http_dxf_import'
                    if entry['import_mode']=='reuse_this_run_import':entry['source_backend']=reuse_import_backend;entry['reuse_source_root']=str(reuse_import_root) if reuse_import_root else None
                    imports.append(entry);save()
                    print('import',backend,case['case'],entry['success'],round(entry['seconds'],3),flush=True)
                for phase in phases:
                    sequence=[(r,c) for c in cases for r in range(repeats)] if phase=='warm' else [(r,c) for r in range(repeats) for c in cases]
                    for repeat,case in sequence:
                        if any(r['backend']==backend and r['phase']==phase and r['repeat']==repeat and r['case']==case['case'] for r in rows):
                            continue
                        if case['case'] not in drawings:
                            rows.append(dict(case=case['case'],backend=backend,phase=phase,repeat=repeat,complete=False,contract_pass=False,seconds=0,error='source_import_failed'))
                        else:
                            clean_derived(workspace,phase=='cold')
                            if backend=='spawn' and phase=='cold':
                                workspace.geometry_executor.close();workspace.geometry_executor=MeasuredSpawnExecutor()
                            rows.append(attempt(client,workspace,drawings[case['case']],case,backend,phase,repeat,out,pagination_limits,policies,require_full_objects))
                        save();print('case',backend,phase,case['case'],rows[-1]['contract_pass'],round(rows[-1]['seconds'],3),flush=True)
    pairs=[]
    for phase in phases:
        for repeat in range(repeats):
            for case in cases:
                pair={r['backend']:r for r in rows if r['case']==case['case'] and r['phase']==phase and r['repeat']==repeat}
                if set(pair)=={'local','spawn'}:
                    pairs.append(dict(case=case['case'],phase=phase,repeat=repeat,
                        comparable=all(r.get('graph_hash') for r in pair.values()),
                        identical=bool(pair['local'].get('graph_hash') and pair['local'].get('graph_hash')==pair['spawn'].get('graph_hash'))))
    report['backend_comparison']=pairs
    if baseline_report:
        baseline_path=Path(baseline_report);baseline=json.loads(baseline_path.read_text());comparisons=[]
        for row in rows:
            old=next((r for r in baseline['observations'] if r['case']==row['case'] and r['backend']==row['backend'] and r['phase']=='cold'),None)
            item=dict(case=row['case'],backend=row['backend'],comparable=False,identical=False)
            if old and old.get('graph_file') and row.get('graph_file'):
                old_graph=json.loads((baseline_path.parent/old['graph_file']).read_text())
                new_graph=json.loads((out/row['graph_file']).read_text())
                item.update(comparable=True,identical=digest_result(old_graph)==digest_result(new_graph),
                    differing_top_level_keys=[k for k in sorted(set(old_graph)|set(new_graph)) if normalize(old_graph.get(k))!=normalize(new_graph.get(k))],
                    before_http_geometry_seconds=old.get('stages',{}).get('http_geometry_seconds'),
                    after_http_geometry_seconds=row.get('stages',{}).get('http_geometry_seconds'),
                    before_graph_sha256=sha256((baseline_path.parent/old['graph_file']).read_bytes()).hexdigest())
            comparisons.append(item)
        report['baseline_comparison']=dict(path=str(baseline_path.resolve()),sha256=sha256(baseline_path.read_bytes()).hexdigest(),observations=comparisons)

    # Repetitions are correlated: report each fixed reference object only once
    # per backend, never inflate the sample count with repeated phase runs.
    reference={}
    for backend in backends:
        selected=[]
        for case in cases:
            available=[r for r in rows if r['backend']==backend and r['case']==case['case'] and r.get('annotations')]
            selected.append(available[0]['annotations'] if available else score_annotations([],case.get('annotation_samples',[])))
        correct=sum(s['matched_identity'] for s in selected)
        annotated=sum(s['annotated_objects'] for s in selected)
        typed=sum(s['typed_predictions_in_annotation_coverage'] for s in selected)
        reference[backend]=dict(unique_reference_objects=annotated,matched_identity=correct,
            typed_predictions_in_annotation_coverage=typed,reference_precision=correct/typed if typed else None,
            reference_recall=correct/annotated if annotated else None,
            unscored_typed_predictions_outside_annotation_coverage=sum(s['unscored_typed_predictions_outside_annotation_coverage'] for s in selected),
            note='First available completed geometry/annotation result per case; cases without completed geometry/annotations count as unmatched. Delivery failures are tracked separately. Frozen agent review, not independent human truth.')
    report['selected_reference_consistency']=reference
    after={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'cad_agent').rglob('*.py'))}
    report['code_unchanged_during_run']=after==code
    save();return report


class NoComputeExecutor:
    def __init__(self):self.events=[]
    def compute(self, *args, **kwargs):
        raise AssertionError('Delivery recovery must never recompute geometry')
    def close(self):
        pass


def recover_delivery(revision, manifest_path=MANIFEST):
    """Replay saved same-attempt evidence with legal pagination; retain cold failures."""
    revision=Path(revision)
    report=json.loads((revision/'evaluation.json').read_text())
    cases={c['case']:c for c in json.loads(Path(manifest_path).read_text())['cases']}
    output=revision/'delivery-recovery';output.mkdir(exist_ok=True)
    results=[]
    for original in report['observations']:
        if not original.get('geometry_complete') or original.get('delivery_complete'):continue
        case=cases[original['case']];graph_path=revision/original['graph_file']
        graph=json.loads(graph_path.read_text())
        row=dict(case=original['case'],backend=original['backend'],original_seconds=original['seconds'],
                 original_graph_file=str(graph_path),original_graph_sha256=sha256(graph_path.read_bytes()).hexdigest(),
                 original_graph_hash=original['graph_hash'],mode='same_attempt_saved_graph_delivery_only',limit=2,success=False)
        started=time.perf_counter()
        with tempfile.TemporaryDirectory(prefix='cad-delivery-recovery-') as temporary:
            root=Path(temporary);app=create_app(OfflineSettings(data_dir=root,embedding_model_dir=root/'unused-models'))
            workspace=app.state.workspace;workspace.geometry_executor.close();workspace.geometry_executor=NoComputeExecutor()
            reuse_imports(revision/'data'/original['backend'],workspace.store,[case])
            workspace.store.put('component',graph['record_id'],graph)
            try:
                with TestClient(app) as client:
                    session=CadAgentSession(workspace,ChatRequest(snapshot_id=graph['snapshot_id'],question='核对这些洞口候选',mode='agent',agent_policy='legacy'))
                    params=dict(layout_id=graph['layout_id'],bbox=case['bbox'],max_opening_m=case.get('max_opening_m',3),unit_override=case.get('unit_override','mm'),view='openings',limit=2)
                    pages=[];items=[];sizes=[]
                    while True:
                        raw=session.invoke_tool('query_building_relations',params|{'offset':len(items)})
                        sizes.append(len(raw));observation=json.loads(raw)
                        if 'error' in observation:raise ValueError(str(observation['error']))
                        if len(raw)>=50000:raise ValueError('Page exceeds 50000-character contract')
                        pages.append(observation);items.extend(observation['items'])
                        if not observation['has_more']:break
                        if not observation['items'] or len(pages)>=12:raise ValueError('Pagination cannot finish within tool budget')
                    assert len(items)==len(graph['openings'])
                    assert {i['object_id'] for i in items}=={i['object_id'] for i in graph['openings']}
                    delivery=session.finish(json.dumps({'claims':[{'text':'这些为选定范围内的洞口候选，身份和房间关系仍需复核。','state':'derived','evidence_basis':'tool_observation','observation_ids':[p['observation_id'] for p in pages[:4]]}]},ensure_ascii=False))
                    response=client.get('/api/tasks/'+delivery['task']['task_id']);response.raise_for_status()
                    label=original['backend']+'-'+original['case']
                    (output/(label+'-pages.json')).write_text(json.dumps(pages,ensure_ascii=False,indent=2))
                    (output/(label+'-delivery.json')).write_text(json.dumps(delivery,ensure_ascii=False,indent=2))
                    row.update(success=True,pages=len(pages),page_characters=sizes,objects=len(items),unknown_objects=sum(i.get('opening_type') not in ('door_candidate','window_candidate') for i in items),task_status=delivery['task']['status'],session_metrics=session.metrics.snapshot(),geometry_recomputed=False)
            except Exception as exc:row.update(error=type(exc).__name__,message=str(exc))
        row['recovery_seconds']=time.perf_counter()-started;results.append(row)
    result=dict(runtime_mode='geometry_pipeline_e2e_delivery_recovery_no_llm',runner_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),observations=results,original_attempts_unchanged=True)
    (output/'evaluation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def replay_pagination(saved_report,manifest_path,output):
    """Run the pagination matrix on exact saved graphs, prohibiting geometry."""
    saved_report=Path(saved_report);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    original=json.loads(saved_report.read_text());cases={c['case']:c for c in json.loads(Path(manifest_path).read_text())['cases']}
    rows=[]
    for old in original['observations']:
        if not old.get('geometry_complete'):continue
        case=cases[old['case']];path=saved_report.parent/old['graph_file'];graph=json.loads(path.read_text())
        with tempfile.TemporaryDirectory(prefix='cad-pagination-matrix-') as temporary:
            root=Path(temporary);app=create_app(OfflineSettings(data_dir=root,embedding_model_dir=root/'unused-models'))
            workspace=app.state.workspace;workspace.geometry_executor.close();workspace.geometry_executor=NoComputeExecutor()
            reuse_imports(saved_report.parent/'data'/old['backend'],workspace.store,[case])
            workspace.store.put('component',graph['record_id'],graph)
            drawing=workspace.store.get('drawing',graph['snapshot_id'])
            params=dict(layout_id=graph['layout_id'],bbox=case['bbox'],max_opening_m=case.get('max_opening_m',3),unit_override=case.get('unit_override','mm'),view='openings')
            with TestClient(app) as client:
                variants=[tool_delivery(client,workspace,drawing,params,graph,limit,output,old['case']+'-'+policy+'-'+limit,policy,False) for policy in ('legacy','efficient') for limit in ('1','default','20')]
            rows.append(dict(case=old['case'],backend=old['backend'],graph_file=str(path.resolve()),graph_sha256=sha256(path.read_bytes()).hexdigest(),pagination_variants=variants))
    variants=[v for row in rows for v in row['pagination_variants']]
    result=dict(runtime_mode='saved_graph_pagination_matrix_no_geometry_no_llm',source_root=str(ROOT),cad_agent_module_file=str(Path(sys.modules['cad_agent'].__file__).resolve()),
        code_hashes={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'cad_agent').rglob('*.py'))},
        runner_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),observations=rows,
        summary=dict(attempts=len(variants),passed=sum(v['contract_pass'] for v in variants),failures=sum(not v['contract_pass'] for v in variants),geometry_calls=sum(v['geometry_calls'] for v in variants)))
    (output/'evaluation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=Path,default=MANIFEST)
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/geometry-isolation-real-e2e-20261019')
    p.add_argument('--pagination-replay-report',type=Path);p.add_argument('--require-full-objects',action='store_true');p.add_argument('--policy',action='append',choices=['legacy','efficient']);p.add_argument('--pagination-limit',action='append',choices=['1','20','default']);p.add_argument('--baseline-report',type=Path);p.add_argument('--delivery-recovery',type=Path);p.add_argument('--reuse-import-root',type=Path);p.add_argument('--provenance-report',type=Path);p.add_argument('--freeze-ready',type=Path);p.add_argument('--reuse-import-backend',choices=['local','spawn']);p.add_argument('--retain-data',action='store_true');p.add_argument('--resume',action='store_true');p.add_argument('--repeats',type=int,default=1);p.add_argument('--backend',action='append',choices=['local','spawn'])
    p.add_argument('--phase',action='append',choices=['cold','warm','alternating']);a=p.parse_args()
    if a.pagination_replay_report:
        result=replay_pagination(a.pagination_replay_report,a.manifest,a.output);print(json.dumps(result['summary']));return int(result['summary']['failures']>0)
    if a.delivery_recovery:
        result=recover_delivery(a.delivery_recovery,a.manifest);print(json.dumps(result));return int(any(not r['success'] for r in result['observations']))
    if a.repeats<1:p.error('repeats must be positive')
    if a.freeze_ready:
        if not a.freeze_ready.exists():
            p.error('Freeze marker absent: do not start this process before the final code freeze')
        frozen=json.loads(a.freeze_ready.read_text())
        actual={str(path.relative_to(ROOT)):sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT/'cad_agent').rglob('*.py'))}
        if frozen.get('code_hashes') != actual:p.error('Freeze marker does not match current production code')
    result=evaluate(a.manifest,a.output,a.repeats,tuple(a.backend or ['local','spawn']),tuple(a.phase or ['cold','warm','alternating']),a.retain_data,a.resume,a.reuse_import_backend,a.reuse_import_root,a.provenance_report,tuple(a.pagination_limit or ['1']),a.baseline_report,tuple(a.policy or ['legacy']),a.require_full_objects)
    print(json.dumps(result['summary']));return int(result['summary']['failures']>0)


if __name__=='__main__':raise SystemExit(main())
