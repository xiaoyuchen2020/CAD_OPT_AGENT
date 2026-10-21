#!/usr/bin/env python3
"""Frozen, bounded SIWC batch execution. Never automatically replays an attempt.

Prepare: .venv/bin/python scripts/run_controlled_batch.py --plan PLAN --output NEW_DIR --prepare-only
Run: add --account-data SHARED_ACCOUNT_DATA --concurrency 2 --configure-account-capacity
Resume: repeat without --configure-account-capacity; unfinished attempts are retained.
Explicit retry: --retry-case Q01-A (creates a new attempt; preserves all old evidence).
"""
from pathlib import Path
import argparse
from dataclasses import replace
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
# Supports both direct invocation and the self-contained frozen runner.
sys.path.insert(0, str(ROOT))
from cad_agent.artifact_storage import reference_file, file_digest
STOP = False
PROFILES = {
    'siwc_sol': ('gpt-6.1-sol', 'low'),
    'siwc_sol_high': ('gpt-6.1-sol', 'high'),
    'siwc_luna_high': ('gpt-6-luna', 'high'),
}


def infrastructure_failure_streak(previous, entry):
    """Stop dispatching after three consecutive link failures; retain pending cases."""
    code = (entry.get('error') or {}).get('code')
    return previous + 1 if code in {'SIWC_TRANSPORT_ERROR', 'SIWC_HTTP_ERROR',
        'SIWC_STREAM_INCOMPLETE', 'SIWC_STREAM_FORMAT', 'SIWC_RESPONSE_FAILED'} else 0


def case_configuration(plan, case):
    profile = case.get('model_profile', plan.get('model_profile'))
    if profile not in PROFILES:
        raise ValueError('Unknown subscription model profile')
    model, effort = PROFILES[profile]
    if case.get('model', plan.get('model')) != model or case.get('reasoning_effort', plan.get('reasoning_effort', 'low')) != effort:
        raise ValueError('Requested model/effort differs from the allowlisted profile')
    batching = case.get('source_read_batching', plan.get('source_read_batching', False))
    if type(batching) is not bool:
        raise ValueError('source_read_batching must be boolean')
    return dict(model_profile=profile, model=model, reasoning_effort=effort, source_read_batching=batching)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(folder):
    return {str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != '.DS_Store'}


def read_records(data, kind):
    database = data / 'index.sqlite3'
    if not database.exists(): return []
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        return [json.loads(r[0]) for r in connection.execute('SELECT data FROM records WHERE kind=?', (kind,))]


