"""Storage migration, cross-instance atomicity, and derived-cache concurrency."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from cad_agent.adapters.store import Store
from cad_agent.cache import BoundedCache
from cad_agent.analysis import retrieval


def test_existing_database_migration_is_read_preserving(tmp_path):
    with sqlite3.connect(tmp_path / 'index.sqlite3') as conn:
        conn.execute('CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL)')
        for i in range(6):
            value = {'id': str(i), 'status': 'running' if i % 2 else 'done', 'snapshot_id': 'snap_a', 'created_at': str(i)}
            conn.execute('INSERT INTO records VALUES (?,?,?,?)', (str(i), 'job', json.dumps(value), str(i)))
    store = Store(tmp_path)
    assert store.count('job', filters={'status': 'running'}) == 3
    assert [v['id'] for v in store.list('job', filters={'status': 'running'}, limit=1, offset=1)] == ['3']
    assert store.count('job', filters={'status': ['running', 'done']}) == 6
    assert store.count('job', filters={'status': []}) == 0
    assert Store(tmp_path).get('job', '5')['status'] == 'running'
    with store.connect() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT data FROM records WHERE kind=? AND json_extract(data, '$.status')=?", ('job','running')).fetchall()
    assert any('records_status' in row[-1] for row in plan)
    with pytest.raises(ValueError):
        store.list('job', filters={'unsafe SQL': 1})


def test_cross_instance_update_does_not_lose_writes(tmp_path):
    stores = [Store(tmp_path), Store(tmp_path)]
    stores[0].put('counter', 'one', {'n': 0})
    def increment(i):
        return stores[i % 2].update('counter', 'one', lambda row: {'n': row['n'] + 1})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(80)))
    assert stores[0].get('counter', 'one')['n'] == 80
    assert stores[0].compare_and_swap('counter', 'one', {'n': 79}, {'n': 0}) is None
    assert stores[1].compare_and_swap('counter', 'one', {'n': 80}, {'n': 81}) == {'n': 81}
    with pytest.raises(ValueError):
        stores[0].put('different_kind', 'one', {'n': 999})
    assert stores[0].get('counter', 'one')['n'] == 81


def test_single_flight_and_unrelated_key_progress():
    cache = BoundedCache(max_entries=2, max_bytes=10)
    started, release = threading.Event(), threading.Event()
    calls = []
    def build():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return 'value'
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(cache.get_or_build, 'a', build, lambda _: 4)
        assert started.wait(2)
        second = pool.submit(cache.get_or_build, 'a', build, lambda _: 4)
        other = pool.submit(cache.get_or_build, 'b', lambda: 'other', lambda _: 4)
        assert other.result(timeout=2) == 'other'
        release.set()
        assert first.result() == second.result() == 'value'
    assert len(calls) == 1
    cache.get_or_build('oversize', lambda: 'large', lambda _: 100)
    assert cache.bytes <= 10
    assert 'oversize' not in cache._items
    cache.get_or_build('c', lambda: 'third', lambda _: 7)
    assert len(cache._items) == 1
    assert cache.bytes == 7


def test_failed_single_flight_can_retry():
    cache = BoundedCache()
    with pytest.raises(RuntimeError):
        cache.get_or_build('a', lambda: (_ for _ in ()).throw(RuntimeError('failed')))
    assert cache.get_or_build('a', lambda: 12) == 12


def test_lexical_index_reused_but_scope_frequencies_preserved(monkeypatch):
    monkeypatch.setattr(retrieval, '_LEXICAL', BoundedCache())
    items = [dict(chunk_id=str(i), index_text=text, text=text) for i, text in enumerate(['墙体材料 AAC', 'AAC AAC', '混凝土'])]
    original = retrieval.tokens
    calls = []
    def tracked(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(retrieval, 'tokens', tracked)
    first = retrieval.lexical_rank(items, 'AAC')
    assert len(calls) == 4
    assert retrieval.lexical_rank(items, 'AAC') == first
    assert len(calls) == 5
    subset = retrieval.lexical_rank(items[:1], 'AAC')
    assert subset[0][1] != first[0][1]
    # A replacement with equal IDs must invalidate token statistics.
    changed = [items[0] | {'index_text': 'zzz', 'text': 'zzz'}]
    assert retrieval.lexical_rank(changed, 'AAC') == []


def test_drawing_cache_keeps_multiple_snapshots_and_invalidates(workspace, demo_bytes):
    from cad_agent.analysis.general import DrawingIndex
    import ezdxf
    import io
    first = workspace.ingest('one.dxf', demo_bytes)['id']
    doc = ezdxf.new()
    doc.modelspace().add_text('另一张图纸')
    buffer = io.StringIO(); doc.write(buffer)
    second = workspace.ingest('two.dxf', buffer.getvalue().encode())['id']
    a = DrawingIndex(workspace, first)
    b = DrawingIndex(workspace, second)
    assert DrawingIndex(workspace, first).regions is a.regions
    assert DrawingIndex(workspace, second).regions is b.regions
    path = workspace.store.artifact(first, 'regions.json')
    rows = json.loads(path.read_text()); rows[0]['text'] = '缓存更新'
    workspace.store.write_json(path, rows)
    assert DrawingIndex(workspace, first).regions is not a.regions
    assert DrawingIndex(workspace, second).regions is b.regions


def test_native_geometry_cache_invalidates_same_snapshot_source(workspace, demo_bytes):
    from cad_agent.geometry.source import native_source
    import ezdxf
    sid = workspace.ingest('one.dxf', demo_bytes)['id']
    before = native_source(workspace, sid)
    assert native_source(workspace, sid) is before
    path = workspace.store.artifact(sid, 'source.dxf')
    doc = ezdxf.readfile(path)
    doc.modelspace().add_line((0, 0), (123, 456))
    doc.saveas(path)
    after = native_source(workspace, sid)
    assert after is not before
    assert len(after['entities']) == len(before['entities']) + 1


def test_model_views_share_caches_first_created_on_a_view(workspace, demo_bytes):
    from cad_agent.analysis.general import DrawingIndex
    sid = workspace.ingest('shared.dxf', demo_bytes)['id']
    workspace.model_catalog['cache-test-profile'] = workspace.settings
    first = workspace.for_model('cache-test-profile')
    second = workspace.for_model('cache-test-profile')
    assert first is not workspace and second is not first
    built = DrawingIndex(first, sid)
    assert DrawingIndex(second, sid).regions is built.regions
    assert DrawingIndex(workspace, sid).regions is built.regions
