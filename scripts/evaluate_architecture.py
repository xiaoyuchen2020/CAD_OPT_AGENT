"""Offline fixed synthetic contracts; never initializes credentials/model runtimes."""
from __future__ import annotations
import argparse
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ezdxf
from cad_agent.adapters.store import Store
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis import retrieval
from cad_agent.cache import BoundedCache
from cad_agent.cad.reader import read_drawing
from cad_agent.cad.regions import build_regions
from cad_agent.contracts import SourceRegion
from cad_agent.geometry.contracts import GeometryMeasure
from cad_agent.geometry.service import GeometryService

DEFAULT_MANIFEST = ROOT / 'tests/fixtures/architecture_tasks.json'


class LocalWorkspace:
    """Only the store/parser contracts needed by the evaluated local functions."""
    def __init__(self, root):
        self.store = Store(root)
        self.lock = threading.RLock()

    def regions(self, sid):
        return [SourceRegion.model_validate(row) for row in json.loads(self.store.artifact(sid, 'regions.json').read_text())]


def make_drawing(workspace, spec):
    doc = ezdxf.new('R2018'); doc.units = spec['units']
    model = doc.modelspace(); scale = spec['scale']
    x, y, xx, yy = spec['frame']
    model.add_lwpolyline([(x*scale,y*scale),(xx*scale,y*scale),(xx*scale,yy*scale),(x*scale,yy*scale)], close=True, dxfattribs={'layer':'FRAME'})
    width, height = spec['rectangle']
    model.add_lwpolyline([(0,0),(width*scale,0),(width*scale,height*scale),(0,height*scale)], close=True, dxfattribs={'layer':'AREA_TARGET'})
    for i, line in enumerate(spec['lines']):
        model.add_text(line, dxfattribs={'insert':(10*scale,(15-i*3)*scale),'height':.3*scale})
    paper = doc.layouts.get('Layout1')
    paper.add_text(spec['paper_text'], dxfattribs={'insert':(5,5),'height':1})
    stream = io.StringIO(); doc.write(stream)
    content = stream.getvalue().encode(); digest = sha256(content).hexdigest()
    sid = 'snap_' + digest[:24]
    path = workspace.store.artifact(sid, 'source.dxf')
    if not path.exists():
        path.write_bytes(content)
    elif sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Synthetic drawing input checksum mismatch')
    texts, scene, rectangles, segments, diagnostics = read_drawing(path, sid)
    regions, frames = build_regions(texts, scene, rectangles, segments, sid)
    scene.update(frames=frames, snapshot_id=sid)
    workspace.store.write_json(workspace.store.artifact(sid, 'scene.json'), scene)
    workspace.store.write_json(workspace.store.artifact(sid, 'regions.json'), [r.model_dump() for r in regions])
    record = dict(id=sid, snapshot_id=sid, sha256=digest, unit=scene['unit'], layouts=scene['layouts'], frames=frames, synthetic=True)
    references = workspace.store.share_artifacts(sid)
    if references:
        record['artifact_references'] = references
    workspace.store.put('drawing', sid, record)
    return record | {'key':spec['key'], 'source_bytes':len(content), 'parser_diagnostics':[d.model_dump() for d in diagnostics]}


def execute(workspace, drawing, task):
    index = DrawingIndex(workspace, drawing['id'])
    op = task['operation']; expected = task['expected']
    model = next(l['layout_id'] for l in drawing['layouts'] if l['name'] == 'Model')
    if op == 'area':
        service = GeometryService(workspace, drawing['id'])
        candidates = [r for r in service.index.data['entities'] if r['layer'] == 'AREA_TARGET']
        if len(candidates) != 1:
            return False, False, {'error':'area target unavailable', 'candidate_count':len(candidates)}
        result = service.measure(GeometryMeasure(operation='area', entity_ids=[candidates[0]['entity_id']]))
        complete = all(k in result for k in ('value','status','source_sha256','entity_ids'))
        correct = result['status'] == expected['status'] and result['source_sha256'] == drawing['sha256']
        if expected['value'] is None:
            correct &= result['value'] is None and expected['diagnostic'] in result['diagnostics']
        else:
            correct &= result['value'] is not None and math.isclose(result['value'], expected['value'], abs_tol=1e-6)
        return complete, bool(correct), {k:result[k] for k in ('value','status','diagnostics','source_sha256')}
    if op == 'scope':
        lid = next(l['layout_id'] for l in drawing['layouts'] if l['name'] == 'Layout1')
        result = index.search(task['query'], layout_id=lid)
        return 'total' in result, result['total'] == expected['total'] and [r['snippet'] for r in result['items']] == [expected['quote']], result
    if op == 'pagination':
        pages = [index.search(task['query'], layout_id=model, offset=i, limit=1) for i in range(expected['total'])]
        rows = [r for page in pages for r in page['items']]
        complete = len(rows) == expected['total'] and len({r['region_id'] for r in rows}) == expected['total']
        correct = (set(r['snippet'] for r in rows) == set(expected['quotes']) and
                   all(p['total'] == expected['total'] for p in pages) and
                   [p['has_more'] for p in pages] == [True, True, False])
        return complete, correct, {'pages':pages}
    result = index.search(task['query'], layout_id=model, mode='lexical' if op == 'lexical' else 'keyword')
    if op == 'isolation':
        return 'total' in result, result['total'] == expected['total'], result
    rows = result['items']
    complete = len(rows) >= expected.get('count', expected.get('minimum_hits', 1))
    evidence = []
    for hit in rows:
        region = index.by_id[hit['region_id']]
        if expected['quote'] in region.text:
            start = region.text.index(expected['quote'])
            evidence.append(index.evidence(region, start, start + len(expected['quote'])))
    correct = bool(evidence) and all(ev['quote'] == expected['quote'] and ev['snapshot_id'] == drawing['id']
        and ev['layout_id'] == model and ev['entity_ids']
        and index.by_id[ev['region_id']].text[slice(*ev['text_span'])] == expected['quote'] for ev in evidence)
    if op == 'literal':
        correct &= result['total'] == expected['count']
    return complete, bool(correct), {'result':result, 'evidence':evidence}


