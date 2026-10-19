"""Actual component service paths cross the fixed pure worker boundary."""
from copy import deepcopy
import json
import os
import pytest

from cad_agent.components.contracts import RelationQuery
from cad_agent.components.compute import compute_refinement, compute_relations, compute_openings
from cad_agent.components.openings import arc_sources, swing_symbols, swing_symbols_from_sources
from cad_agent.geometry.source import native_source, scoped_geometry_index
from tests.test_components import ingest
from tests.test_opening_relations import fixture

FUNCTIONS={'component_refine':compute_refinement,'component_relations':compute_relations,'component_openings':compute_openings}


def forbid_parent_native_index(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('parent rebuilt full native geometry index')
    monkeypatch.setattr('cad_agent.geometry.source.native_source',forbidden)
    monkeypatch.setattr('cad_agent.geometry.source._native_source',forbidden)


def test_component_service_uses_worker_and_matches_pure_local_results(workspace,monkeypatch):
    service,drawing,lid=ingest(workspace,fixture,units=6)
    forbid_parent_native_index(monkeypatch)
    executor=workspace.geometry_executor
    original=executor.compute
    calls=[]
    def observed(operation,payload,**kwargs):
        # A JSON round-trip is also an explicit check that native DXF/source,
        # Workspace/Store objects and clients cannot cross this boundary.
        if operation in FUNCTIONS:
            json.dumps(payload,allow_nan=False)
            local=FUNCTIONS[operation](deepcopy(payload))
        result=original(operation,payload,**kwargs)
        if operation in FUNCTIONS:
            assert result==local
            assert executor.last_worker_pid!=os.getpid()
            calls.append(operation)
        return result
    monkeypatch.setattr(executor,'compute',observed)
    result=service.build('relations',RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind='floor_plan'))
    assert calls==list(FUNCTIONS)
    assert len(result['rooms'])==2
    assert result['openings'][0]['passage_status']=='supported_door_between_two_spaces'
    assert workspace.store.get('component',result['record_id'])==json.loads(json.dumps(result))


@pytest.mark.parametrize('operation',list(FUNCTIONS))
def test_failed_component_compute_never_publishes_component_record(workspace,monkeypatch,operation):
    service,drawing,lid=ingest(workspace,fixture,units=6)
    executor=workspace.geometry_executor
    original=executor.compute
    def interrupted(name,payload,**kwargs):
        if name==operation:raise ValueError('geometry timeout at '+operation)
        return original(name,payload,**kwargs)
    monkeypatch.setattr(executor,'compute',interrupted)
    with pytest.raises(ValueError,match='geometry timeout'):
        service.build('relations',RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind='floor_plan'))
    assert workspace.store.count('component')==0


def test_detached_native_arc_inputs_preserve_symbol_geometry(workspace):
    service,drawing,lid=ingest(workspace,fixture,units=6)
    source=native_source(workspace,drawing['id'])
    records=scoped_geometry_index(workspace,drawing['id'],lid,[-1,-1,14,6],'m').query(lid,[-1,-1,14,6])
    arcs=arc_sources(records,source)
    detached=json.loads(json.dumps(arcs))
    assert swing_symbols_from_sources(records,detached,1)==swing_symbols(records,source,1)
    assert swing_symbols_from_sources(records,detached,1)


def connection_fixture(doc):
    from tests.test_components import connected_rooms
    connected_rooms(doc)
    for entity in doc.modelspace():entity.dxf.layer='A-WALL'
    doc.modelspace().add_arc((6.1,2),1,270,360,dxfattribs={'layer':'A-DOOR'})
    doc.modelspace().add_line((6.1,2),(7.1,2),dxfattribs={'layer':'A-DOOR'})
    for name,x in [('左室',2),('右室',9)]:
        doc.modelspace().add_text(name,dxfattribs={'insert':(x,1.4),'height':.15})


