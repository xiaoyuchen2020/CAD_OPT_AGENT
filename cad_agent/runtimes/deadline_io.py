"""Bound synchronous HTTP I/O by the caller's absolute deadline and cancellation.

Only I/O runs in the daemon worker. Parsing, usage and session mutations stay on
the caller, so a late network return cannot publish output after cancellation.
"""
import queue
import socket
import threading


_END = object()


class DeadlineIO:
    POLL_SECONDS = 0.05

    def __init__(self, remaining, diagnostic):
        self.remaining = remaining
        self.diagnostic = diagnostic
        self.jobs = queue.Queue()
        self.stopped = threading.Event()
        self.contexts = []
        self.response = None
        self.thread = threading.Thread(target=self._work, name="responses-deadline-io", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def _work(self):
        try:
            while not self.stopped.is_set():
                job = self.jobs.get()
                if job is None or self.stopped.is_set():
                    break
                operation, result = job
                try:
                    result.put((True, operation()))
                except BaseException as error:
                    result.put((False, error))
        finally:
            for context in reversed(self.contexts):
                try:
                    context.__exit__(None, None, None)
                except Exception:
                    pass  # Cleanup must never expose server text or mask the cause.

    def call(self, operation, phase):
        self.remaining()
        self.diagnostic['io_phase'] = phase
        result = queue.Queue(maxsize=1)
        self.jobs.put((operation, result))
        while True:
            left = self.remaining()
            try:
                ok, value = result.get(timeout=min(left, self.POLL_SECONDS))
            except queue.Empty:
                continue
            self.remaining()  # A response arriving after the deadline is not accepted.
            if ok:
                return value
            raise value

    def enter(self, context, phase, response=False):
        def operation():
            value = context.__enter__()
            self.contexts.append(context)
            if response:
                self.response = value
            return value
        return self.call(operation, phase)

    def iterate(self, iterator, phase):
        iterator = iter(iterator)
        while True:
            value = self.call(lambda: next(iterator, _END), phase)
            if value is _END:
                return
            yield value

    def __exit__(self, error_type, error, traceback):
        self.stopped.set()
        if error is not None:
            code = getattr(error, 'code', None)
            self.diagnostic['termination_reason'] = (
                'absolute_deadline' if code == 'AGENT_TIME_LIMIT' else
                'cancelled' if type(error).__name__ == 'ExecutionCancelled' else 'request_failed')
            # httpcore exposes the live socket through network_stream. shutdown,
            # unlike close alone on another thread, wakes a blocked socket read.
            response = self.response
            network = response.extensions.get('network_stream') if response is not None else None
            self.diagnostic['socket_shutdown'] = False
            try:
                sock = network.get_extra_info('socket') if network is not None else None
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
                    self.diagnostic['socket_shutdown'] = True
            except Exception:
                pass
        self.jobs.put(None)
        # Bound cleanup too: a custom transport, DNS or pending response headers
        # may not expose a cancellable socket. Do not claim it has stopped.
        self.thread.join(timeout=0.1)
        self.diagnostic['io_worker_exit_confirmed'] = not self.thread.is_alive()
        self.diagnostic['io_cleanup_pending'] = self.thread.is_alive()
        return False


class DeadlineResponse:
    """Minimal response facade used by the existing bounded failure inspector."""
    def __init__(self, response, io):
        self.response, self.io = response, io

    def iter_bytes(self, chunk_size):
        return self.io.iterate(self.response.iter_bytes(chunk_size=chunk_size), 'failure_body')
