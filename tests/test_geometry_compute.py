import os
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from cad_agent.geometry.compute import GeometryExecutor, LocalGeometryExecutor
from cad_agent.geometry.contracts import RoomMeasure, RoomDiscover
from cad_agent.geometry.service import GeometryService


def records():
    result=[]
    for x,y,w,h in [(0,0,6,4),(-.2,-.2,6.4,4.4)]:
        points=[[x,y],[x+w,y],[x+w,y+h],[x,y+h],[x,y]]
        for a,b in zip(points,points[1:]):
            result.append(dict(entity_id=str(len(result)),layer='walls',linear=True,status='ready',paths=[[a,b]]))
    return result


def payload(operation):
    if operation=='room':
        args=RoomMeasure(layout_id='model',seed=[1,1],radius_m=10)
        return dict(records=records(),seed=[1,1],factor=1,args=args.model_dump(),frame_bbox=None,other_seeds=[])
    args=RoomDiscover(layout_id='model',bbox=[-1,-1,7,5],view_kind='floor_plan')
    return dict(records=records(),bounds=args.bbox,factor=1,args=args.model_dump())


def test_worker_reuse_and_local_adapter_are_numerically_identical():
    executor=GeometryExecutor()
    try:
        for operation in ('room','discover'):
            data=payload(operation)
            expected=LocalGeometryExecutor().compute(operation,data)
            assert executor.compute(operation,data)==expected
            assert executor.last_worker_pid!=os.getpid()
            if operation=='room':
                pid=executor.last_worker_pid
                assert executor.last_timing['cold_start'] is True
            else:
                assert executor.last_worker_pid==pid
                assert executor.last_timing['cold_start'] is False
        assert executor.pending==0
    finally: executor.close()
    assert executor._process is None


def test_timeout_and_worker_error_retire_process_and_next_call_recovers():
    executor=GeometryExecutor()
    try:
        with pytest.raises(ValueError,match='超时'):
            executor.compute('room',payload('room'),timeout=.000001)
        assert executor._process is None
        executor.compute('room',payload('room'))
        previous=executor.last_worker_pid
        with pytest.raises(ValueError,match='失败'):
            executor.compute('room',{'args':{}})
        assert executor._process is None
        executor.compute('room',payload('room'))
        assert executor.last_worker_pid!=previous
        with pytest.raises(ValueError,match='Unsupported'):
            executor.compute('os.system',{})
    finally: executor.close()


def test_queue_is_bounded_while_worker_slot_is_busy():
    executor=GeometryExecutor(capacity=1)
    executor._serial.acquire()
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(executor.compute,'room',payload('room'),.1)
        try:
            import time
            deadline=time.monotonic()+2
            while executor.pending!=1 and time.monotonic()<deadline:time.sleep(.001)
            with pytest.raises(ValueError,match='队列已满'):
                executor.compute('room',payload('room'))
            with pytest.raises(ValueError,match='等待超时'):future.result(timeout=2)
        finally: executor._serial.release();executor.close()


def test_geometry_timeout_does_not_publish_measurement(workspace,demo_bytes):
    drawing=workspace.ingest('demo.dxf',demo_bytes)
    service=GeometryService(workspace,drawing['id'])
    args=RoomDiscover(layout_id=drawing['layouts'][0]['layout_id'],bbox=[0,0,1000,1000],view_kind='floor_plan',unit_override='mm')
    before=workspace.store.count('measurement')
    workspace.geometry_executor.timeout=.000001
    with pytest.raises(ValueError,match='超时'):service.discover(args)
    assert workspace.store.count('measurement')==before
    assert workspace.geometry_executor._process is None
