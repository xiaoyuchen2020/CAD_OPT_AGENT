"""Bounded phase timings; wall time is distinct from overlapping stage totals."""
from collections import defaultdict
from contextlib import contextmanager
import threading
import time


class PhaseMetrics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.lock = threading.RLock()
        self.phases = defaultdict(lambda: dict(calls=0, failures=0, seconds=0.0, max_seconds=0.0))

    @contextmanager
    def measure(self, phase):
        tick = self.clock()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            elapsed = max(0.0, self.clock() - tick)
            with self.lock:
                row = self.phases[phase]
                row['calls'] += 1
                row['failures'] += int(failed)
                row['seconds'] += elapsed
                row['max_seconds'] = max(row['max_seconds'], elapsed)

    def returned_error(self, phase):
        """Count an explicit tool error packet, which does not raise Python errors."""
        with self.lock:
            self.phases[phase]['failures'] += 1

    def snapshot(self):
        with self.lock:
            return dict(version='phase-metrics-v1', wall_seconds=round(max(0,self.clock()-self.started),6),
                        phases={key:{k:round(v,6) if isinstance(v,float) else v for k,v in row.items()}
                                for key,row in sorted(self.phases.items())},
                        note='阶段可能嵌套；不可将所有阶段相加作为端到端耗时。外部运行时总耗时不等于纯模型等待。')
