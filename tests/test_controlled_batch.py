"""Batch runner acceptance with in-process HTTP mock, no remote model access."""
from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import httpx
import pytest

from test_agent import final
from test_siwc import response, sse
from test_siwc_concurrency import seed
from cad_agent.runtimes.responses_subscription import ResponsesTransport

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('controlled_batch', ROOT / 'scripts/run_controlled_batch.py')
batch = importlib.util.module_from_spec(spec); spec.loader.exec_module(batch)


def setup_batch(tmp_path, workspace, demo_bytes, capacity, batch_mode=False):
    drawing = workspace.ingest('fixture.dxf', demo_bytes)
    source = tmp_path / 'source'; source.mkdir()
    (source / 'cad_agent').symlink_to(ROOT / 'cad_agent', target_is_directory=True)
    src = source / 'artifacts/fixture/data'
    shutil.copytree(workspace.settings.data_dir, src)
    caps = dict(seconds=15, tool_calls=8, rounds=8, exploration_soft_tokens=96000,
                context_chars=84000, final_context_chars=24000)
    plan = dict(runtime='responses_subscription', model='gpt-6.1-sol', model_profile='siwc_sol',
                sources={'fixture':['fixture', drawing['id']]}, cases=[dict(id=f'Q{i:02d}-A',
                question=('1. 读取第一条材料原文。\n2. 读取第二条材料原文。' if batch_mode else '读取图纸标题原文并引用证据。'), source='fixture', caps=caps, pair_id=f'Q{i:02d}',
                arm='A', title='fixture', rules=[]) for i in range(1,5)],
                fixed_total_soft_tokens={f'Q{i:02d}-A':150000 for i in range(1,5)})
    plan_path = tmp_path / 'plan.json'; batch.dump(plan_path, plan)
    shutil.copy2(ROOT / 'artifacts/designer-facts-latest-20261019/budget_control.py', tmp_path / 'budget_control.py')
    args = Namespace(output=tmp_path / 'out', plan=plan_path, source_root=source,
                     account_data=tmp_path / 'account', concurrency=capacity, queue_timeout=2,
                     retry_case=[], configure_account_capacity=False)
    args.output.mkdir(); seed(args.account_data, capacity)
    return args, batch.prepare(args), drawing


