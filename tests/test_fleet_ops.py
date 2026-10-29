"""Operational invariants without stopping services or touching live credentials."""
from pathlib import Path
import io
import json
import sqlite3
import sys
import tarfile
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import fleet_backup as backup
import fleet_restore as restore


def test_active_jobs_sees_center_and_direct_agent_work(tmp_path):
    center=tmp_path/'cad-center';center.mkdir()
    with sqlite3.connect(center/'fleet.sqlite3') as db:
        db.execute('CREATE TABLE jobs(status TEXT)')
        db.executemany('INSERT INTO jobs VALUES(?)',[('queued',),('running',),('cancel_requested',),('completed',)])
    node=tmp_path/'cad-a';node.mkdir()
    with sqlite3.connect(node/'index.sqlite3') as db:
        db.execute('CREATE TABLE records(kind TEXT,data TEXT)')
        db.executemany('INSERT INTO records VALUES(?,?)',[
            ('chat_run',json.dumps({'status':'queued'})),('job',json.dumps({'status':'running'})),
            ('drawing',json.dumps({'status':'running'})),('chat_run',json.dumps({'status':'completed'}))])
    assert backup.active_jobs(tmp_path)=={'center':2,'cad-a':2}


def test_services_restart_in_finally_without_starting_previously_stopped_unit(monkeypatch):
    running=['cad-atlas-center','cad-atlas-worker-a','cad-atlas-agent-a']
    calls=[]
    monkeypatch.setattr(backup,'running_units',lambda:running)
    monkeypatch.setattr(backup,'active_jobs',lambda:{'center':0})
    def command(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[1]=='is-active' else 0)
    monkeypatch.setattr(backup,'command',command)
    with pytest.raises(RuntimeError,match='archive write failed'):
        with backup.stopped_fleet():
            raise RuntimeError('archive write failed')
    starts=[x[-1] for x in calls if x[1]=='start']
    assert starts==['cad-atlas-agent-a','cad-atlas-center','cad-atlas-worker-a']
    assert not any('cad-atlas-agent-b' in x for x in calls)
    assert not any('oda' in str(x) for x in calls)


def test_race_to_busy_reopens_center_without_stopping_analysis(monkeypatch):
    calls=[]
    monkeypatch.setattr(backup,'running_units',lambda:backup.UNITS)
    monkeypatch.setattr(backup,'active_jobs',lambda:{'center':1})
    def command(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[1]=='is-active' else 0)
    monkeypatch.setattr(backup,'command',command)
    with pytest.raises(RuntimeError,match='ACTIVE_JOBS'):
        with backup.stopped_fleet():
            pytest.fail('must not enter backup while busy')
    assert [x[-1] for x in calls if x[1]=='stop']==['cad-atlas-center']
    assert [x[-1] for x in calls if x[1]=='start']==['cad-atlas-center']


def tar_fixture(extra=None):
    raw=io.BytesIO()
    with tarfile.open(fileobj=raw,mode='w') as arc:
        values={'manifest.json':json.dumps({'schema_version':1,'state_path':str(backup.STATE),'config_path':str(backup.CONFIG)}).encode(),
            'state/cad-center/fleet.sqlite3':b'fixture','config/center.env':b'fixture',
            'config/a.env':b'fixture','config/b.env':b'fixture','release/pyproject.toml':b'fixture'}
        for name,data in values.items():
            info=tarfile.TarInfo(name);info.size=len(data);arc.addfile(info,io.BytesIO(data))
        if extra:
            arc.addfile(extra)
    raw.seek(0)
    return tarfile.open(fileobj=raw,mode='r')


def test_restore_accepts_complete_manifest_without_mutation():
    with tar_fixture() as arc:
        assert restore.validate_archive(arc)['schema_version']==1


@pytest.mark.parametrize('name,kind',[('../escape',tarfile.REGTYPE),('/etc/escape',tarfile.REGTYPE),
    ('state/symlink',tarfile.SYMTYPE),('state/device',tarfile.CHRTYPE),('config/a.env',tarfile.REGTYPE)])
def test_restore_rejects_unsafe_archive(name,kind):
    info=tarfile.TarInfo(name);info.type=kind;info.linkname='/etc'
    with tar_fixture(info) as arc,pytest.raises(ValueError):
        restore.validate_archive(arc)
