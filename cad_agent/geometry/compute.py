"""Bounded pure geometry operations; workers receive scoped data, never a Store.

Only registered static operations are accepted. No code, import path or callable is an
input. One reusable spawned process runs at a time; deadlines terminate timed-out work so
it cannot publish a late result or occupy the worker indefinitely.
"""
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .contracts import RoomMeasure, RoomDiscover
from .rooms import analyze_room
from .discovery import discover_enclosures

OPERATIONS = frozenset({'room', 'discover', 'geometry_measure', 'spatial_query',
    'discovery_anchors', 'boundary_candidates', 'contour_area', 'tiled_discovery',
    'component_refine', 'component_relations', 'component_openings',
    'connection_evidence', 'enclosure_inspection', 'source_geometry', 'visual_candidates',
    'source_descriptors', 'source_dimensions', 'source_viewports', 'source_bundle', 'source_text_metadata',
    'similarity_search'})


def dispatch(operation, payload):
    if operation == 'similarity_search':
        from cad_agent.analysis.similarity_search import compute_similarity_search
        return compute_similarity_search(payload)
    if operation == 'source_bundle':
        from .source import compute_source_bundle
        return compute_source_bundle(payload)
    if operation=='source_text_metadata':
        from .source import compute_text_metadata
        return compute_text_metadata(payload)
    if operation in {'source_descriptors','source_dimensions','source_viewports'}:
        from .source import compute_source_metadata
        return compute_source_metadata(operation,payload)
    if operation == 'source_geometry':
        from .source import compute_source_geometry
        return compute_source_geometry(payload)
    if operation == 'visual_candidates':
        from .operations import visual_candidates
        return visual_candidates(payload)
    if operation in {'geometry_measure', 'spatial_query', 'discovery_anchors'}:
        from .operations import measure, spatial_query, discovery_anchors
        return {'geometry_measure':measure,'spatial_query':spatial_query,
                'discovery_anchors':discovery_anchors}[operation](payload)
    if operation == 'boundary_candidates':
        from .room_boundary import compute_boundary_candidates
        return compute_boundary_candidates(payload)
    if operation == 'contour_area':
        from .contours import compute_contour_area
        return compute_contour_area(payload)
    if operation == 'tiled_discovery':
        from .tiled_discovery import compute_subdivision
        return compute_subdivision(payload)
    if operation in {'component_refine','component_relations','component_openings'}:
        from cad_agent.components.compute import compute_refinement, compute_relations, compute_openings
        return {'component_refine':compute_refinement,'component_relations':compute_relations,
                'component_openings':compute_openings}[operation](payload)
    if operation == 'connection_evidence':
        from .connection_compute import compute_connection_evidence
        return compute_connection_evidence(payload)
    if operation == 'enclosure_inspection':
        from cad_agent.analysis.enclosure_inspection import compute_inspection
        return compute_inspection(payload)
    if operation == 'room':
        args=RoomMeasure.model_validate(payload['args'])
        return analyze_room(payload['records'],payload['seed'],payload['factor'],args,
                            payload.get('frame_bbox'),payload.get('other_seeds',[]))
    if operation == 'discover':
        args=RoomDiscover.model_validate(payload['args'])
        return discover_enclosures(payload['records'],payload['bounds'],payload['factor'],args)
    raise ValueError('Unsupported isolated geometry operation')


def _worker(connection):
    try:
        while True:
            operation,payload=connection.recv()
            try:
                connection.send({'ok':True,'pid':os.getpid(),'result':dispatch(operation,payload)})
            except Exception as exc:
                connection.send({'ok':False,'pid':os.getpid(),'error':str(exc),'type':type(exc).__name__})
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


class LocalGeometryExecutor:
    """Explicit in-process adapter for minimal offline/evaluation workspaces."""
    def compute(self, operation, payload, timeout=None):
        return dispatch(operation,payload)


