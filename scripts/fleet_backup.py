#!/usr/bin/env python3
"""Consistent root-only backup of the single-host CAD fleet; ODA is untouched."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tarfile

STATE = Path('/var/lib/cad-atlas')
CONFIG = Path('/etc/cad-atlas')
CURRENT = Path('/opt/cad-atlas/current')
UNITS = ['cad-atlas-center', 'cad-atlas-worker-a', 'cad-atlas-worker-b',
         'cad-atlas-agent-a', 'cad-atlas-agent-b']
START_ORDER = ['cad-atlas-agent-a', 'cad-atlas-agent-b', 'cad-atlas-center',
               'cad-atlas-worker-a', 'cad-atlas-worker-b']


def command(*args, check=True):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, timeout=180)


def require_root():
    if os.geteuid() != 0:
        raise SystemExit('Run as root: backup and restore contain service credentials.')


def active_jobs(state=STATE):
    """Read databases without opening models or exposing prompt/result content."""
    counts = {}
    center = state/'cad-center/fleet.sqlite3'
    if center.is_file():
        with sqlite3.connect(center.as_uri()+'?mode=ro', uri=True, timeout=10) as db:
            counts['center'] = db.execute("SELECT count(*) FROM jobs WHERE status IN ('running','cancel_requested')").fetchone()[0]
    for node in ('cad-a','cad-b'):
        path = state/node/'index.sqlite3'
        if path.is_file():
            with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10) as db:
                counts[node] = db.execute("SELECT count(*) FROM records WHERE kind IN ('chat_run','job') AND json_extract(data,'$.status') IN ('queued','running')").fetchone()[0]
    return counts


def running_units():
    return [unit for unit in UNITS if command('systemctl','is-active','--quiet',unit,check=False).returncode == 0]


@contextmanager
def maintenance_lock():
    path = Path('/run/lock/cad-atlas-maintenance.lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another fleet maintenance operation is running.')
        yield


@contextmanager
def stopped_fleet(allow_interrupt=False):
    """Stop entry point first; finally restart only units this operation stopped."""
    was_running = running_units()
    stopped = []
    try:
        for unit in UNITS:
            if unit not in was_running:
                continue
            # Once center has stopped, no browser or worker can submit/claim anew.
            if unit != 'cad-atlas-center' and not allow_interrupt and any(active_jobs().values()):
                raise RuntimeError('ACTIVE_JOBS: fleet became busy before shutdown; retry later.')
            stopped.append(unit)
            command('systemctl','stop',unit)
            if command('systemctl','is-active','--quiet',unit,check=False).returncode == 0:
                raise RuntimeError('Could not stop '+unit)
        yield was_running
    finally:
        failures = []
        for unit in START_ORDER:
            if unit in stopped:
                try:
                    command('systemctl','start',unit)
                except Exception:
                    failures.append(unit)
        if failures:
            raise RuntimeError('Service restart failed: '+', '.join(failures))


def safe_member(info):
    # Symlinks could capture data outside the known state/config/release trees.
    if not (info.isfile() or info.isdir()):
        raise ValueError('Unsupported non-regular backup member: '+info.name)
    return info


def make_backup(destination, allow_interrupt=False):
    destination = Path(destination)
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    if destination.is_symlink() or destination.stat().st_uid != 0:
        raise ValueError('Backup directory must be a root-owned real directory.')
    os.chmod(destination,0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    final = destination/f'cad-atlas-{stamp}.tar.gz'
    temporary = final.with_suffix('.incomplete')
    with stopped_fleet(allow_interrupt) as units:
        source = CURRENT.resolve(strict=True)
        manifest = dict(schema_version=1,created_at=datetime.now(timezone.utc).isoformat(),
            host=socket.gethostname(),state_path=str(STATE),config_path=str(CONFIG),
            release_path=str(source),release_name=source.name,previously_active_units=units,
            credentials_included=True,venv_included=False,oda_included=False)
        if not STATE.is_dir() or not CONFIG.is_dir():
            raise ValueError('Fleet state/config directory is missing.')
        try:
            with temporary.open('xb') as raw:
                os.fchmod(raw.fileno(),0o600)
                with tarfile.open(fileobj=raw,mode='w:gz') as archive:
                    import io
                    payload = json.dumps(manifest,indent=2).encode()
                    info = tarfile.TarInfo('manifest.json')
                    info.size=len(payload);info.mode=0o600
                    archive.addfile(info,io.BytesIO(payload))
                    archive.add(STATE,arcname='state',filter=safe_member)
                    archive.add(CONFIG,arcname='config',filter=safe_member)
                    archive.add(source,arcname='release',filter=safe_member)
                raw.flush();os.fsync(raw.fileno())
            os.replace(temporary,final)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return final


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',default='/var/backups/cad-atlas')
    parser.add_argument('--allow-interrupt',action='store_true',help='Explicitly permit interrupting active analyses (manual maintenance only).')
    parser.add_argument('--check',action='store_true',help='Read-only readiness check; do not stop services.')
    args=parser.parse_args()
    require_root();os.umask(0o077)
    with maintenance_lock():
        active=active_jobs()
        if args.check:
            print(json.dumps({'status':'busy' if any(active.values()) else 'ready','active':active}))
            return
        if any(active.values()) and not args.allow_interrupt:
            print(json.dumps({'status':'deferred','reason':'active_jobs','active':active}))
            return
        try:
            archive=make_backup(args.destination,args.allow_interrupt)
        except RuntimeError as exc:
            if str(exc).startswith('ACTIVE_JOBS:'):
                print(json.dumps({'status':'deferred','reason':'fleet_became_busy'}))
                return
            raise
        print(json.dumps({'status':'completed','archive':str(archive),'bytes':archive.stat().st_size}))


if __name__=='__main__':
    main()
