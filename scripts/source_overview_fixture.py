"""Synthetic wire-format fixtures, never a production workflow writer.

Only the isolated preview and tests use this module. No evaluator/model imports.
"""
import json
import sqlite3
from pathlib import Path
from uuid import uuid4
from cad_agent.exploration.contracts import digest


class FixtureWorkflow:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root/'rule_workflow.sqlite3'
        if self.path.exists():
            raise ValueError('Fixture refuses to open an existing workflow database')
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE objects(id TEXT, project TEXT, kind TEXT, data TEXT);
                CREATE TABLE workflows(id TEXT, project TEXT, data TEXT);''')

    def put_object(self, project, kind, data, ident):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?)', (ident,project,kind,json.dumps(data)))

    def create(self, project, key, inputs):
        run = dict(run_id='fixture_'+uuid4().hex, project_id=project,
                   status='waiting_input', inputs=inputs, result={'gaps':['合成示例：适用范围待确认']})
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO workflows VALUES(?,?,?)', (run['run_id'],project,json.dumps(run)))
        return run


def fixture_package(archive, project):
    drawings = {d['id']:d for d in archive['artifacts']['drawings']}
    atoms = {e['evidence_id']:dict(e,source_sha256=drawings[e['snapshot_id']]['sha256'])
             for e in archive['result']['citations']}
    package = dict(project_id=project, atoms=atoms, assertions=archive['result']['claims'],
                   origin={k:archive[k] for k in ('job_id','attempt_id')})
    return dict(package, package_id='fixture_'+digest(package))
