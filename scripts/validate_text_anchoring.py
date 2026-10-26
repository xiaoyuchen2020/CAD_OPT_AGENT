"""Reproducible real-DXF verification in an isolated workspace.

Default is local only. Review/agent explicitly use the existing subscription
account and its shared queue, never copy credentials or enable paid fallback.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_source_record_workflow import seed
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.source_record_workflow import try_run
from cad_agent.analysis.model_context import encode_messages, codec
from cad_agent.analysis.delivery_context import bounded_packet
from cad_agent.analysis.context_policy import reference_dependencies
from cad_agent.evidence import verify_source_evidence
from cad_agent.analysis.run_journal import public_value


def save(out, name, value):
    (out/name).write_text(json.dumps(public_value(value), ensure_ascii=False, indent=2)+'\n')


def source_audit(session):
    from cad_agent.analysis.general import DrawingIndex
    indexes = {}
    for evidence in session.evidence.values():
        sid = evidence['snapshot_id']
        if sid not in indexes: indexes[sid] = DrawingIndex(session.w, sid)
        idx = indexes[sid]
        verify_source_evidence(evidence, snapshot_id=sid, region=idx.by_id.get(evidence['region_id']),
                               drawing_unit=idx.drawing['unit'])
    # Check a bounded model packet, including source/observation dependency closure.
    packet = dict(evidence=list(session.evidence.values()), observations=list(session.observations.values()),
                  question=session.request.question)
    packet = bounded_packet(packet, 24000, session.task.data['subtasks'], include_templates=False)
    messages = encode_messages(session, [{'role':'user','content':json.dumps(packet,ensure_ascii=False)}],
                               restore_read_context=False)
    decoded = codec(session).decode(json.loads(messages[0]['content']))
    shown = {e['evidence_id'] for e in packet['evidence']}
    observed = {o['observation_id'] for o in packet['observations']}
    for o in packet['observations']:
        erefs, orefs = reference_dependencies(o)
        assert erefs <= shown and orefs <= observed
    for group in decoded.get('source_groups', []):
        for line in group['text'].splitlines():
            alias, quote = line.split('] ', 1)
            eid = codec(session).reverse[alias[1:].split()[0]]
            assert json.loads(quote) == session.evidence[eid]['quote']
    return dict(anchors_verified=len(session.evidence), bounded_sources=len(shown),
                bounded_observations=len(observed), omissions=packet.get('context_omissions', {}),
                encoded_chars=sum(len(m.get('content','')) for m in messages))


def table_run(session, kind, reference):
    tool='extract_'+kind
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.analysis.business_tables import extract_planning_metrics, extract_building_schedule
    from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest, BuildingScheduleRequest
    method,schema=(extract_planning_metrics,PlanningMetricsRequest) if kind=='planning_metrics' else (extract_building_schedule,BuildingScheduleRequest)
    index=DrawingIndex(session.w,session.request.snapshot_id)
    def read(args):
        result=method(session.w,index.snapshot,schema(**args),index)
        session._register_tree(result)
        session.register_observation(tool,result)
        return result
    discovery=read({})
    if discovery.get('error'): raise ValueError(discovery['error'])
    pages=[]
    for candidate in discovery['candidates']:
        args=dict(table_id=candidate['table_id'],limit=2)
        while True:
            page=read(args)
            if page.get('error'): raise ValueError(page['error'])
            pages.append(page)
            if not page.get('has_more'): break
            args=page['next_actions'][0]['arguments']
    fields=[f for p in pages for f in p.get('fields',[])]
    rows=[r for p in pages for r in p.get('records',[])]
    if kind=='planning_metrics':
        expected=reference['fields']
        for f in expected:
            assert any(v['field']==f['field'] and v['value']==f['value'] and v['state']=='observed' for v in fields),f['field']
        for label,value in reference['parking'].items():
            assert any(f['field']=='parking_spaces' and f['value']==value and label in str(f) for f in fields),label
        checks=dict(expected_categories=len(expected),parking_categories=len(reference['parking']))
    else:
        assert len(rows)==len(reference['rows'])
        for expected in reference['rows']:
            values=expected['values']
            matches=[r for r in rows if any(f['field']=='name' and f['value']==values['name'] for f in r['fields'])]
            assert len(matches)==1,values['name']
            actual={f['field']:f for f in matches[0]['fields']}
            for key,value in values.items():
                # Positive F storeys have an explicit normalized count; the
                # basement expression remains non-scalar in the product.
                normalized=value[:-1] if key=='storeys' and value and value[:-1].isdigit() and value.endswith('F') else value
                assert actual.get(key,{}).get('value')==normalized,(values['name'],key)
        checks=dict(expected_rows=len(reference['rows']),expected_cells=reference['explicit_target_cells'])
    assert pages
    return dict(pages=pages,acceptance=checks)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source-data',type=Path,required=True)
    ap.add_argument('--snapshot',required=True)
    ap.add_argument('--question',required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--mode',choices=['auto','review','agent','planning_metrics','building_schedule'],default='auto')
    ap.add_argument('--reference',type=Path)
    ap.add_argument('--account-data',type=Path)
    ap.add_argument('--reimport',action='store_true')
    args=ap.parse_args()
    live=args.mode in ('review','agent')
    if live and not args.account_data: ap.error('Live mode requires --account-data')
    if args.mode in ('planning_metrics','building_schedule') and not args.reference: ap.error('Table mode requires an independent --reference')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    save(out,'code-manifest.json',{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ('cad_agent','scripts') for p in (ROOT/folder).rglob('*.py')})
    w=Workspace(Settings(data_dir=out/'data',provider='responses_subscription'))
    input_record=seed(args.source_data,w,args.snapshot)
    sid=args.snapshot
    if args.reimport:
        path=w.store.artifact(sid,'source.dxf')
        sid=w.ingest('real-anchoring-validation.dxf',path.read_bytes())['id']
    save(out,'input.json',dict(input_record,active_snapshot=sid,reimported=args.reimport))
    if live:
        from cad_agent.adapters.siwc import SiwcAccount
        w.siwc_account.close();w.siwc_account=SiwcAccount(args.account_data)
        status=w.siwc_account.status()
        save(out,'account-policy.json',{k:status[k] for k in ('connected','active_runs','max_concurrency','configured_concurrency')})
        if not status['connected']: raise ValueError('Subscription account is not connected')
        # Runtime queued_lease enforces the existing account capacity.
    request=ChatRequest(snapshot_id=sid,question=args.question,mode='agent',runtime='responses_subscription',
        model_profile='siwc_sol',source_record_mode=args.mode if args.mode in ('auto','review','agent') else 'agent',
        allow_fallback=False,source_read_batching=False)
    save(out,'request.json',request.model_dump())
    s=CadAgentSession(w,request);s.journal.bind('text_anchoring_validation')
    started=time.monotonic();result=None;error=None;audit=None
    try:
        if args.mode in ('planning_metrics','building_schedule'):
            result=table_run(s,args.mode,json.loads(args.reference.read_text()))
        elif live:
            from cad_agent.runtimes.registry import run_agent
            result=run_agent(s)
        else:
            result=try_run(s)
        if result is None: raise ValueError('Source workflow did not produce a result')
        save(out,'result.json',result)
        audit=source_audit(s);save(out,'source-audit.json',audit)
    except Exception as exc:
        error=dict(type=type(exc).__name__,code=getattr(exc,'code',None),message=str(exc))
        save(out,'error.json',error)
    finally:
        for name,value in [('task',s.task.data),('trace',s.trace),('evidence',s.evidence),
            ('observations',s.observations),('usage',s.usage),('events',s.journal.snapshot()),
            ('runtime-diagnostics',getattr(s,'runtime_diagnostics',{}))]: save(out,name+'.json',value)
        summary=dict(passed=result is not None and error is None,mode=args.mode,live_model=live,
            elapsed_seconds=round(time.monotonic()-started,2),error=error,source_audit=audit,
            model_requests=len(s.usage),input_tokens=sum(u.get('prompt_tokens',0) for u in s.usage),
            output_tokens=sum(u.get('completion_tokens',0) for u in s.usage),
            task_status=s.task.data.get('status'),tool_calls=s.calls,
            limits='Source/representation validation and explicit sample checks; not whole-project engineering or universal semantic acceptance.')
        save(out,'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
        w.close()
    return 0 if summary['passed'] else 1

if __name__=='__main__':sys.exit(main())
