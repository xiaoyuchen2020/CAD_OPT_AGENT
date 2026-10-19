"""Pure-stage parity in spawned workers; never opens production .data or a model."""
from copy import deepcopy
import json
import os
from pathlib import Path
import pytest
from cad_agent.geometry.compute import GeometryExecutor
from cad_agent.geometry.room_boundary import compute_boundary_candidates
from cad_agent.geometry.contours import compute_contour_area
from cad_agent.geometry.tiled_discovery import compute_subdivision
from cad_agent.geometry.contracts import RoomDiscover
from test_room_boundary import records, label


def boundary_payload():
    item=label()
    return dict(records=records(),item=item,labels=[item],scope=[-1,-1,11,11])


def area_payload():
    return dict(candidate={'geometry_model':{'outer_contour_id':'outer','interior_contour_ids':['inner'],'topological_hole_ids':['inner']}},
                library=[dict(contour_id='outer',boundary=[[0,0],[10,0],[10,10],[0,10],[0,0]]),
                         dict(contour_id='inner',boundary=[[2,2],[4,2],[4,4],[2,4],[2,2]])],
                factor=1,basis='topological_face',exclusions=[])


def tiled_payload(limited=False):
    args=RoomDiscover(layout_id='model',bbox=[-1,-1,11,11],view_kind='floor_plan',close_openings=False)
    rows=[dict(r,layout_id='model',kind='LWPOLYLINE') for r in records()]
    original=dict(measurement_id='baseline',applied_unit='m',scope_bbox=args.bbox,candidates=[],contours=[],complete=not limited,
                  diagnostics=['segment_limit_exceeded'] if limited else [],source_sha256='source')
    return dict(original=original,key='tiled',snapshot_id='snapshot',records=rows,arc_sources={},args=args.model_dump())


@pytest.mark.parametrize('operation,function,payload',[
    ('boundary_candidates',compute_boundary_candidates,boundary_payload),
    ('contour_area',compute_contour_area,area_payload),
    ('tiled_discovery',compute_subdivision,tiled_payload),
    ('tiled_discovery',compute_subdivision,lambda:tiled_payload(True)),
])
def test_spawned_stage_exact_parity_and_json_only(operation,function,payload):
    data=json.loads(json.dumps(payload()));before=deepcopy(data)
    expected=function(data)
    executor=GeometryExecutor(timeout=30)
    try:
        actual=executor.compute(operation,data)
        assert actual==expected
        assert executor.last_worker_pid!=os.getpid()
        json.dumps(actual)
        assert data==before
        if operation=='contour_area': assert actual['basis_area_m2']==96
        if operation=='boundary_candidates': assert all(c['area_m2'] is None for c in actual)
        if operation=='tiled_discovery' and data['original']['diagnostics']:
            assert actual['complete'] is False
            assert actual['subdivision']['full_scope_checked'] is False
            assert actual['subdivision']['attempted']
    finally:executor.close()


def test_real_frozen_kongpu_boundary_stage_parity():
    path=Path(__file__).resolve().parents[1]/'artifacts/dynamic-room-boundary-20261016/input.json'
    if not path.exists():pytest.skip('Optional frozen real-drawing evidence is absent.')
    bundle=json.loads(path.read_text())
    target=next(t for t in bundle['texts'] if t['entity_id']==bundle['target']['label_entity_id'])
    item=dict(region_id=target['evidence'][0]['region_id'],number=1,label_evidence=target['evidence'][0])
    rows=[dict(entity_id=s['entity_id'],linear=True,paths=[[s['a'],s['b']]],
               effective_layer=s['visibility']['effective_layer']) for s in bundle['segments']]
    data=dict(records=rows,item=item,labels=[item],scope=bundle['bbox'])
    expected=compute_boundary_candidates(data)
    # Frozen real region currently has no accepted hypothesis; isolation must
    # preserve this gap, never turn it into a claimed successful room boundary.
    assert len(rows)==584 and expected==[]
    executor=GeometryExecutor(timeout=30)
    try:
        actual=executor.compute('boundary_candidates',data)
        assert actual==expected
        assert executor.last_worker_pid!=os.getpid()
        known={r['entity_id'] for r in rows}
        for c in actual:
            assert set(c['source_entity_ids'])<=known
            assert c['area_m2'] is None
    finally:executor.close()


def test_boundary_and_tiled_parent_paths_never_load_native_source(workspace, monkeypatch):
    """Native entity expansion must remain in the spawned source-cache owner."""
    from cad_agent.geometry import source
    from cad_agent.geometry.room_boundary import RoomBoundaryService, PrepareBoundaries
    from cad_agent.geometry.tiled_discovery import discover_with_subdivision
    from test_room_batch import room_fixture
    from test_room_discovery import plan
    def forbidden(*args, **kwargs):
        pytest.fail('Parent process attempted native-source expansion.')
    monkeypatch.setattr(source,'native_source',forbidden)
    monkeypatch.setattr(source,'_native_source',forbidden)
    batches,args,drawing=room_fixture(workspace)
    batch=batches.run(args)
    review=RoomBoundaryService(workspace,drawing['id']).prepare(PrepareBoundaries(batch_id=batch['batch_id']))
    assert review['snapshot_id']==drawing['id'] and review['items']
    service,discovery_args,drawing=plan(workspace,['会议室'])
    result=discover_with_subdivision(service,discovery_args)
    assert result['snapshot_id']==drawing['id']
    assert workspace.geometry_executor.last_worker_pid!=os.getpid()