def test_connection_prepare_uses_detached_worker_evidence(workspace,monkeypatch):
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.geometry.connection_review import ConnectionReviewService,PrepareConnections
    from cad_agent.geometry.connection_compute import compute_connection_evidence
    service,drawing,lid=ingest(workspace,connection_fixture,units=6)
    reading=DrawingIndex(workspace,drawing['id'])
    ids=[next(r.region_id for r in reading.scope(lid,'') if text in r.text) for text in ['左室','右室']]
    args=PrepareConnections(layout_id=lid,pairs=[dict(source_region_id=ids[0],target_region_id=ids[1])])
    monkeypatch.setattr('cad_agent.geometry.connection_review.sheets',lambda *args:[])
    forbid_parent_native_index(monkeypatch)
    original=workspace.geometry_executor.compute
    calls=[]
    def observed(operation,payload,**kwargs):
        if operation=='connection_evidence':
            json.dumps(payload,allow_nan=False)
            local=compute_connection_evidence(deepcopy(payload))
        result=original(operation,payload,**kwargs)
        if operation=='connection_evidence':
            assert result==local
            assert workspace.geometry_executor.last_worker_pid!=os.getpid()
            calls.append(operation)
        return result
    monkeypatch.setattr(workspace.geometry_executor,'compute',observed)
    review=ConnectionReviewService(workspace,drawing['id']).prepare(args)
    assert calls and len(calls)<=2
    assert review['items'][0]['openings']
    assert workspace.store.get('measurement',review['measurement_id'])==json.loads(json.dumps(review))


def test_connection_compute_timeout_keeps_review_unpublished(workspace,monkeypatch):
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.geometry.connection_review import ConnectionReviewService,PrepareConnections
    _,drawing,lid=ingest(workspace,connection_fixture,units=6)
    reading=DrawingIndex(workspace,drawing['id'])
    ids=[next(r.region_id for r in reading.scope(lid,'') if text in r.text) for text in ['左室','右室']]
    args=PrepareConnections(layout_id=lid,pairs=[dict(source_region_id=ids[0],target_region_id=ids[1])])
    original=workspace.geometry_executor.compute
    def interrupted(operation,payload,**kwargs):
        if operation=='connection_evidence':raise ValueError('connection timeout')
        return original(operation,payload,**kwargs)
    monkeypatch.setattr(workspace.geometry_executor,'compute',interrupted)
    with pytest.raises(ValueError,match='connection timeout'):
        ConnectionReviewService(workspace,drawing['id']).prepare(args)
    assert workspace.store.count('measurement')==0


def test_dimensions_service_avoids_parent_native_index_and_preserves_output(workspace,monkeypatch):
    from cad_agent.components.dimensions import read_dimensions_from_source
    from cad_agent.components.contracts import DimensionQuery
    from tests.test_components import dimensions
    service,drawing,lid=ingest(workspace,dimensions)
    expected=read_dimensions_from_source(native_source(workspace,drawing['id']),drawing['id'],lid,None,'mm')
    forbid_parent_native_index(monkeypatch)
    result=service.build('dimensions',DimensionQuery(layout_id=lid))
    assert result['dimensions']==expected['dimensions']
    assert result['total_native_dimensions']==expected['total_native_dimensions']
    assert workspace.geometry_executor.last_worker_pid!=os.getpid()


def test_cached_connection_review_does_not_read_native_or_scene_again(workspace,monkeypatch):
    from cad_agent.contracts import stable_id
    from cad_agent.geometry.connection_review import ConnectionReviewService,PrepareConnections,VERSION
    _,drawing,lid=ingest(workspace,connection_fixture,units=6)
    args=PrepareConnections(layout_id=lid,pairs=[dict(source_region_id='one',target_region_id='two')])
    rid=stable_id('measure',drawing['id'],drawing['sha256'],VERSION,json.dumps(args.model_dump(),sort_keys=True))
    record=dict(measurement_id=rid,snapshot_id=drawing['id'],source_sha256=drawing['sha256'],kind='connection_review')
    workspace.store.put('measurement',rid,record)
    forbid_parent_native_index(monkeypatch)
    def forbidden(*args,**kwargs):raise AssertionError('cache hit rebuilt source context')
    monkeypatch.setattr('cad_agent.geometry.connection_review.DrawingIndex',forbidden)
    monkeypatch.setattr(workspace.geometry_executor,'compute',forbidden)
    assert ConnectionReviewService(workspace,drawing['id']).prepare(args)==record
