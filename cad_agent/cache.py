"""Bounded derived-data LRU with per-key single flight (builders run unlocked)."""
from collections import OrderedDict
from concurrent.futures import Future
from threading import RLock


class BoundedCache:
    def __init__(self, max_entries=4, max_bytes=128 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._lock = RLock()
        self._items, self._pending = OrderedDict(), {}
        self.bytes = 0

    def get_or_build(self, key, build, weight=lambda value: 1):
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key][0]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result()
        try:
            value = build()
            size = max(1, int(weight(value)))
            with self._lock:
                if size <= self.max_bytes and self.max_entries > 0:
                    self._items[key] = (value, size)
                    self.bytes += size
                    while len(self._items) > self.max_entries or self.bytes > self.max_bytes:
                        _, (_, removed) = self._items.popitem(last=False)
                        self.bytes -= removed
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)


def workspace_cache(workspace, name, **kwargs):
    # Only publication takes the workspace lock, never parsing/building/IO.
    with workspace.lock:
        shared = getattr(workspace, '_derived_caches', None)
        if shared is None:
            shared = workspace._derived_caches = {}
        cache = shared.get(name, getattr(workspace, name, None))
        if not isinstance(cache, BoundedCache):
            cache = BoundedCache(**kwargs)
        shared[name] = cache
        setattr(workspace, name, cache)
        return cache


def file_stamp(path):
    stat = path.stat()
    return (str(path.resolve()), stat.st_ino, stat.st_mtime_ns, stat.st_size)