@pytest.mark.parametrize('capacity,batch_mode', [(1,False), (2,False), (2,True)])
def test_frozen_batch_isolated_bounded_and_no_replay(tmp_path, workspace, demo_bytes, monkeypatch, capacity, batch_mode):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
    # The original frozen budget hook installs process-wide class patches; restore
    # them after each local test, as a real CLI run has its own process lifetime.
    for klass, name in [(CadAgentSession, '__init__'), (CadAgentSession, 'round_limit'),
                        (CodexSubscriptionRuntime, 'exploration_budget'), (CodexSubscriptionRuntime, 'total_budget')]:
        monkeypatch.setattr(klass, name, getattr(klass, name))
    args, manifest, drawing = setup_batch(tmp_path, workspace, demo_bytes, capacity, batch_mode)
    regions = [r.region_id for r in workspace.regions(drawing['id'])]
    assert len(regions) >= 4
    if batch_mode:
        from cad_agent.analysis.agent import AgentRunner
        original_run = AgentRunner.run
        def planned_run(session, history=None):
            session.request.source_read_batching = True  # Explicit mock test opt-in.
            session.task.auto_plan()  # Predeclared local test plan, no model call.
            return original_run(session, history)
        monkeypatch.setattr(AgentRunner, 'run', planned_run)
    rid = workspace.regions(drawing['id'])[0].region_id
    lock = threading.Lock(); seen = []; live = peak = 0
    original_request = ResponsesTransport.request
    def request(self, session, payload):
        self._test_session = session
        return original_request(self, session, payload)
    def handle(request, session):
        nonlocal live, peak
        with lock: live += 1; peak = max(peak, live)
        try:
            payload = json.loads(request.content)
            seen.append((session.w.ab_current_case['id'], session.w.settings.data_dir, payload['model']))
            time.sleep(.08)
            if not session.evidence:
                if batch_mode:
                    selected = regions[:2] if int(session.w.ab_current_case['id'][1:3]) % 2 else regions[2:4]
                    owners = [p['id'] for p in session.task.parts]
                    reads = [dict(tool='read_source_context', arguments={'region_id':region, 'neighbors':0}, task_ids=[owner])
                             for region, owner in zip(selected, owners)]
                    result = response([dict(type='function_call', namespace='cad', name='batch_read_sources',
                         arguments=json.dumps({'reads':reads}), call_id='batch')])
                else:
                    result = response([dict(type='function_call', namespace='cad', name='read_source_context',
                         arguments=json.dumps({'region_id':rid}), call_id='read')])
            else:
                answer = final(next(iter(session.evidence)))
                if batch_mode:
                    assert session.calls == 2
                    claims = [dict(text='该条款的材料要求见引用原文。', state='observed',
                         evidence_ids=step['evidence_ids'], question_part_ids=[step['id']])
                         for step in session.task.data['subtasks']]
                    answer = json.dumps(dict(claims=claims, missing_information=[]), ensure_ascii=False)
                result = response([dict(type='message', content=[dict(type='output_text', text=answer)])])
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, text=sse(result))
        finally:
            with lock: live -= 1
    monkeypatch.setattr(ResponsesTransport, 'request', request)
    monkeypatch.setattr(ResponsesTransport, 'client', lambda self, timeout:httpx.Client(transport=httpx.MockTransport(lambda req:handle(req,self._test_session))))
    monkeypatch.setattr(batch, 'STOP', False)
    batch.execute(args, manifest)
    results = json.loads((args.output / 'results.json').read_text())
    assert len(results) == 4 and all(r['status'] == 'completed' for r in results)
    assert peak == capacity and len(seen) == 8
    assert len({str(item[1]) for item in seen}) == 4
    assert {item[2] for item in seen} == {'gpt-6.1-sol'}
    if not batch_mode: assert len({r['answer'] for r in results}) == 1
    assert len({r['tokens'] for r in results}) == 1
    for result in results:
        state = json.loads((Path(result['evidence_path']) / 'state.json').read_text())
        assert state['case']['caps'] == json.loads(args.plan.read_text())['cases'][0]['caps']
        run = json.loads((Path(result['evidence_path']) / 'run.json').read_text())
        assert run['request']['allow_fallback'] is False
        references = json.loads((Path(result['evidence_path']) / 'input-references.json').read_text())
        input_dir = Path(result['evidence_path']) / references['relative_to']
        dxf = input_dir / 'source.dxf'
        assert dxf.is_symlink()
        assert dxf.resolve() == (args.output / 'inputs' / drawing['id'] / 'source.dxf').resolve()
        assert references['files']['source.dxf']['sha256'] == drawing['sha256']
        if batch_mode:
            task = json.loads((Path(result['evidence_path']) / 'task.json').read_text())
            selected = regions[:2] if int(result['id'][1:3]) % 2 else regions[2:4]
            assert {e['region_id'] for e in task['evidence'].values()} == set(selected)
            for step, region in zip(task['subtasks'], selected):
                assert step['calls'] == 1
                assert {task['evidence'][eid]['region_id'] for eid in step['evidence_ids']} == {region}
            assert len(run['trace']) == 2
            assert all(t['tool'] == 'read_source_context' for t in run['trace'])
        assert result['queue_seconds'] >= 0 and result['execution_seconds'] >= 0
        assert len(batch.read_records(Path(result['evidence_path']) / 'data', 'chat_run')) == 1
    # A new invocation gathers receipts, but does not submit prior cases again.
    before = list(seen)
    batch.execute(args, manifest)
    assert seen == before
    assert len(json.loads((args.output / 'results.json').read_text())) == 4
    assert batch.prepare(args) == manifest
    # Retry is explicit, gets a new path/receipt, and preserves the original bytes.
    old = (args.output / 'attempts/Q01-A/0001/run.json').read_bytes()
    args.retry_case = ['Q01-A']
    batch.execute(args, manifest)
    assert len(seen) == 10
    assert (args.output / 'attempts/Q01-A/0001/run.json').read_bytes() == old
    assert (args.output / 'attempts/Q01-A/0002/run.json').exists()
    assert len(json.loads((args.output / 'results.json').read_text())) == 5


def test_interrupted_reservation_is_preserved_and_not_pending(tmp_path):
    out = tmp_path / 'out'; path = out / 'attempts/Q01-A/0001'
    state = {'state':'reserved', 'case':{'id':'Q01-A'}, 'attempt':1, 'concurrency':2}
    batch.dump(path / 'state.json', state)
    original = (path / 'state.json').read_bytes()
    results, attempted = batch.restore(out, {})
    assert results == [] and attempted == {'Q01-A'}
    assert (path / 'state.json').read_bytes() == original
    receipt = json.loads((path / 'interrupted.json').read_text())
    assert receipt['automatic_replay'] is False
    assert receipt['status'] == 'interrupted_unknown_outcome'


def test_frozen_plan_and_code_tamper_fail_closed(tmp_path, workspace, demo_bytes):
    args, manifest, _ = setup_batch(tmp_path, workspace, demo_bytes, 1)
    module = args.output / 'frozen-code/cad_agent/config.py'
    module.write_text(module.read_text()+'\n# changed\n')
    with pytest.raises(ValueError, match='Frozen code changed'): batch.prepare(args)


def test_prepare_only_cli_and_frozen_plan_tamper(tmp_path, workspace, demo_bytes):
    args, manifest, _ = setup_batch(tmp_path, workspace, demo_bytes, 1)
    fresh = tmp_path / 'prepare-only'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/run_controlled_batch.py'),
        '--plan', str(args.plan), '--output', str(fresh), '--source-root', str(args.source_root),
        '--prepare-only'], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'no model requests' in result.stdout and not (fresh / 'attempts').exists()
    frozen_plan = args.output / 'plan.json'
    plan = json.loads(frozen_plan.read_text()); plan['cases'][0]['caps']['seconds'] += 100
    batch.dump(frozen_plan, plan)
    with pytest.raises(ValueError, match='Plan differs'): batch.prepare(args)
