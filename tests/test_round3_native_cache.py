"""Native-index retention: configured memory budget, oversize, LRU and stamps."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import ezdxf
import pytest
from cad_agent.cache import BoundedCache
from cad_agent.geometry import source


def drawing(path, x=1):
    doc=ezdxf.new();doc.modelspace().add_line((0,0),(x,2));doc.saveas(path)
    return path


def spy_reader(monkeypatch):
    read=source.ezdxf.readfile;calls=[]
    def counted(path):
        calls.append(str(path));return read(path)
    monkeypatch.setattr(source.ezdxf,'readfile',counted)
    return calls


def test_default_and_explicit_limits_are_shared_with_local_workspace(monkeypatch,tmp_path):
    monkeypatch.delenv('CAD_NATIVE_GEOMETRY_CACHE_BYTES',raising=False)
    monkeypatch.delenv('CAD_NATIVE_GEOMETRY_CACHE_ENTRIES',raising=False)
    assert source.native_cache_options()==dict(max_bytes=512*1024*1024,max_entries=4)
    path=drawing(tmp_path/'one.dxf')
    monkeypatch.setenv('CAD_NATIVE_GEOMETRY_CACHE_BYTES',str(path.stat().st_size*12))
    monkeypatch.setenv('CAD_NATIVE_GEOMETRY_CACHE_ENTRIES','1')
    workspace=SimpleNamespace(lock=Lock(),store=SimpleNamespace(artifact=lambda *a:path))
    first=source.native_source(workspace,'sid')
    assert source.native_source(workspace,'sid') is first
    assert workspace._native_geometry.max_bytes==path.stat().st_size*12
    assert workspace._native_geometry.max_entries==1


@pytest.mark.parametrize('name,value',[('CAD_NATIVE_GEOMETRY_CACHE_BYTES','-1'),('CAD_NATIVE_GEOMETRY_CACHE_ENTRIES','no'),('CAD_NATIVE_GEOMETRY_CACHE_BYTES','1.5')])
def test_invalid_config_is_not_silently_unbounded(monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError,match=name):source.native_cache_options()


def test_oversize_index_is_computed_without_lowering_weight_or_retaining(monkeypatch,tmp_path):
    path=drawing(tmp_path/'one.dxf');weight=path.stat().st_size*12
    calls=spy_reader(monkeypatch);cache=BoundedCache(max_entries=4,max_bytes=weight-1)
    a=source._native_source(path,'sid',cache);b=source._native_source(path,'sid',cache)
    assert a['entities'] and a is not b
    assert len(calls)==2 and cache.bytes==0 and not cache._items


def test_lru_evicts_under_memory_budget_and_rebuilds_exact_source(monkeypatch,tmp_path):
    paths=[drawing(tmp_path/f'{i}.dxf',i+1) for i in range(3)]
    budget=sum(p.stat().st_size*12 for p in paths[:2])
    calls=spy_reader(monkeypatch);cache=BoundedCache(max_entries=2,max_bytes=budget)
    a=source._native_source(paths[0],'s0',cache)
    source._native_source(paths[1],'s1',cache)
    assert source._native_source(paths[0],'s0',cache) is a  # refresh A; B is LRU
    source._native_source(paths[2],'s2',cache)
    assert cache.bytes<=budget and len(cache._items)<=2
    source._native_source(paths[1],'s1',cache)
    assert calls.count(str(paths[0]))==1 and calls.count(str(paths[1]))==2


def test_changed_file_stamp_and_snapshot_do_not_reuse_old_index(monkeypatch,tmp_path):
    path=drawing(tmp_path/'one.dxf');calls=spy_reader(monkeypatch)
    cache=BoundedCache(max_entries=4,max_bytes=1024*1024)
    first=source._native_source(path,'sid',cache)
    doc=ezdxf.readfile(path);doc.modelspace().add_line((10,10),(20,20));doc.saveas(path)
    before=len(calls);second=source._native_source(path,'sid',cache)
    assert len(calls)==before+1 and len(second['entities'])==len(first['entities'])+1
    third=source._native_source(path,'other-snapshot',cache)
    assert not set(second['entities'])&set(third['entities'])


def test_same_key_concurrent_native_reads_use_singleflight(monkeypatch,tmp_path):
    path=drawing(tmp_path/'one.dxf');read=source.ezdxf.readfile
    entered=Event();release=Event();barrier=Barrier(5);calls=[]
    def held(path):
        calls.append(str(path));entered.set();assert release.wait(5);return read(path)
    monkeypatch.setattr(source.ezdxf,'readfile',held)
    cache=BoundedCache(max_entries=2,max_bytes=1024*1024)
    def get():
        barrier.wait();return source._native_source(path,'sid',cache)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(get) for _ in range(4)];barrier.wait()
        assert entered.wait(5);release.set();items=[f.result() for f in futures]
    assert len(calls)==1 and all(x is items[0] for x in items)


def test_zero_budget_explicitly_disables_retention(monkeypatch):
    monkeypatch.setenv('CAD_NATIVE_GEOMETRY_CACHE_BYTES','0')
    assert source.native_cache_options()['max_bytes']==0
