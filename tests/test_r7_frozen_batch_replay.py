"""Replay all 12 frozen missing-owner calls on copied real drawing sources."""
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from test_batch_reads import invoke
from test_remediation_assembly import verify_sources

FROZEN = Path(__file__).resolve().parents[1] / 'artifacts/designer-facts-b-final-20261019'
# Explicit test fixture owners. These select a single relevant original requirement;
# production never supplies or infers an owner. q1 for Q08 is source inspection,
# not an assertion that the retrieved roof heading proves the garage/floor answer.
OWNERS = {'Q02-B': ['q1', 'q6'], 'Q05-B': ['q1', 'q1'],
          'Q08-B': ['q1', 'q1', 'q1', 'q1'], 'Q10-B': ['q2', 'q2'],
          'Q19-B': ['q1', 'q1']}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_frozen_12_missing_owners_reject_and_explicit_correction_reads_original(workspace, monkeypatch):
    if not FROZEN.exists():
        pytest.skip('Frozen production artifacts not distributed')
    def no_model(*args, **kwargs):
        pytest.fail('Offline replay must not call a model')
    monkeypatch.setattr(workspace.gateway, 'chat_turn', no_model)
    records, source_hashes = [], {}
    for case, owners in OWNERS.items():
        run_path = FROZEN / 'attempts' / case / '0001/run.json'
        run = json.loads(run_path.read_text())
        sid = run['snapshot_id']
        source = FROZEN / 'inputs' / sid
        if sid not in source_hashes:
            source_hashes[sid] = {name: digest(source/name) for name in ('source.dxf', 'drawing.json', 'regions.json', 'scene.json', 'entities.json')}
            workspace.store.put('drawing', sid, json.loads((source/'drawing.json').read_text()))
            dest = workspace.store.root / 'drawings' / sid
            dest.mkdir(parents=True, exist_ok=True)
            for name in ('regions.json', 'scene.json', 'entities.json'):
                shutil.copyfile(source/name, dest/name)
        failures = [(i, t) for i, t in enumerate(run['trace']) if t.get('source_read_batch') and
                    t.get('error', {}).get('code') == 'INVALID_TOOL_INPUT']
        assert len(failures) == len(owners)
        for (trace_position, entry), owner in zip(failures, owners):
            request = dict(run['request'], mode='agent', runtime='native',
                           resume_task_id='', parent_run_id='', source_read_batching=True)
            bad = dict(tool=entry['tool'], arguments=entry['arguments'], task_ids=entry['task_ids'])
            assert bad['task_ids'] == []
            s = AgentRunner(workspace, ChatRequest.model_validate(request))
            s.task.auto_plan()
            failure = invoke(s, [bad])
            error = failure['items'][0]['result']['error']
            assert error['issue'] == 'missing_task_ids' and error['path'] == 'reads.0.task_ids'
            assert s.calls == len(s.trace) == 1 and s.trace[0]['status'] == 'failed'
            assert not s.evidence and not s.observations and not s.policy.cache
            assert sum(t['calls'] for t in s.task.data['subtasks']) == 0
            # The repair is test input, never a runtime owner fallback.
            corrected = dict(bad, task_ids=[owner])
            success = invoke(s, [corrected])
            assert not success['pending_reads']
            item = success['items'][0]
            assert not item['result'].get('error'), (case, trace_position, item)
            assert s.calls == len(s.trace) == 2 and s.trace[1]['status'] == 'completed'
            assert s.evidence and set(s.trace[1]['task_ids']) == {owner}
            assert sum(t['calls'] for t in s.task.data['subtasks']) == 1
            assert all(not t['evidence_ids'] for t in s.task.data['subtasks'] if t['id'] != owner)
            for evidence in s.evidence.values():
                verify_sources(s.index, evidence)
            records.append(dict(case=case, original_run=str(run_path.relative_to(FROZEN)),
                original_run_sha256=digest(run_path), original_trace_position=trace_position,
                original_batch=entry['source_read_batch'], original_position=entry['source_read_position'],
                frozen_child=bad, replay_path=error['path'], error=error,
                corrected_owner=owner, owner_basis='explicit test fixture; no production inference',
                corrected_status=s.trace[1]['status'], evidence_count=len(s.evidence),
                source_spans_verified=True, calls=s.calls, failed_calls=1, successful_calls=1,
                pagination_retained=item['result'].get('has_more', False),
                no_business_completion_inferred=True))
    assert len(records) == 12
    for sid, hashes in source_hashes.items():
        assert hashes == {name: digest(FROZEN/'inputs'/sid/name) for name in hashes}
    diagnostics = json.loads((FROZEN/'failure-diagnostics.json').read_text())
    expected = sum(t.get('batch') is not None and t['error']['code'] == 'INVALID_TOOL_INPUT'
        for row in diagnostics['current_rows'] for t in row['failed_tools'])
    assert expected == len(records)
    report_dir = os.environ.get('R7_REPORT_DIR')
    if report_dir:
        destination = Path(report_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination/'r7-frozen-replay.json').write_text(json.dumps(dict(
            replay_type='offline frozen child arguments; real source reader; no model calls',
            frozen_failed_calls=12, errors_located=12, corrected_reads_completed=12,
            source_hashes=source_hashes, records=records), ensure_ascii=False, indent=2))
