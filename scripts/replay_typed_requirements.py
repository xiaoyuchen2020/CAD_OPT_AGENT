"""Replay frozen task plans locally; no model, network or source CAD mutation."""
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cad_agent.analysis.recall import pending_calls
from cad_agent.analysis.typed_requirements import annotate_step


def main():
    source = ROOT / 'artifacts/designer-facts-unlimited-final-20261020/evaluated-results.json'
    output = ROOT / 'artifacts/bottleneck-round3-20261020/typed-requirements-report.json'
    baseline = ROOT / 'artifacts/bottleneck-round3-20261020/before/cad_agent/analysis/recall.py'
    spec = importlib.util.spec_from_file_location('before_typed_recall', baseline)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    entries = json.loads(source.read_text())
    rows, counts, removed, retained = [], Counter(), Counter(), Counter()
    for entry in entries:
        task_path = Path(entry['evidence_path']) / 'task.json'
        original = json.loads(task_path.read_text())
        task = deepcopy(original)
        before_calls, after_calls, cells = [], [], []
        for step in task['subtasks']:
            preserved = deepcopy(step)
            annotate_step(step)
            for key in ('requirement', 'status', 'delivered', 'claims', 'evidence_ids', 'operation_receipts'):
                assert step.get(key) == preserved.get(key), (entry['id'], key)
            for prior, current in zip(preserved.get('coverage', []), step.get('coverage', [])):
                for key in ('state', 'evidence_ids', 'question_binding', 'object', 'field'):
                    assert prior.get(key) == current.get(key), (entry['id'], key)
                counts[current['requirement_type']] += 1
                cells.append(dict(requirement_id=step['id'], object=current['object'], field=current['field'],
                    requirement_type=current['requirement_type'], producer=current['producer'],
                    acceptance_contract=current['acceptance_contract']))
            # Re-enable one frozen requirement only to compare routing under
            # equal budgets, without modifying its persisted terminal result.
            replay = deepcopy(task)
            for s in replay['subtasks']:
                s.update(status='pending' if s['id'] == step['id'] else 'partial', calls=0, stalled=0)
            before = old.pending_calls(replay)
            after = pending_calls(replay)
            before_calls.extend(before)
            after_calls.extend(after)
            key = lambda c: (c.get('object'), c.get('field'))
            exact_before = {key(c) for c in before if c['reason'] == 'explicit_object_field_gap'}
            exact_after = {key(c) for c in after if c['reason'] == 'explicit_object_field_gap'}
            for pair in exact_before - exact_after:removed[pair] += 1
            for pair in exact_before & exact_after:retained[pair] += 1
        rows.append(dict(id=entry['id'], task_id=original['task_id'], evidence_path=str(task_path),
            task_sha256=hashlib.sha256(task_path.read_bytes()).hexdigest(),
            original_status=original['status'], cell_types=cells,
            before_pending=before_calls, after_pending=after_calls,
            original_evidence_and_status_preserved=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(version='typed-requirements-replay-v1', cohort_count=len(entries),
        input_path=str(source), input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        baseline_path=str(baseline), baseline_sha256=hashlib.sha256(baseline.read_bytes()).hexdigest(),
        replay_mode='offline single-requirement pending projection; calls=0/stalled=0; unchanged historical results',
        model_calls=0, external_calls=0, source_drawings_mutated=False,
        producer_type_counts=dict(counts),
        removed_automatic_search_count=sum(removed.values()),
        retained_source_search_count=sum(retained.values()),
        removed_automatic_searches=[dict(object=o,field=f,count=n) for (o,f),n in removed.most_common()],
        retained_source_searches=[dict(object=o,field=f,count=n) for (o,f),n in retained.most_common()],
        acceptance='routing regression only; no new geometry, calculation, engineering acceptance or model-quality result',
        tests='tests/test_round3_typed_requirements.py', rows=rows)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in payload.items() if k in (
        'cohort_count','producer_type_counts','removed_automatic_search_count','retained_source_search_count',
        'model_calls','acceptance')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
