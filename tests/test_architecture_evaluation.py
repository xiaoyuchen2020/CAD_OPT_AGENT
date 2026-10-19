import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('architecture_evaluation', ROOT/'scripts/evaluate_architecture.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_fixed_contracts_are_complete_and_correct_without_runtime(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('Synthetic evaluation must not access network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    report = evaluation.evaluate(repeats=1)
    assert report['runtime_mode'] == 'synthetic_contract'
    assert report['model_calls'] == 0
    assert len(report['sources']) == 3
    assert len({s['sha256'] for s in report['sources']}) == 3
    assert {s['unit'] for s in report['sources']} == {'mm','m','unknown'}
    assert report['summary']['attempts'] == 30
    assert report['summary']['complete'] == report['summary']['correct'] == 30
    assert len(report['code_sha256']) == len(report['manifest_sha256']) == 64
    assert all(p['p95_seconds'] >= p['p50_seconds'] for p in report['phases'].values())


def test_wrong_golden_is_reported_separately_from_completeness(tmp_path):
    manifest = json.loads(evaluation.DEFAULT_MANIFEST.read_text())
    manifest['tasks'] = [dict(manifest['tasks'][3], expected={'status':'measured','value':99.0})]
    path = tmp_path/'wrong-golden.json'; path.write_text(json.dumps(manifest))
    report = evaluation.evaluate(path,repeats=1)
    assert report['summary']['attempts'] == report['summary']['complete'] == 3
    assert report['summary']['correct'] == 0
    assert report['summary']['failed'] == 3


def test_exception_is_retained_in_denominator_and_timings(monkeypatch):
    def fail(*args):
        raise RuntimeError('injected failure')
    monkeypatch.setattr(evaluation, 'execute', fail)
    report = evaluation.evaluate(repeats=1)
    assert report['summary']['attempts'] == report['summary']['failed'] == 30
    assert report['summary']['complete'] == report['summary']['correct'] == 0
    assert all(row['details']['error'] == 'RuntimeError' for row in report['observations'])
    assert report['summary']['p95_seconds'] >= 0


def test_source_setup_failure_is_counted_for_each_dependent_case(monkeypatch):
    original = evaluation.make_drawing
    def fail_one(workspace, source):
        if source['key'] == 'beta':
            raise ValueError('injected parser failure')
        return original(workspace, source)
    monkeypatch.setattr(evaluation, 'make_drawing', fail_one)
    report = evaluation.evaluate(repeats=1)
    assert report['summary']['attempts'] == 30
    assert report['summary']['failed'] == 9
    assert report['summary']['correct'] == 21
    assert next(s for s in report['sources'] if s['key'] == 'beta')['setup_error'] == 'ValueError'
