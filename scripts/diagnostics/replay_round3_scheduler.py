"""Offline dispatch replay from frozen Q11/Q14 trajectories; no model/tool execution."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from cad_agent.analysis.focus import TaskFocus, ToolsetRequest
from cad_agent.analysis.scheduling import schedule, execution_state
from cad_agent.analysis.tasks import TaskLedger
from cad_agent.analysis.tool_metadata import CONTROL

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'artifacts/designer-facts-unlimited-final-20261020/evaluated-results.json'
OUT=ROOT/'artifacts/bottleneck-round3-20261020/scheduler-report.json'


def restore(row):
    # The trace retains tool owners and observed artifacts. Only publication
    # statuses are reset: this is a routing-state replay, not a source rerun.
    result=row['result'];data=deepcopy(result['task'])
    data['batches']={b['batch_id']:deepcopy(b) for b in result.get('room_batches',[])}
    data['observations']={o['observation_id']:deepcopy(o) for o in result.get('tool_observations',[])}
    data['evidence']={}
    for step in data['subtasks']:
        step.update(status='pending',delivered=False,claims=[],calls=step['calls'],reason=None)
        for cell in step.get('coverage',[]):
            if cell['state'] in ('addressed','unknown'):cell['state']='unread'
    ledger=TaskLedger.__new__(TaskLedger);ledger.data=data;ledger.save=lambda:None
    s=SimpleNamespace(w=SimpleNamespace(settings=SimpleNamespace(provider='responses_subscription')),task=ledger,room_batches=data['batches'],observations=data['observations'],evidence={},
        coverage=SimpleNamespace(pending=lambda:[]),geometry_reviews={},boundary_reviews={},reviewed_image_ids=set(),
        request=SimpleNamespace(question=row['question'],parent_run_id=None,source_read_batching=False,related_drawings=[],runtime='responses_subscription'))
    s.focus=TaskFocus(s)
    # Replay successful explicit expansion receipts only; failed attempts did
    # not mutate the original allowance. Repeated controls after that are input.
    for t in result['trace']:
        if t['tool']=='request_toolset' and t['status']=='completed':
            a=t['arguments'];ids=t.get('task_ids',[]) or ['q1']
            for i in ids:
                s.focus.expansions[i]=s.focus.expansions.get(i,0)+1
                if a.get('tool_name'):s.focus.explicit_tools.setdefault(i,set()).add(a['tool_name'])
                else:s.focus.expanded.setdefault(i,set()).add(a['family'])
    return s


def main():
    rows=json.loads(SOURCE.read_text());replays=[]
    for rid in ('Q11-B-sol-high','Q14-B-sol-high'):
        row=next(r for r in rows if r['id']==rid);s=restore(row)
        offered=s.focus.names();work=schedule(s)
        invariant=all(set(item['next_action']['tools'])<=offered and
                      all(execution_state(s.task.data,i)['executable'] for i in item['next_action']['task_ids'])
                      for item in work if item['executable'])
        assert invariant
        errors=[{'tool':t['tool'],'message':t.get('error',{}).get('message','')} for t in row['result']['trace'] if t['status']=='failed']
        failed_controls=[t for t in row['result']['trace'] if t['tool']=='request_toolset' and t['status']=='failed']
        responses=[]
        for t in failed_controls:
            try:responses.append(s.focus.expand(ToolsetRequest.model_validate(t['arguments']),t.get('task_ids',[]) or ['q1']))
            except ValueError as error:responses.append({'error':str(error)})
        replays.append({'case_id':rid,'source_run_id':row['run_id'],'source_task_id':row['task_id'],
            'historical_failed_tools':errors,'frozen_ledger_call_counts':{t['id']:t['calls'] for t in s.task.data['subtasks']},
            'execution_states':{t['id']:execution_state(s.task.data,t['id']) for t in s.task.data['subtasks']},
            'schedule':work,'offered_tools':sorted(offered),'replayed_control_responses':responses,
            'required_pending_has_selectable_owner_and_offered_tool':invariant,
            'executable_pending':sum(w['executable'] for w in work),'blocked_pending':sum(not w['executable'] for w in work)})
    report={'kind':'offline_scheduler_routing_replay','source':str(SOURCE.relative_to(ROOT)),
        'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'model_calls':0,'cad_tool_calls':0,
        'reconstruction':'Frozen task/observation/trace payloads; publication states reset, persisted per-owner call counts preserved (failed multi-owner validation may charge no owners). No new source facts or final answers generated.',
        'unchanged_budgets':{'subtask_calls':12,'repeated_no_progress':2,'optional_expansions':2},
        'bounded_replanning':{'max_revisions':2,'resets_call_budget':False,'grants_receipts':False},
        'improvements':['Required pending tools directly exposed independently of optional expansion cap','Budget/dependency blocks classified separately from absent capabilities','Registered plan_task reopening accepted; bounded revisions preserve source receipts and call counts'],
        'unresolved':['Q11 upstream geometry still exhausted at 12 calls; dependent numerical binding remains unavailable and must be partial','Q14 real object identities, geometry, relations and applied units still require execution; no business or answer-quality acceptance inferred'],
        'replays':replays}
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'report':str(OUT),'invariants_passed':len(replays),'model_calls':0},ensure_ascii=False))

if __name__=='__main__':main()
