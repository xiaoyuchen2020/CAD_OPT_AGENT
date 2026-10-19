import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from cad_agent.adapters.store import Store
from cad_agent.execution import ExecutionManager, ExecutionContext, ExecutionCancelled, BoundedExecutor


def task():
    return dict(task_id='task_test',status='partial',revision=0,run_ids=[],subtasks=[])


def run(key='run_test'):
    return dict(id=key,status='queued')


def test_live_owner_survives_second_workspace_and_expired_owner_recovers(tmp_path):
    store=Store(tmp_path); manager=ExecutionManager(store,lease_seconds=.3)
    data=task(); record=run(); manager.claim('chat_run',record,data)
    other=ExecutionManager(Store(tmp_path),lease_seconds=.3)
    try:
        time.sleep(.4)
        assert store.get('chat_run',record['id'])['status']=='queued'
        manager.close()
        time.sleep(.4)
        other.recover()
        assert store.get('chat_run',record['id'])['status']=='failed'
        assert store.get('agent_task',data['task_id'])['status']=='partial'
        with pytest.raises(ExecutionCancelled):
            manager.update('chat_run',record['id'],record['execution_token'],{'status':'completed'})
    finally:
        manager.close();other.close()


def test_concurrent_resume_only_one_claim(tmp_path):
    store=Store(tmp_path);manager=ExecutionManager(store)
    store.put('agent_task','task_test',task())
    def claim(i):
        try:
            manager.claim('chat_run',run(str(i)),task());return True
        except ValueError:return False
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sum(pool.map(claim,range(2)))==1
        assert len(store.list('chat_run'))==1
    finally: manager.close()


def test_cancel_checkpoint_receipt_and_idempotent_cancel(tmp_path):
    manager=ExecutionManager(Store(tmp_path));record=run()
    try:
        manager.claim('chat_run',record,task())
        ctx=ExecutionContext(manager,'chat_run',record['id'],record['execution_token'])
        checkpoint=ctx.before_tool('measure',{'id':'a'})
        manager.cancel(record['id']);manager.cancel(record['id'])
        manager.update('chat_run',record['id'],record['execution_token'],{'stage':'工具已完成'})
        with pytest.raises(ExecutionCancelled):ctx.after_tool(checkpoint)
        saved=manager.store.get('chat_run',record['id'])
        assert saved['tool_checkpoint']['status']=='completed'
        with pytest.raises(ExecutionCancelled):ctx.before_tool('measure',{'id':'a'})
    finally: manager.close()


def test_bounded_queue_releases_slot_after_completion():
    pool=BoundedExecutor(max_workers=1,capacity=1);gate=threading.Event()
    try:
        future=pool.submit(gate.wait)
        with pytest.raises(ValueError):pool.submit(lambda:None)
        gate.set();future.result(timeout=2)
        assert pool.submit(lambda:42).result(timeout=2)==42
    finally:gate.set();pool.shutdown(wait=True)


def test_parser_worker_is_separate_spawned_process(workspace):
    import os
    assert workspace.parse_executor.submit(os.getpid).result(timeout=15) != os.getpid()


def test_task_ledger_stale_owner_cannot_overwrite_recovery(tmp_path):
    from types import SimpleNamespace
    from cad_agent.analysis.tasks import TaskLedger
    manager=ExecutionManager(Store(tmp_path))
    data=task();record=run()
    try:
        manager.claim('chat_run',record,data)
        ledger=TaskLedger.__new__(TaskLedger)
        ledger.w=SimpleNamespace(store=manager.store,execution=manager)
        ledger.data=data
        manager.store.compare_and_swap('agent_task',data['task_id'],{}, {'execution_lease_until':0})
        manager.recover()
        with pytest.raises(ExecutionCancelled):ledger.save()
        assert manager.store.get('agent_task',data['task_id'])['status']=='partial'
    finally:manager.close()


def test_finalised_but_unpublished_task_recovers_after_owner_expires(tmp_path):
    manager=ExecutionManager(Store(tmp_path))
    data=task();record=run()
    try:
        manager.claim('chat_run',record,data)
        manager.store.compare_and_swap('agent_task',data['task_id'],{},
            {'status':'completed','publication_status':'not_published','execution_lease_until':0})
        manager.finish('chat_run',record['id'],record['execution_token'],data)
        manager.recover()
        restored=manager.store.get('agent_task',data['task_id'])
        assert restored['status']=='partial'
        assert restored['execution_token'] is None
    finally:manager.close()


def test_job_cancel_uses_same_cooperative_fence(tmp_path):
    manager=ExecutionManager(Store(tmp_path));record=run('job_test')
    try:
        manager.claim('job',record)
        manager.cancel(record['id'],kind='job')
        with pytest.raises(ExecutionCancelled):
            manager.update('job',record['id'],record['execution_token'],{'status':'completed'})
        assert manager.update('job',record['id'],record['execution_token'],{'status':'cancelled'})['status']=='cancelled'
    finally:manager.close()


def test_parse_wait_does_not_hold_workspace_lock(workspace, demo_bytes, monkeypatch):
    entered=threading.Event(); release=threading.Event()
    original=workspace.parse_executor.submit
    class Delayed:
        def __init__(self,future): self.future=future
        def result(self):
            entered.set()
            assert release.wait(10)
            return self.future.result(timeout=10)
    monkeypatch.setattr(workspace.parse_executor,'submit',lambda *a,**kw:Delayed(original(*a,**kw)))
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(workspace.ingest,'demo.dxf',demo_bytes)
        try:
            assert entered.wait(5)
            acquired=workspace.lock.acquire(timeout=.2)
            assert acquired
            workspace.lock.release()
        finally: release.set()
        assert future.result(timeout=15)['id'].startswith('snap_')
