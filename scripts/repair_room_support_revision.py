"""One explicitly authorized supplement; frozen validator and original native input."""
import argparse, difflib, hashlib, json, shutil, time
from contextlib import contextmanager
from types import SimpleNamespace
from scripts import room_boundary_revision as base
from scripts import check_room_boundary_revision as checks

PRIOR = base.OUT
OUT = base.ROOT / 'artifacts/dynamic-room-support-repair-20261016'
# Configure the existing trusted harness to write only this new experiment root.
# Imported functions resolve base.OUT dynamically; checker output resolves checks.OUT.
base.OUT = OUT
checks.OUT = OUT
read, dump, sha = base.read, base.dump, base.sha

@contextmanager
def wait_for_connection(account):
    deadline = time.monotonic() + 900
    while True:
        lease = account.lease()
        try:
            lease.__enter__()
            break
        except Exception as exc:
            if getattr(exc, 'code', None) != 'SIWC_BUSY' or time.monotonic() >= deadline:
                raise
            time.sleep(20)  # Preflight only; no model request has been sent.
    try:
        yield
    finally:
        lease.__exit__(None, None, None)

def historical():
    rows = []
    for name in ['dynamic-adaptation-20261016', 'dynamic-cross-drawing-20261016',
                 'dynamic-shadow-near-axis-20261016', 'dynamic-multimethod-20261016',
                 'dynamic-nontable-real-20261016', 'dynamic-room-boundary-20261016',
                 'dynamic-room-boundary-revision-20261016']:
        root = base.ROOT / 'artifacts' / name
        manifest = json.loads((root / 'evidence-sha256.json').read_text())
        manifest = manifest.get('files', manifest)
        assert all(sha(root / p) == h for p, h in manifest.items()), name
        rows.append(dict(root=name, files=len(manifest), unchanged=True))
    return rows

def prepare():
    assert not OUT.exists(), 'Never overwrite a previous experiment'
    evidence = historical()
    OUT.mkdir(); (OUT / 'data').mkdir()
    for name in ['input.json', 'source.svg', 'source.png', 'render-provenance.json',
                 'source-audit.json', 'baseline-overlay.svg', 'baseline-overlay.png']:
        shutil.copy2(PRIOR / name, OUT / name)
    for name in ['adaptation-runtime.json', 'table-recovery-runtime.json']:
        shutil.copy2(PRIOR / 'data' / name, OUT / 'data' / name)
    shutil.copy2(PRIOR / 'validator-frozen.py', OUT / 'validator-frozen.py')
    shutil.copy2(PRIOR / 'additional-request-preview.json', OUT / 'approved-request-preview.json')
    dump(OUT / 'authorization.json', dict(answer='允许追加 1 次定向修正',
         max_additional_requests=1, previous_requests=3, previous_usage_tokens=20935,
         total_token_budget=96000, reservation=32000, max_output_tokens=6000,
         scope='Existing code plus endpoint-overlap error only; no new drawing regions or images',
         prior_approval_block_resolved_by_explicit_user_reply=True,
         automatic_adoption=False, automatic_registration=False))
    dump(OUT / 'history-before.json', evidence)
    dump(OUT / 'production-before.json', {str(p.relative_to(base.ROOT)): sha(p)
         for folder in ['cad_agent', 'web'] for p in (base.ROOT / folder).rglob('*')
         if p.is_file() and '__pycache__' not in p.parts})
    w = base.workspace()
    try:
        b = read('input.json'); base.source_check(w, b)
        dump(OUT / 'baseline-before-fresh.json', base.fresh_baseline(w, b))
    finally: w.close()
    print('Prepared isolated supplement; historical files:', sum(r['files'] for r in evidence), flush=True)

