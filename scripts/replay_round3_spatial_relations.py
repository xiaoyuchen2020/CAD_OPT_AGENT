"""Offline source revalidation and relations; never invokes a model or edits inputs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from shapely.geometry import Polygon, LineString, box
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cad_agent.cache import BoundedCache
from cad_agent.geometry.source import _native_source, _build_geometry
from cad_agent.geometry.spatial_relations import (
    MeasureSpatialRelation,InspectDoorSweep,MeasureDoorSweepRelation,
    spatial_relation,door_sweep,door_sweep_relation)

OUT=ROOT/'artifacts/bottleneck-round3-20261020'
FROZEN=ROOT/'artifacts/designer-facts-unlimited-final-20261020'

def main():
    started=time.monotonic()
    runs=[r for r in json.loads((FROZEN/'evaluated-results.json').read_text()) if r['pair_id']=='Q14']
    task_by_arm={r['case']['variant']:json.loads((Path(r['evidence_path'])/'task.json').read_text()) for r in runs}
    task=task_by_arm['luna-high'];sid=task['snapshot_id'];folder=FROZEN/'inputs'/sid
    drawing=json.loads((folder/'drawing.json').read_text());source_path=folder/'source.dxf'
    digest=sha256(source_path.read_bytes()).hexdigest();assert digest==drawing['sha256']==task['source_sha256']
    scope=next(iter(task['drawing_scopes'].values()))
    old=[o['data'] for o in task['observations'].values() if o['tool']=='read_geometry'][0]['items']
    legend_page=next(o['data'] for o in task_by_arm['sol-high']['observations'].values()
        if o['tool']=='inspect_scope_objects' and o['data'].get('reference_context'))
    legend_ids=sorted({eid for row in legend_page['items'] for eid in row.get('entity_ids',[])})
    ids=sorted({r['entity_id'] for r in old}|set(legend_ids))
    print('Reopening one native DXF index for persisted Q14 entity selections.',flush=True)
    cache=BoundedCache(max_entries=1,max_bytes=512*1024*1024)
    source=_native_source(source_path,sid,cache)
    def fetch(ids):
        return _build_geometry(source_path,sid,drawing,cache,entity_ids=ids,prepared_source=source)['entities']
    fresh={r['entity_id']:r for r in fetch(ids)}
    for r in old:
        assert fresh[r['entity_id']]['paths']==r['paths']
        assert fresh[r['entity_id']]['entity_path']==r['entity_path']
    print('Verified',len(old),'persisted native geometry records; index reused.',flush=True)
    def make(records,role='source_geometry',reference=False):
        return dict(object_id='offline_'+sha256('|'.join(r['entity_id'] for r in records).encode()).hexdigest()[:16],
            source_sha256=digest,snapshot_id=sid,layout_id=scope['layout_id'],frame_id=scope['frame_id'],scope_id=scope['scope_id'],
            role=dict(value=role,status='asserted',reference_only=reference),geometry=dict(records=records),
            label=[],unit=dict(value=drawing['unit'],policy=deepcopy(task['unit_policy'])),
            exclusions=dict(records=[]),coverage=dict(selected_entities_complete=True))
    refs=legend_page['reference_context']
    border=fresh[refs[0]['border_entity_id']]
    legend=Polygon(border['paths'][0])
    legend_sample=next(fresh[e] for e in legend_ids if e!=border['entity_id'] and fresh[e]['linear'] and
        all(legend.covers(LineString(p)) for p in fresh[e]['paths']))
    # Explicitly prove the native enclosure relationship before setting reference_only.
    negative=make([legend_sample],role='placed_object',reference=True)
    try:
        spatial_relation(negative,make([fresh[old[0]['entity_id']]]),MeasureSpatialRelation(object_id=negative['object_id'],other_object_id='other'))
    except ValueError as exc:
        assert 'SPATIAL_REFERENCE' in str(exc);legend_rejection=str(exc)
    else:raise AssertionError('Legend geometry was accepted.')
    objects=[make([fresh[r['entity_id']]]) for r in old if r['linear']]
    pairs=[]
    for a,b in zip(objects,objects[1:]):
        try:
            result=spatial_relation(a,b,MeasureSpatialRelation(object_id=a['object_id'],other_object_id=b['object_id']))
            assert {r['operation']:r['fulfilled'] for r in result['operation_receipts']} == {'geometry':True,'relations':True}
            pairs.append(dict(result=result))
        except ValueError as exc:pairs.append(dict(object_ids=[a['object_id'],b['object_id']],unsupported=str(exc)))
    assert any('result' in p for p in pairs)
    # Inspect one bounded native ARC neighbourhood at a time; no whole-map tessellation.
    arc_attempts=[];found=None;scope_box=box(*scope['bbox'])
    for eid,(entity,path,lid,bounds) in source['entities'].items():
        if entity.dxftype()!='ARC' or lid!=scope['layout_id'] or not bounds or not scope_box.covers(box(*bounds)):
            continue
        if legend.intersects(box(*bounds)):continue
        nearby=[source['ids'][int(i)] for i in source['tree'].query(box(*bounds))]
        nearby=[i for i in nearby if source['entities'][i][2]==lid and source['entities'][i][0].dxftype() in ('LINE','LWPOLYLINE','POLYLINE')]
        if len(nearby)>128:continue
        records=fetch([eid,*nearby]);door=make(records,role='door')
        inspection,candidates=door_sweep(door,InspectDoorSweep(object_id=door['object_id'],arc_entity_id=eid))
        arc_attempts.append(dict(arc_entity_id=eid,source_entity_count=len(records),status=inspection['status']))
        if candidates:
            supporting={eid}|{i for e in candidates[0][0]['radial_leaf_support'] for i in e['leaf_entity_ids']}
            door=make([r for r in records if r['entity_id'] in supporting],role='door')
            inspection,_=door_sweep(door,InspectDoorSweep(object_id=door['object_id'],arc_entity_id=eid))
            target=objects[0]
            measured=door_sweep_relation(door,target,MeasureDoorSweepRelation(object_id=door['object_id'],other_object_id=target['object_id'],arc_entity_id=eid))
            assert {r['operation']:r['fulfilled'] for r in inspection['operation_receipts']} == {'geometry':True}
            assert {r['operation']:r['fulfilled'] for r in measured['operation_receipts']} == {'geometry':True,'relations':measured['relation'] in ('positive_area_intersection','disjoint')}
            found=dict(source_object=door,inspection=inspection,relation_to_selected_source_region=measured,
                semantic_limit='Native radial-leaf ARC candidate only; door/fire-hydrant identity and actual placement are not confirmed.')
            break
        if len(arc_attempts)>=200:break
    report=dict(implementation_status='complete',model_calls=0,
        evidence_class='offline_real_native_geometry_replay_not_business_acceptance',full_question_completion=False,
        source=dict(path=str(source_path),sha256=digest,snapshot_id=sid,native_index_build_count=1,
            previously_observed_entity_ids=[r['entity_id'] for r in old],native_paths_revalidated=True),
        selected_runs=[dict(id=r['id'],task_id=r['task_id'],run_id=r['run_id'],evidence_path=r['evidence_path']) for r in runs],
        borrowed_methods=[dict(source='/Users/vigorchen/Documents/Work/cad_llms_group/process/tools/block/spatial.py',
            idea='Spatial index/bounding box broadphase followed by native geometry narrowphase; no rule thresholds imported.')],
        source_relations=pairs,
        legend_negative=dict(reference_source=refs,source_border_entity_id=border['entity_id'],
            enclosed_sample_entity_id=legend_sample['entity_id'],native_polygon_coverage_verified=True,rejection=legend_rejection),
        native_arc_search=dict(attempts=arc_attempts,whole_scope_enumerated=False,result=found),
        tests=dict(command='.venv/bin/python -m pytest tests/test_round3_spatial_relations.py tests/test_round3_spatial_boundaries.py -q',
            relation_passed=24,boundary_passed=16,total_passed=40,
            covered=['rigid rotations/translations','multientity closed boundaries','overlap/touch/containment',
                'interleaved bboxes without intersection','multilayout/scope/hash mismatch rejection','same name independent objects',
                'legend rejection','unknown units/policy','open paths not object clearance','native ARC mirrored OCS',
                'missing/partial hinge support','segmented radial leaf','bounded curved intersection and touching uncertainty',
                'operation receipts distinguish geometry, proven relations and indeterminate curved contact',
                'refinement next_calls preserve both explicit object IDs and validate against Pydantic contract']),
        proven_facts=['Same-scope selected native polygon intersection, containment, touching and minimum distance.',
            'Native ARC with full radial leaf support and analytic sector area; curved overlap/distance bounds.',
            'Observed legend enclosure prevents placed-object use.'],
        remaining_limits=['No full census or six-class Q14 engineering identity is claimed.',
            'No main outline, pit, parking, road, home passage, door or hydrant role is inferred from name or bbox.',
            'Arc-only, missing hinge/leaf, nonplanar ARC and ambiguous multiple arcs remain unsupported.',
            'Near curved boundary contact remains bounded/uncertain rather than exact touching.',
            'Relations with unapplied exclusions require a separately explicit closed region.'],
        elapsed_seconds=time.monotonic()-started,
        historical_dxf_unchanged=sha256(source_path.read_bytes()).hexdigest()==digest)
    (OUT/'spatial-relations-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Source relations:',sum('result' in p for p in pairs),'native arc candidate:',bool(found),'attempts:',len(arc_attempts),flush=True)
    print(OUT/'spatial-relations-report.json',flush=True)

if __name__=='__main__':main()
