"""Offline B06 replay on immutable basement/pump DXF; never invokes a model."""
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
from collections import Counter
import hashlib
import json
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.text_units import read_unit_candidates, read_elevation_annotation
from cad_agent.contracts import SourceRegion
from cad_agent.geometry.source import _native_source
from cad_agent.cache import BoundedCache
from cad_agent.components.dimensions import read_dimensions_from_source
from cad_agent.geometry.room_batch import RoomBatchService
from shapely.geometry import Polygon

FROZEN=ROOT/'artifacts/designer-facts-repaired-20261019'
OUT=ROOT/'artifacts/bottleneck-repairs-20261019'
BASEMENT='snap_fa5c416d0ba39b836b0eeb8b'
PUMP='snap_e316a1345c7a5db25e19458f'


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def index_for(sid):
    directory=FROZEN/'inputs'/sid
    class ReadOnlyStore:
        def get(self,kind,key):
            if (kind,key)!=('drawing',sid):raise KeyError((kind,key))
            return json.loads((directory/'drawing.json').read_text())
        def artifact(self,snapshot,name):
            if snapshot!=sid:raise KeyError(snapshot)
            return directory/name
    store=ReadOnlyStore()
    w=SimpleNamespace(store=store,lock=threading.RLock(),regions=lambda snapshot:[SourceRegion.model_validate(r)
        for r in json.loads(store.artifact(snapshot,'regions.json').read_text())])
    return DrawingIndex(w,sid)


def run():
    started=time.monotonic()
    paths=[FROZEN/'inputs'/sid/name for sid in (BASEMENT,PUMP)
           for name in ('source.dxf','drawing.json','scene.json','regions.json','entities.json')]
    paths += [FROZEN/'attempts'/q/'0001/task.json' for q in ('Q11-A','Q16-A','Q17-A')]
    before={str(p.relative_to(ROOT)):digest(p) for p in paths}
    basement=index_for(BASEMENT)
    lid=basement.drawing['layouts'][0]['layout_id']
    # Reuse native walker: nested INSERT geometry and entity IDs are identical
    # to the production read_dimensions path. No scope is inferred from text.
    native=_native_source(FROZEN/'inputs'/BASEMENT/'source.dxf',BASEMENT,BoundedCache(max_entries=1))
    dimensions=read_dimensions_from_source(native,BASEMENT,lid,None,basement.drawing['unit'])
    task11=json.loads((FROZEN/'attempts/Q11-A/0001/task.json').read_text())
    frozen_ids={d['object_id'] for o in task11['observations'].values() if o['tool']=='read_dimensions' for d in o['data']['items']}
    selected=[d for d in dimensions['dimensions'] if d['object_id'] in frozen_ids]
    assert {d['object_id'] for d in selected}==frozen_ids
    assert all(d['geometry_length_m'] is None for d in selected)
    assert all(not d['target_binding']['confirmed_entity_ids'] for d in selected)
    for d in selected:
        assert any(c['state']=='conflict' for c in d['measurement_comparisons'])
    print('basement dimensions audited',len(selected),flush=True)
    task16=json.loads((FROZEN/'attempts/Q16-A/0001/task.json').read_text())
    elevation_ids={e['region_id'] for e in task16['evidence'].values() if e.get('quote') in ('-0.100','-5.000')}
    elevations=[read_elevation_annotation(basement.by_id[rid]) for rid in sorted(elevation_ids)]
    assert len(elevations)>=2 and all(r and r['derived_height'] is None and r['pairability']=='unresolved' for r in elevations)
    pump=index_for(PUMP);target=pump.by_id['region_8a3aaca06e66314cf7e4907c']
    units=read_unit_candidates(pump,target)
    assert len(units)==1 and units[0]['unit_resolution']=='source_supported_reading'
    assert units[0]['state']=='candidate' and units[0]['measurement_comparability']=='unconfirmed'
    task17=json.loads((FROZEN/'attempts/Q17-A/0001/task.json').read_text())
    batch=deepcopy(next(b for b in task17['batches'].values() if any(i.get('candidate_area_m2')==136.8504 for i in b['items'])))
    svc=RoomBatchService.__new__(RoomBatchService);svc.index=pump
    svc.measure_selected(batch)
    item=next(i for i in batch['items'] if i.get('candidate_area_m2')==136.8504)
    polygon=Polygon(item['highlight']['boundary'],item['highlight'].get('holes',[]))
    assert round(polygon.area*.001**2,6)==136.8504
    assert item['area_m2'] is None and item['area_basis']=='selected_wall_enclosure'
    check=next(c for c in item['area_annotation_checks'] if c['value']==154.05)
    assert check['unit']=='m2' and check['numeric_difference']==-17.1996
    assert check['semantic_difference'] is None and check['basis_comparability']=='unconfirmed'
    print('pump native TEXT unit relation and saved candidate polygon audited',flush=True)
    after={str(p.relative_to(ROOT)):digest(p) for p in paths}
    assert before==after
    report=dict(evidence_class='offline_frozen_dxf_and_saved_candidate_polygon_replay',model_calls=0,
        elapsed_seconds=round(time.monotonic()-started,3),input_hashes=before,input_unchanged=True,
        basement=dict(source_name=basement.drawing['name'],unit=basement.drawing['unit'],
            total_native_dimensions=dimensions['total_native_dimensions'],truncated=dimensions['truncated'],
            returned_dimension_count=len(dimensions['dimensions']),
            status_counts=dict(Counter(d['status'] for d in dimensions['dimensions'])),
            frozen_Q11_dimension_count=len(selected),frozen_Q11_dimensions=selected,
            evidence_scope='All returned native dimensions examined; frozen Q11 page IDs independently rebound. Not ramp-width measurements.',
            source_diagnostics=dimensions['source_diagnostics']),
        Q16=dict(elevations=elevations,numeric_height_delivered=False,
            remaining=['datum','unit','surface','same vertical control point','member and equipment occupation']),
        Q17=dict(unit_candidates=units,candidate_area_m2=item['candidate_area_m2'],area_m2=item['area_m2'],
            area_basis=item['area_basis'],polygon_area_recomputed_m2=round(polygon.area*.001**2,6),
            geometry_check=item['geometry_check'],diagnostics=item['diagnostics'],area_annotation_checks=item['area_annotation_checks'],
            scope=batch['scope']),
        limitations=['No model calls, deployment or engineering/business acceptance.',
            'Dimension-channel disagreement does not prove a lower-level distance algorithm wrong.',
            'Native definition controls are bound; semantic target identity is not confirmed by endpoint proximity.',
            'Basement physical drawing units remain unknown; supplied unit overrides remain assumptions.',
            'Q16 signed numbers are not paired into height without datum, surface and control-point evidence.',
            'Q17 source-supported m2 reading does not establish net area, fire-compartment area or area equivalence.',
            'Q17 geometric replay reuses the frozen candidate boundary; its original opening/finish/partial-source limitations remain.'])
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'b06-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(output=str(OUT/'b06-replay.json'),seconds=report['elapsed_seconds'],model_calls=0,
        native_dimensions=dimensions['total_native_dimensions'],q11_rebound=len(selected),q16=len(elevations),
        q17_unit=check['unit'],numeric_difference=check['numeric_difference'],semantic_difference=check['semantic_difference']),ensure_ascii=False))


if __name__=='__main__':run()