def generate():
    from cad_agent.adapters.siwc import SiwcAccount
    from cad_agent.extensions.adaptation.model import check_code
    from cad_agent.extensions.adaptation.contracts import Proposal
    from cad_agent.extensions.adaptation.sandbox import runtime
    from cad_agent.runtimes.responses_subscription import ResponsesTransport
    assert not (OUT / 'request-claimed.json').exists(), 'Single authorized request already claimed; never retry'
    preview = read('approved-request-preview.json')
    assert preview['code'] == (PRIOR / 'candidate-2.py').read_text()
    payload = dict(model='gpt-6.1-sol', instructions=base.SYSTEM,
        input=[dict(role='user', content=json.dumps(dict(instruction=preview['instruction'],
        error='IRRELEVANT_EDGE_SOURCE: six native sources overlap only microscopic endpoint slivers; keep all other code unchanged.',
        code=preview['code']), ensure_ascii=False))], tools=[], tool_choice='none', store=False,
        stream=True, reasoning={'effort': 'low'}, max_output_tokens=6000)
    bound = len(base.encoded(payload)) + 6000 + 512
    assert bound <= 32000 and 20935 + 32000 <= 96000
    w = base.workspace()
    try:
        base.source_check(w, read('input.json')); runtime(OUT / 'data')
        settings = w.model_catalog['siwc_sol']
        assert settings.model == 'gpt-6.1-sol' and settings.provider == 'responses_subscription'
        w.siwc_account.close()
        w.siwc_account = SiwcAccount(base.ROOT / 'artifacts/hybrid-retrieval-20261015/data')
        # A busy connection sends nothing and does not consume the single request.
        with wait_for_connection(w.siwc_account):
            with (OUT / 'request-claimed.json').open('x') as f:
                json.dump(dict(cumulative_call=4, prompt_sha256=base.digest(payload)), f)
            a = dict(number=4, status='requesting', conservative_token_upper_bound=bound)
            state = dict(model='gpt-6.1-sol', provider='responses_subscription', model_calls=1,
                cumulative_model_calls=4, previous_usage_tokens=20935, attempts=[a],
                automatic_adoption=False, automatic_registration=False, status='running')
            dump(OUT / 'prompt-4.json', payload); dump(OUT / 'run.json', state)
            deadline = time.monotonic() + 180
            def remaining():
                n = deadline - time.monotonic()
                if n <= 0: raise TimeoutError('MODEL_TIME_LIMIT')
                return n
            session = SimpleNamespace(w=SimpleNamespace(settings=settings), usage=[], trace=[],
                runtime_diagnostics={'model_requests':[{}]}, _remaining=remaining, progress=lambda **kw:None)
            try:
                response = ResponsesTransport(w.siwc_account).request(session, payload)
                parts = []
                for item in response['output']:
                    if item.get('type') == 'reasoning': continue
                    assert item.get('type') == 'message' and item.get('role') == 'assistant'
                    for part in item.get('content', []):
                        assert part.get('type') == 'output_text'
                        parts.append(part['text'])
                content = ''.join(parts).strip(); dump(OUT / 'response-4.json', dict(content=content))
                if content.startswith('```'): content = content.split('\n', 1)[1].rsplit('```', 1)[0]
                proposal = Proposal.model_validate_json(content, strict=True)
                dump(OUT / 'proposal-4.json', proposal.model_dump())
                if proposal.strategy == 'abstain': a.update(status='abstained', reason=proposal.explanation)
                else:
                    code = proposal.code; check_code(code)
                    (OUT / 'candidate-4.py').write_text(code)
                    diff = ''.join(difflib.unified_diff(preview['code'].splitlines(True), code.splitlines(True),
                        fromfile='previous-candidate-2.py', tofile='model-candidate-4.py'))
                    (OUT / 'model-change.diff').write_text(diff)
                    expected = preview['code'].replace('if y>x: spans.append((x,y,sid))',
                        'if (y-x)*length > tol/10: spans.append((x,y,sid))')
                    import ast
                    only_filter = ast.dump(ast.parse(code)) == ast.dump(ast.parse(expected))
                    dump(OUT / 'scope-check.json', dict(only_authorized_filter_changed=only_filter,
                        old_sha256=hashlib.sha256(preview['code'].encode()).hexdigest(),
                        returned_code_sha256=hashlib.sha256(code.encode()).hexdigest()))
                    assert only_filter, 'Returned code exceeded the authorized repair scope'
                    a.update(status='generated', code_path='candidate-4.py', code_sha256=hashlib.sha256(code.encode()).hexdigest())
            except Exception as exc:
                a.update(status='failed', error=getattr(exc, 'code', type(exc).__name__), message=str(exc)[:300])
            finally:
                usage = {}
                for entry in session.usage:
                    for k, v in entry.items():
                        if type(v) in (int, float): usage[k] = usage.get(k, 0) + v
                charged = usage.get('total_tokens', 32000)
                a.update(usage=usage, usage_known=bool(session.usage), transport_diagnostics=session.runtime_diagnostics)
                state.update(status='closed', cumulative_charged_tokens=20935+charged)
                dump(OUT / 'run.json', state)
            print('MODEL', a['status'], a.get('usage'), a.get('error'), flush=True)
    finally: w.close()

def execute():
    from cad_agent.extensions.adaptation.sandbox import execute as sandbox
    state = read('run.json'); a = state['attempts'][0]
    assert a['status'] == 'generated'
    b = read('input.json'); code = (OUT / a['code_path']).read_text()
    assert hashlib.sha256(code.encode()).hexdigest() == a['code_sha256']
    w = base.workspace()
    try: base.source_check(w, b)
    finally: w.close()
    ex = sandbox(OUT / 'data', b, code, timeout=20); dump(OUT / 'execution-4.json', ex)
    v = base.validator().validate(b, ex['output']); dump(OUT / 'validation-4.json', v)
    a.update(status='completed', validation=v)
    if v['validation_status'] == 'passed': state.update(status='passed', accepted_attempt=4)
    dump(OUT / 'run.json', state)
    print('VALIDATION', v, flush=True)

def verify():
    checks.negatives(); checks.replays(); checks.numeric_diagnostics(); checks.render(); checks.preserve()
    p = read('preservation.json'); p['prior_evidence'] = historical()
    dump(OUT / 'preservation.json', p)
    old = json.loads((PRIOR / 'execution-2.json').read_text())['output']
    new = read('execution-4.json')['output']; removed = []; added = []
    assert old['vertices'] == new['vertices'] and old['area'] == new['area']
    assert old['excluded_crossing_ids'] == new['excluded_crossing_ids']
    for i, (before, after) in enumerate(zip(old['edge_segment_ids'], new['edge_segment_ids'])):
        removed.extend(dict(edge_index=i, segment_id=s) for s in sorted(set(before)-set(after)))
        added.extend(dict(edge_index=i, segment_id=s) for s in sorted(set(after)-set(before)))
    controls = []
    import copy
    b = read('input.json'); v = base.validator()
    for row in removed:
        bad = copy.deepcopy(new); bad['edge_segment_ids'][row['edge_index']].append(row['segment_id'])
        result = v.validate(b, bad)
        assert result['errors'] == ['IRRELEVANT_EDGE_SOURCE']
        controls.append(dict(**row, validation=result))
    dump(OUT / 'support-repair-audit.json', dict(vertices_area_and_exclusions_identical=True,
        removed_support_claims=removed, added_support_claims=added,
        raw_output_not_modified=True, individual_reinsertion_controls=controls,
        previous_candidate_still_rejected=v.validate(b, old)))

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('actions', nargs='+', choices=['prepare', 'generate', 'execute', 'verify'])
    for action in p.parse_args().actions: globals()[action]()
