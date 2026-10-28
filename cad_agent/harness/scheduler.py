"""Bounded pure computation pool; ordered results, no session/Store in workers.

The static operation registry is deliberately separate from model tools. Queued
work is cancelled on failure, running pure work can finish but cannot publish.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_check = ContextVar('cad_compute_check', default=lambda: None)


@contextmanager
def compute_scope(check):
    token = _check.set(check)
    try:
        yield
    finally:
        _check.reset(token)


def _dispatch(operation, payload):
    if operation == 'measure_pair':
        from cad_agent.geometry.local_measurement import measure_pair
        return measure_pair(payload['a'], payload['b'], payload['basis'])
    raise ValueError('COMPUTE_OPERATION_DENIED')


def ordered_compute(operation, payloads, *, workers=2, check=None):
    if operation not in {'measure_pair'}:
        raise ValueError('COMPUTE_OPERATION_DENIED')
    if type(workers) is not int or not 1 <= workers <= 2:
        raise ValueError('COMPUTE_CONCURRENCY_LIMIT')
    check = check or _check.get()
    iterator = iter(payloads)
    # At most two detached payloads exist in the executor queue at any time.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='cad-pure')
    pending = []
    def submit():
        check()
        try:
            payload = next(iterator)
        except StopIteration:
            return False
        pending.append(pool.submit(_dispatch, operation, deepcopy(payload)))
        return True
    try:
        for _ in range(workers):
            if not submit(): break
        while pending:
            check()
            try:
                result = pending[0].result(timeout=.05)
            except TimeoutError:
                continue
            pending.pop(0)
            check()
            yield result
            submit()
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
