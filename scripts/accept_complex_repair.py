"""Frozen-code acceptance of the original seven real-model scenarios."""
import fcntl
import regress_complex_repair as suite
suite.OUT=suite.ROOT/'artifacts/complex-repair-acceptance-20261016'
if __name__=='__main__':
    suite.OUT.mkdir(parents=True,exist_ok=True)
    with (suite.OUT/'runner.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        suite.main()
