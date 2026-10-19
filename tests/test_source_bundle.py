"""Request reuse remains effective when the native source cannot fit in cache."""
import os
import pytest
from cad_agent.cache import BoundedCache
from cad_agent.components.contracts import RelationQuery
from cad_agent.geometry.compute import LocalGeometryExecutor
from cad_agent.geometry import source
from tests.test_components import ingest
from tests.test_opening_relations import fixture


@pytest.mark.parametrize('mode,reference_reads',[('auto',5),('all',1)])
def test_relations_one_native_build_matches_five_build_reference(workspace,monkeypatch,mode,reference_reads):
    service,drawing,lid=ingest(workspace,fixture,units=6)
    monkeypatch.setattr(workspace,'geometry_executor',LocalGeometryExecutor())
    # A zero admission budget simulates a source larger than the unchanged 128 MiB budget.
    cache=BoundedCache(max_bytes=0)
    monkeypatch.setattr(source,'_WORKER_NATIVE',cache)
    read=source.ezdxf.readfile;reads=[]
    def counted(*args,**kwargs):
        reads.append(str(args[0]));return read(*args,**kwargs)
    monkeypatch.setattr(source.ezdxf,'readfile',counted)
    args=RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind='floor_plan',linework_mode=mode)
    bundled=service.build('relations',args)
    assert len(reads)==1
    assert cache.bytes==0
    original_get=workspace.store.get
    def uncached(kind,key):
        if kind in ('component','measurement'):raise KeyError(key)
        return original_get(kind,key)
    monkeypatch.setattr(workspace.store,'get',uncached)
    monkeypatch.setattr(source,'ScopedSourceBundle',lambda *args:None)
    reads.clear()
    reference=service.build('relations',args)
    assert len(reads)==reference_reads
    assert bundled==reference


def test_bundle_is_request_scoped_validates_scope_and_source(workspace):
    _,drawing,lid=ingest(workspace,fixture,units=6)
    bundle=source.ScopedSourceBundle(workspace,drawing['id'])
    data=bundle.get(lid,[-1,-1,14,6],'m')
    assert data is bundle.get(lid,[-1,-1,14,6],'m')
    assert workspace.geometry_executor.last_worker_pid!=os.getpid()
    with pytest.raises(ValueError,match='范围不匹配'):
        bundle.get(lid,[-1,-1,15,6],'m')
    path=workspace.store.artifact(drawing['id'],'source.dxf')
    with path.open('a') as stream:stream.write('\n')
    with pytest.raises(ValueError,match='来源文件已变化'):
        bundle.get(lid,[-1,-1,14,6],'m')


def test_failed_bundle_does_not_publish_measurements_or_component(workspace,monkeypatch):
    service,_,lid=ingest(workspace,fixture,units=6)
    real=workspace.geometry_executor.compute
    def fail(operation,payload,**kwargs):
        if operation=='source_bundle':raise ValueError('bundle deadline')
        return real(operation,payload,**kwargs)
    monkeypatch.setattr(workspace.geometry_executor,'compute',fail)
    with pytest.raises(ValueError,match='bundle deadline'):
        service.build('relations',RelationQuery(layout_id=lid,bbox=[-1,-1,14,6]))
    assert workspace.store.count('measurement')==0
    assert workspace.store.count('component')==0