class GeometryExecutor:
    def __init__(self, capacity=3, timeout=120):
        if capacity<1 or timeout<=0: raise ValueError('Invalid geometry executor limits')
        self.capacity,self.timeout=capacity,timeout
        self._slots=threading.BoundedSemaphore(capacity)
        self._serial=threading.Lock()
        self._state=threading.Lock()
        self._context=multiprocessing.get_context('spawn')
        self._process=None
        self._connection=None
        self._closed=False
        self.last_timing=None
        self.pending=0
        self.last_worker_pid=None

    def _retire(self, process, connection):
        if process is not None and process.pid is not None:
            if process.is_alive(): process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill();process.join(timeout=2)
        if connection is not None: connection.close()
        with self._state:
            if self._process is process:
                self._process=None
                self._connection=None

    def compute(self, operation, payload, timeout=None):
        if operation not in OPERATIONS: raise ValueError('Unsupported isolated geometry operation')
        duration=self.timeout if timeout is None else timeout
        if duration<=0: raise ValueError('Invalid geometry timeout')
        if not self._slots.acquire(blocking=False): raise ValueError('几何计算队列已满，请稍后重试。')
        started=time.monotonic()
        deadline=started+duration
        acquired=False
        process=connection=None
        succeeded=False
        cold=False
        watchdog=None
        timed_out=threading.Event()
        def expire(worker):
            # Target this request's process only. Never look up a replacement.
            timed_out.set()
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=.2)
                if worker.is_alive(): worker.kill()
        with self._state: self.pending+=1
        try:
            acquired=self._serial.acquire(timeout=max(0,deadline-time.monotonic()))
            if not acquired: raise ValueError('几何计算等待超时，未生成测量结果。')
            queue_seconds=time.monotonic()-started
            with self._state:
                if self._closed: raise ValueError('几何计算执行器已关闭。')
                if self._process is None:
                    parent,child=self._context.Pipe()
                    connection=parent
                    process=self._context.Process(target=_worker,args=(child,),name='cad-geometry')
                    try:
                        process.start()
                    finally:
                        child.close()
                    self._process,self._connection=process,parent
                    cold=True
                process,connection=self._process,self._connection
                self.last_worker_pid=process.pid
            # poll() only promises some bytes; send()/recv() can both block on
            # pipe capacity or a partial frame. Killing the peer unblocks them.
            watchdog=threading.Timer(max(0,deadline-time.monotonic()),expire,args=(process,))
            watchdog.daemon=True
            watchdog.start()
            connection.send((operation,payload))
            if not connection.poll(max(0,deadline-time.monotonic())):
                raise ValueError('几何计算超时，未生成测量结果；请缩小范围。')
            message=connection.recv()
            if not message['ok']:
                raise ValueError('几何计算失败：'+message['error'])
            if timed_out.is_set() or time.monotonic()>deadline:
                raise ValueError('几何计算超时，未生成测量结果；请缩小范围。')
            succeeded=True
            self.last_timing=dict(cold_start=cold,elapsed_seconds=time.monotonic()-started,
                                  queue_seconds=queue_seconds,worker_pid=process.pid)
            return message['result']
        except (EOFError, OSError):
            if timed_out.is_set():
                raise ValueError('几何计算超时，未生成测量结果；请缩小范围。') from None
            raise ValueError('几何计算进程中断，未生成测量结果。') from None
        finally:
            # Join even after cancellation: a callback already firing must be
            # gone before releasing _serial or allowing another request.
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join()
            late_timeout=timed_out.is_set() and succeeded
            if timed_out.is_set(): succeeded=False
            if acquired and not succeeded:
                self._retire(process,connection)
            with self._state: self.pending-=1
            if acquired:self._serial.release()
            self._slots.release()
            if late_timeout:
                raise ValueError('几何计算超时，未生成测量结果；请缩小范围。')

    def close(self):
        with self._state:
            self._closed=True
            process,connection=self._process,self._connection
        self._retire(process,connection)


_REQUEST_REMAINING = ContextVar('geometry_request_remaining', default=None)


@contextmanager
def remaining_budget(remaining):
    """Request-local deadline; never mutate the shared executor's timeout."""
    token = _REQUEST_REMAINING.set(remaining)
    try:
        yield
    finally:
        _REQUEST_REMAINING.reset(token)


def compute(workspace, operation, payload, *, timeout=None):
    executor=getattr(workspace,'geometry_executor',None)
    if executor is None:
        executor=LocalGeometryExecutor()
    remaining = _REQUEST_REMAINING.get()
    if remaining is None:
        return executor.compute(operation,payload) if timeout is None else executor.compute(operation,payload,timeout=timeout)
    left = remaining()-5
    if left <= 0:
        raise ValueError('几何计算剩余时间不足；未启动新步骤。')
    result = executor.compute(operation,payload,timeout=min(getattr(executor,'timeout',120),left,
                                                          timeout if timeout is not None else float('inf')))
    remaining()
    return result