def clear_memory(workspace):
    getattr(workspace, '_derived_caches', {}).clear()
    for name in ('_reading_cache','_scene_cache','_native_geometry','_geometry_cache'):
        if hasattr(workspace,name): delattr(workspace,name)
    retrieval._CHUNKS = BoundedCache(max_entries=8)
    retrieval._LEXICAL = BoundedCache(max_entries=32)


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values)*q)-1)] if values else None


def summarize(rows):
    durations = [r['seconds'] for r in rows]  # Failures remain in denominator and latency.
    return {'attempts':len(rows), 'complete':sum(r['complete'] for r in rows),
            'correct':sum(r['correct'] for r in rows), 'failed':sum(not r['correct'] for r in rows),
            'p50_seconds':statistics.median(durations) if durations else None,
            'p95_seconds':percentile(durations,.95)}


def evaluate(manifest_path=DEFAULT_MANIFEST, repeats=3):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('runtime_mode') != 'synthetic_contract' or repeats < 1:
        raise ValueError('Only synthetic_contract manifests and positive repeats are supported')
    observations = []
    with tempfile.TemporaryDirectory(prefix='cad-architecture-eval-') as folder:
        workspace = LocalWorkspace(Path(folder))
        drawings = {}
        for source_spec in manifest['drawings']:
            try:
                drawings[source_spec['key']] = make_drawing(workspace, source_spec)
            except Exception as exc:
                drawings[source_spec['key']] = {'key':source_spec['key'], 'setup_error':type(exc).__name__, 'message':str(exc)}
        tasks = manifest['tasks']
        for phase in ('cold','warm','alternating'):
            ordered = tasks if phase != 'warm' else sorted(tasks,key=lambda t:t['drawing'])
            for repeat in range(repeats):
                for task in ordered:
                    if phase == 'cold': clear_memory(workspace)
                    started = time.perf_counter()
                    try:
                        drawing = drawings[task['drawing']]
                        if 'setup_error' in drawing:
                            raise RuntimeError('Source setup failed: ' + drawing['setup_error'] + ': ' + drawing['message'])
                        complete, correct, details = execute(workspace, drawing, task)
                    except Exception as exc:
                        complete, correct, details = False, False, {'error':type(exc).__name__, 'message':str(exc)}
                    observations.append(dict(task_id=task['id'],drawing=task['drawing'],phase=phase,repeat=repeat,
                        seconds=time.perf_counter()-started,complete=complete,correct=correct,details=details))
    files = [Path(__file__), manifest_path, *sorted((ROOT/'cad_agent').rglob('*.py'))]
    hashes = {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name:sha256(p.read_bytes()).hexdigest() for p in files}
    return dict(schema_version=1, runtime_mode='synthetic_contract', model_calls=0,
        scope='Synthetic local parser/retrieval/evidence/geometry contracts; not real drawing engineering accuracy.',
        cold_definition='In-memory derived caches cleared per case; OS file cache and persisted geometry/measurements retained.',
        phase_definition={'warm':'Grouped by drawing after cold phase; no cache reset.', 'alternating':'Fixed alpha/beta/gamma interleaving from manifest; no cache reset.'},
        environment={'python':platform.python_version(),'platform':platform.platform(),'ezdxf':ezdxf.__version__},
        manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(), code_hashes=hashes,
        code_sha256=sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),
        sources=list(drawings.values()),summary=summarize(observations),
        phases={phase:summarize([r for r in observations if r['phase']==phase]) for phase in ('cold','warm','alternating')},
        observations=observations)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=DEFAULT_MANIFEST)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=evaluate(args.manifest,args.repeats)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report['summary'],ensure_ascii=False))
    return int(report['summary']['failed'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