def prepare(args):
    out = args.output
    if (out / 'manifest.json').exists():
        manifest = json.loads((out / 'manifest.json').read_text())
        if digest(args.plan) != manifest['plan_sha256'] or digest(out / 'plan.json') != manifest['plan_sha256']:
            raise ValueError('Plan differs from frozen plan; use a new output directory.')
        if hashes(out / 'frozen-code') != manifest['code']:
            raise ValueError('Frozen code changed; refusing resume.')
        for item in manifest['drawings'].values():
            for name, expected in item['files'].items():
                if digest(out / 'inputs' / item['snapshot_id'] / name) != expected:
                    raise ValueError('Frozen drawing input changed.')
        return manifest
    if any(p.name != 'runner.lock' for p in out.iterdir()):
        raise ValueError('Output directory is nonempty without a manifest; preserve it and use a new directory.')
    plan = json.loads(args.plan.read_text())
    if plan.get('runtime') != 'responses_subscription':
        raise ValueError('This runner accepts direct SIWC plans only.')
    ids = [c['id'] for c in plan['cases']]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', i) for i in ids):
        raise ValueError('Case ids must be unique safe path components.')
    for case in plan['cases']:
        case_configuration(plan, case)
        caps = case['caps']
        for field in ('seconds', 'tool_calls', 'rounds', 'context_chars', 'exploration_soft_tokens'):
            if not isinstance(caps[field], (int, float)) or caps[field] <= 0: raise ValueError('Invalid fixed budget')
        if case['id'] not in plan['fixed_total_soft_tokens']: raise ValueError('Missing fixed total token budget')
    frozen = out / 'frozen-code'
    shutil.copytree(args.source_root / 'cad_agent', frozen / 'cad_agent', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    shutil.copy2(__file__, frozen / 'runner.py')
    budget = args.plan.parent / 'budget_control.py'
    shutil.copy2(budget, frozen / 'budget_control.py')
    shutil.copy2(args.plan, out / 'plan.json')
    drawings = {}
    for key, (folder, sid) in plan['sources'].items():
        source = args.source_root / 'artifacts' / folder / 'data'
        records = [r for r in read_records(source, 'drawing') if r['id'] == sid]
        if len(records) != 1: raise ValueError(f'Missing drawing {sid}')
        drawing = records[0]; drawing['latest_fact_set_id'] = None
        dest = out / 'inputs' / sid; dest.mkdir(parents=True, exist_ok=True)
        dump(dest / 'drawing.json', drawing)
        references = {}
        for name in ('source.dxf', 'scene.json', 'regions.json', 'entities.json', 'render.json'):
            path = source / 'drawings' / sid / name
            if path.exists():
                references[name] = reference_file(path, dest / name, args.source_root / 'artifacts')
        drawings[key] = {'snapshot_id': sid, 'name': drawing['name'], 'files': hashes(dest),
                         'references': references}
    manifest = {'plan_sha256': digest(args.plan), 'source_root': str(args.source_root),
                'code': hashes(frozen), 'drawings': drawings, 'created_at': time.time()}
    dump(out / 'manifest.json', manifest)
    return manifest


def collect_attempt(path, state, manifest, elapsed=None):
    runs = read_records(path / 'data', 'chat_run')
    run = next((r for r in runs if r['id'] == state.get('run_id')), None)
    if run is None and len(runs) == 1: run = runs[0]
    if run is None: return None
    if run['status'] in ('queued', 'running'): return None
    task = next((t for t in read_records(path / 'data', 'agent_task') if t['task_id'] == run['task_id']), {})
    dump(path / 'run.json', run); dump(path / 'task.json', task)
    case = state['case']; result = run.get('result') or {}
    queue = run.get('subscription_queue_seconds', 0) + run.get('queue_wait_seconds', 0)
    entry = dict(id=case['id'], attempt=state['attempt'], case=case, run_id=run['id'], task_id=run['task_id'],
                 status=run['status'], error=run.get('error'), result=result, answer=result.get('answer'),
                 tokens=sum(u.get('total_tokens', 0) for u in run.get('usage', [])),
                 queue_seconds=round(state.get('dispatch_queue_seconds', 0)+queue, 3),
                 execution_seconds=round(max(0, elapsed-queue), 3) if elapsed is not None else None,
                 elapsed_seconds=round(elapsed, 3) if elapsed is not None else None,
                 concurrency=state['concurrency'], timings_recovered=elapsed is None,
                 evidence_path=str(path))
    dump(path / 'result.json', entry)
    dump(path / 'state.json', state | {'state': 'finished', 'run_id': run['id'], 'finished_at': time.time()})
    return entry


def restore(out, manifest):
    """Read receipts only; never instantiate/replay an interrupted Workspace."""
    results, attempted = [], set()
    for path in sorted((out / 'attempts').glob('*/*')) if (out / 'attempts').exists() else []:
        state_path = path / 'state.json'
        if not state_path.exists(): raise ValueError(f'Incomplete attempt reservation: {path}')
        state = json.loads(state_path.read_text()); attempted.add(state['case']['id'])
        result_path = path / 'result.json'
        if result_path.exists(): results.append(json.loads(result_path.read_text())); continue
        result = collect_attempt(path, state, manifest)
        if result is not None: results.append(result)
        else:
            # Preserve original state and DB. A submitted request may have consumed
            # usage even when no run-id receipt or terminal SSE was persisted.
            dump(path / 'interrupted.json', {'status': 'interrupted_unknown_outcome',
                 'automatic_replay': False, 'checked_at': time.time(), 'case_id': state['case']['id']})
    return results, attempted


def execute(args, manifest):
    global STOP
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.contracts import ChatRequest
    out = args.output; plan = json.loads((out / 'plan.json').read_text())
    spec = importlib.util.spec_from_file_location('frozen_budget_control', out / 'frozen-code/budget_control.py')
    budget = importlib.util.module_from_spec(spec); spec.loader.exec_module(budget); budget.install(plan)
    account = SiwcAccount(args.account_data, args.concurrency)
    if args.configure_account_capacity: account.configure_concurrency(args.concurrency)
    status = account.status()
    if status['max_concurrency'] != args.concurrency: raise ValueError('Account capacity mismatch; explicitly configure it when idle.')
    if not status['connected']: raise ValueError('Shared subscription account is not connected.')
    results, attempted = restore(out, manifest)
    dump(out / "results.json", sorted(results, key=lambda r:(r["id"], r["attempt"])))
    retry = set(args.retry_case)
    known = {c['id'] for c in plan['cases']}
    if retry - known: raise ValueError('Unknown retry case id')
    # Explicit retries still must not overlap a potentially alive previous attempt.
    for cid in retry:
        for path in (out / 'attempts' / cid).glob('*'):
            for run in read_records(path / 'data', 'chat_run'):
                if run.get('execution_token') and run.get('execution_lease_until', 0) > time.time():
                    raise ValueError(f'Previous attempt may still be running: {cid}')
    pending = [c for c in plan['cases'] if c['id'] not in attempted or c['id'] in retry]
    active = {}; submitted_at = time.monotonic()
    link_failures = 0
    stop_reason = None
    settings_path = out / ('resume-settings-'+str(time.time_ns())+'.json' if (out / 'execution-settings.json').exists() else 'execution-settings.json')
    dump(settings_path, dict(runtime=plan['runtime'], model=plan['model'], fallback=False,
         case_configurations={c['id']:case_configuration(plan,c) for c in plan['cases']},
         concurrency=args.concurrency, queue_timeout_seconds=args.queue_timeout,
         budgets='unchanged frozen per-case caps and fixed_total_soft_tokens',
         account_data=str(args.account_data), frozen_code=str(out / 'frozen-code')))
    settings = replace(Settings.load(), siwc_max_concurrency=args.concurrency, siwc_queue_timeout_seconds=args.queue_timeout,
                       embedding_model_dir=args.source_root / '.data/models/bge-small-zh-v1.5')
    try:
        while pending or active:
            if STOP and not active: break
            while pending and len(active) < args.concurrency and not STOP:
                case = pending.pop(0); cid = case['id']
                existing = list((out / 'attempts' / cid).glob('*'))
                attempt = max([int(p.name) for p in existing] or [0])+1
                path = out / 'attempts' / cid / f'{attempt:04d}'
                path.mkdir(parents=True, exist_ok=False)
                state = dict(case=case, attempt=attempt, state='reserved', concurrency=args.concurrency,
                             dispatch_queue_seconds=time.monotonic()-submitted_at, created_at=time.time())
                dump(path / 'state.json', state)  # Before any possible model side effect.
                w = None
                try:
                    caps = case['caps']
                    w = Workspace(replace(settings, data_dir=path / 'data', agent_timeout_seconds=caps['seconds'],
                                          agent_max_tool_calls=caps['tool_calls'], agent_max_rounds=caps['rounds']))
                    w.siwc_account = SiwcAccount(args.account_data, args.concurrency); w.ab_current_case = case
                    sid = plan['sources'][case['source']][1]
                    drawing = json.loads((out / 'inputs' / sid / 'drawing.json').read_text())
                    w.store.put('drawing', sid, drawing)
                    dest = w.store.artifact_dir(sid)
                    references = {}
                    for name, expected in manifest['drawings'][case['source']]['files'].items():
                        if name != 'drawing.json':
                            source = out / 'inputs' / sid / name
                            if file_digest(source) != expected:
                                raise ValueError('Frozen drawing input changed.')
                            references[name] = reference_file(source, dest / name, args.source_root / 'artifacts')
                    dump(path / 'input-references.json', {
                        'snapshot_id': sid, 'relative_to': str(dest.relative_to(path)),
                        'files': references})
                    started = time.monotonic()
                    selected = case_configuration(plan, case)
                    actual = w.model_catalog[selected['model_profile']]
                    if (actual.model, actual.reasoning_effort) != (selected['model'], selected['reasoning_effort']):
                        raise ValueError('Frozen product model configuration differs from test plan')
                    run = w.start_chat(ChatRequest(snapshot_id=sid, question=case['question'], mode='agent',
                         runtime=plan['runtime'], model_profile=selected['model_profile'], allow_fallback=False,
                         source_read_batching=selected['source_read_batching']))
                    state.update(state='started', run_id=run['id']); dump(path / 'state.json', state)
                    active[run['id']] = (w, path, state, started)
                    print('START', cid, attempt, run['id'], flush=True)
                except BaseException:
                    if w is not None: w.close()
                    raise
            for rid, (w, path, state, started) in list(active.items()):
                run = w.store.get('chat_run', rid)
                if run['status'] in ('queued', 'running'): continue
                entry = collect_attempt(path, state, manifest, time.monotonic()-started)
                results.append(entry)
                dump(out / 'results.json', sorted(results, key=lambda r:(r['id'], r['attempt'])))
                w.close(); del active[rid]
                print('DONE', entry['id'], entry['status'], 'tokens', entry['tokens'], flush=True)
                link_failures = infrastructure_failure_streak(link_failures, entry)
                if link_failures >= 3:
                    STOP = True
                    stop_reason = 'consecutive_subscription_link_failures'
                    print('STOP', stop_reason, 'pending', len(pending), flush=True)
                if (entry.get('error') or {}).get('code') in ('SIWC_LOGIN_REQUIRED', 'SIWC_REFRESH_FAILED',
                     'SIWC_AUTH_EXPIRED', 'SIWC_ACCESS_DENIED', 'SIWC_USAGE_LIMIT', 'SIWC_CONFIG_CONFLICT'):
                    STOP = True
            time.sleep(.05)
    finally:
        for rid, (w, path, state, started) in active.items():
            w.cancel_chat(rid); w.close()
        account.close()
        dump(out / 'summary.json', dict(attempts=len(results), pending=len(pending), stopped=STOP,
             stop_reason=stop_reason,
             tokens=sum(r['tokens'] for r in results), real_server_throughput_verified=False,
             frozen_unchanged=hashes(out / 'frozen-code') == manifest['code']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--account-data', type=Path)
    parser.add_argument('--concurrency', type=int, choices=(1, 2), default=1)
    parser.add_argument('--queue-timeout', type=float, default=600)
    parser.add_argument('--configure-account-capacity', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--retry-case', action='append', default=[])
    parser.add_argument('--frozen', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.plan = args.plan.resolve(); args.output = args.output.resolve(); args.source_root = args.source_root.resolve()
    if not math.isfinite(args.queue_timeout) or args.queue_timeout < 0:
        parser.error('--queue-timeout must be finite and nonnegative')
    if not args.prepare_only and args.account_data is None: parser.error('--account-data required to run')
    if args.account_data: args.account_data = args.account_data.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = prepare(args)
        if args.prepare_only:
            print('PREPARED', len(manifest['drawings']), 'drawings; no model requests', flush=True); return
        if not args.frozen:
            command = [sys.executable, str(args.output / 'frozen-code/runner.py'), *sys.argv[1:],
                       '--source-root', str(args.source_root), '--frozen']
            os.execv(sys.executable, command)
        sys.path.insert(0, str(args.output / 'frozen-code'))
        from dotenv import dotenv_values
        for key, value in dotenv_values(args.source_root / '.env').items():
            if value is not None: os.environ.setdefault(key, value)
        execute(args, manifest)


def stop_handler(*_):
    global STOP
    STOP = True
    print('STOP_REQUESTED: finish current attempts, preserve pending cases', flush=True)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, stop_handler); signal.signal(signal.SIGTERM, stop_handler)
    main()
