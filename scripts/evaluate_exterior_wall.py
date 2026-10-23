"""Explicit subscription-only comparison on the frozen exterior-wall source.

Use --code-root to execute an archived implementation without changing checkout.
Credentials stay in their existing account directory, never copied or exported.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()
                if k not in {'execution_token', 'access_token', 'refresh_token', 'id_token', 'api_key'}}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument('--source-data', type=Path, required=True)
    ap.add_argument('--account-data', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--repetitions', type=int, default=3)
    ap.add_argument('--reindex', action='store_true')
    args = ap.parse_args()
    if not args.live:
        ap.error('--live is required to spend subscription usage')
    if not 1 <= args.repetitions <= 3:
        ap.error('repetitions must be 1..3')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.code_root.resolve()))
    from fastapi.testclient import TestClient
    from cad_agent.app import create_app
    from cad_agent.config import Settings
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.artifact_storage import copy_artifact
    sid = 'snap_4c04439de885a70159a0b17d'
    with sqlite3.connect(f'file:{args.source_data}/index.sqlite3?mode=ro', uri=True) as conn:
        drawing = json.loads(conn.execute('SELECT data FROM records WHERE kind=? AND id=?', ('drawing', sid)).fetchone()[0])
    source = args.source_data / 'drawings' / sid
    digest = hashlib.sha256((source / 'source.dxf').read_bytes()).hexdigest()
    assert digest == drawing['sha256']
    hashes = {str(p.relative_to(args.code_root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((args.code_root / 'cad_agent').rglob('*.py'))}
    (out / 'manifest.json').write_text(json.dumps(dict(source_sha256=digest, question='帮我找下外墙做法',
        source_snapshot_id=sid, model='gpt-6.1-sol', effort='low', runtime='responses_subscription',
        allow_fallback=False, source_read_batching=False, reindex=args.reindex, code=hashes), ensure_ascii=False, indent=2))
    app = create_app(Settings(data_dir=out / 'data', provider='responses_subscription'))
    w = app.state.workspace
    w.siwc_account.close()
    w.siwc_account = SiwcAccount(args.account_data)
    assert w.siwc_account.status()['connected'], 'Subscription not connected; no fallback.'
    w.store.put('drawing', sid, drawing)
    if drawing.get('latest_fact_set_id'):
        fid = drawing['latest_fact_set_id']
        with sqlite3.connect(f'file:{args.source_data}/index.sqlite3?mode=ro', uri=True) as conn:
            record = conn.execute('SELECT kind,data FROM records WHERE id=?', (fid,)).fetchone()
        if record:
            w.store.put(record[0], fid, json.loads(record[1]))
    for name in ('source.dxf', 'source.dwg', 'oda.json', 'scene.json', 'regions.json', 'entities.json', 'render.json'):
        if (source / name).is_file():
            copy_artifact(source / name, w.store.artifact(sid, name))
    # New parser receives identical DXF bytes in a new snapshot; never mutates old spans.
    if args.reindex:
        drawing = w.reindex(sid)
        sid = drawing['id']
    rows = []
    with TestClient(app) as client:
        for attempt in range(1, args.repetitions + 1):
            started = time.monotonic()
            r = client.post('/api/chat-runs', json=dict(snapshot_id=sid, question='帮我找下外墙做法',
                mode='agent', runtime='responses_subscription', model_profile='siwc_sol',
                allow_fallback=False, source_read_batching=False, agent_policy='efficient'))
            if r.is_error:
                raise RuntimeError(f'Run creation failed: {r.status_code} {r.text[:500]}')
            run = r.json()
            print('START', attempt, run['id'], flush=True)
            while run['status'] in ('queued', 'running'):
                if time.monotonic() - started > 720:
                    client.post('/api/chat-runs/' + run['id'] + '/cancel').raise_for_status()
                if time.monotonic() - started > 850:
                    raise TimeoutError('Cancellation grace period exceeded')
                time.sleep(1)
                r = client.get('/api/chat-runs/' + run['id']); r.raise_for_status(); run = r.json()
            r = client.get('/api/tasks/' + run['task_id']); r.raise_for_status(); task = r.json()
            result = run.get('result') or {}
            usage = run.get('usage', [])
            row = dict(attempt=attempt, run_id=run['id'], status=run['status'], task_status=task.get('status'),
                seconds=round(time.monotonic() - started, 3), input_tokens=sum(u.get('prompt_tokens', 0) for u in usage),
                output_tokens=sum(u.get('completion_tokens', 0) for u in usage), model_requests=len(usage),
                cached_tokens=sum(u.get('prompt_cache_hit_tokens', 0) for u in usage),
                answer=result.get('answer'), trace=[t.get('tool') for t in run.get('trace', [])], error=run.get('error'))
            for name, value in [('run', run), ('task', task)]:
                (out / f'{attempt}-{name}.json').write_text(json.dumps(clean(value), ensure_ascii=False, indent=2))
            rows.append(row)
            (out / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print('DONE', attempt, row['status'], row['model_requests'], row['input_tokens'], flush=True)
            if run['status'] != 'completed':
                break


if __name__ == '__main__':
    main()
