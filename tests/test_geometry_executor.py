"""IPC deadline regressions: blocked writes and incomplete framed responses."""
import importlib
import os
import struct
import time
import pytest

module=importlib.import_module('cad_agent.geometry.compute')


def no_reader(connection):
    time.sleep(30)


def partial_response(connection):
    connection.recv()
    os.write(connection.fileno(),struct.pack('!i',1000000)+b'x')
    time.sleep(30)


def request():
    return dict(records=[],seed=[1,1],factor=1,args=dict(layout_id='m',seed=[1,1]))


@pytest.mark.parametrize('worker,large_request',[(no_reader,True),(partial_response,False)])
def test_watchdog_unblocks_stuck_ipc_and_does_not_kill_replacement(monkeypatch,worker,large_request):
    executor=module.GeometryExecutor()
    original=module._worker
    monkeypatch.setattr(module,'_worker',worker)
    data=request()
    if large_request:data['padding']=b'x'*(8*1024*1024)
    started=time.monotonic()
    try:
        with pytest.raises(ValueError,match='超时'):
            executor.compute('room',data,timeout=.5)
        assert time.monotonic()-started<3
        retired_pid=executor.last_worker_pid
        assert executor._process is None and executor.pending==0
        monkeypatch.setattr(module,'_worker',original)
        assert executor.compute('room',request())['status']=='unresolved'
        replacement=executor.last_worker_pid
        assert replacement!=retired_pid
        time.sleep(.6)
        executor.compute('room',request())
        assert executor.last_worker_pid==replacement
    finally:executor.close()


def test_process_start_failure_closes_both_pipe_ends_and_releases_capacity():
    executor=module.GeometryExecutor(capacity=1)
    original=executor._context
    handles=[]
    class FailingContext:
        def Pipe(self):
            pair=original.Pipe();handles.extend(pair);return pair
        def Process(self,**kwargs):
            process=original.Process(**kwargs)
            def fail():raise OSError('injected spawn failure')
            process.start=fail
            return process
    executor._context=FailingContext()
    try:
        with pytest.raises(ValueError,match='中断'):executor.compute('room',request())
        assert handles and all(c.closed for c in handles)
        assert executor.pending==0 and executor._process is None
        executor._context=original
        assert executor.compute('room',request())['status']=='unresolved'
    finally:executor.close()
