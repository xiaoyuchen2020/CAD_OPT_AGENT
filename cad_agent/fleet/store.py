"""Durable SQLite queue with atomic claims and fenced execution attempts."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re
import secrets
import sqlite3
import time
from uuid import uuid4


class Conflict(ValueError):
    pass


def iso(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class FleetStore:
    def __init__(self, root, lease_seconds=60, max_attempts=3):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "uploads").mkdir(exist_ok=True, mode=0o700)
        (self.root / "archives").mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "fleet.sqlite3"
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        with self.connection() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL, node_id TEXT, attempt_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, progress TEXT, result TEXT, error TEXT, delivery_status TEXT, snapshot_id TEXT, run_id TEXT);
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, error TEXT, archive_path TEXT);
            CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, last_seen REAL NOT NULL, job_id TEXT, progress TEXT);
            CREATE TABLE IF NOT EXISTS node_metadata(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_controls(job_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_commands(job_id TEXT NOT NULL,command_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(job_id,command_id));
            CREATE TABLE IF NOT EXISTS operation_attempt_telemetry(job_id TEXT NOT NULL,attempt_id TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(job_id,attempt_id));
            CREATE TABLE IF NOT EXISTS operation_telemetry(job_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS job_keys(key TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status,created_at);
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def add_upload(self, filename, content):
        filename = Path(filename.replace("\\", "/")).name
        if Path(filename).suffix.lower() not in (".dwg", ".dxf") or not content:
            raise ValueError("仅支持非空 DWG / DXF 图纸")
        if len(content) > 32 * 1024 * 1024:
            raise ValueError("图纸超过 32 MB 上传上限")
        filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)
        suffix = Path(filename).suffix
        filename = filename[:-len(suffix)][:220] + suffix
        ident = uuid4().hex
        path = self.root / "uploads" / ident
        with path.open("xb") as file:
            file.write(content)
            file.flush()
            import os
            os.fsync(file.fileno())
        value = dict(id=ident, filename=filename, sha256=sha256(content).hexdigest(), size_bytes=len(content), created_at=iso())
        with self.connection(True) as db:
            db.execute("INSERT INTO uploads VALUES(:id,:filename,:sha256,:size_bytes,:created_at)", value)
        return self.upload(ident)

    def upload(self, ident, db=None):
        if db is None:
            with self.connection() as connection:
                return self.upload(ident, connection)
        row = db.execute("SELECT * FROM uploads WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError(ident)
        return dict(row) | {"url": f"/api/fleet/uploads/{ident}/file"}

    def create_job(self, payload, idempotency_key=None):
        if not payload.get("upload_ids") or len(set(payload["upload_ids"])) != len(payload["upload_ids"]):
            raise ValueError("请选择图纸且不要重复选择")
        ident, now = uuid4().hex, iso()
        signature = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 240):
            raise ValueError("无效的任务幂等标识")
        with self.connection(True) as db:
            if idempotency_key:
                old = db.execute("SELECT job_id,payload_sha256 FROM job_keys WHERE key=?", (idempotency_key,)).fetchone()
                if old:
                    if old['payload_sha256'] != signature:
                        raise Conflict('同一任务幂等标识不能绑定不同输入')
                    return self._job(db, old['job_id'])
            for item in payload["upload_ids"]:
                self.upload(item, db)
            db.execute("INSERT INTO jobs(id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?)", (ident, "queued", dump(payload), now, now))
            if idempotency_key:
                db.execute("INSERT INTO job_keys VALUES(?,?,?)", (idempotency_key, ident, signature))
            return self._job(db, ident)

    def job_for_key(self, key):
        with self.connection() as db:
            row = db.execute('SELECT job_id FROM job_keys WHERE key=?', (key,)).fetchone()
            return self._job(db, row['job_id']) if row else None

    def _job(self, db, ident, *, summary=False):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError(ident)
        value = dict(row)
        value.update(json.loads(value.pop("payload")))
        if summary:
            # The polling list needs status, not every model trace and source
            # record. Full results remain available on detail and archive URLs.
            value['result'] = None
        for field in ("progress", "result", "error"):
            value[field] = json.loads(value[field]) if value[field] is not None else None
        value["job_id"] = ident
        value["lease_expires_at"] = iso(value["lease_until"]) if value["lease_until"] else None
        value["uploads"] = [self.upload(x, db) for x in value["upload_ids"]]
        archive = db.execute("SELECT archive_path FROM attempts WHERE id=?", (value["attempt_id"],)).fetchone()
        value["archive_url"] = f"/api/fleet/jobs/{ident}/archive" if archive and archive[0] else None
        value["node_url"] = f'/nodes/{value["node_id"]}/' if value["node_id"] else None
        return value

    def job(self, ident):
        with self.connection(True) as db:
            self._expire(db)
            return self._job(db, ident)

    def jobs(self):
        with self.connection(True) as db:
            self._expire(db)
            return [self._job(db, r[0], summary=True) for r in db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()]

    def _expire(self, db):
        now = time.time()
        for row in db.execute("SELECT * FROM jobs WHERE status IN ('running','cancel_requested') AND lease_until < ?", (now,)).fetchall():
            status = "cancelled" if row["status"] == "cancel_requested" else ("queued" if row["attempts"] < self.max_attempts else "failed")
            error = dump({"code": "LEASE_EXPIRED", "message": "执行节点失联；旧执行编号已失效"})
            db.execute("UPDATE attempts SET status='expired',ended_at=?,error=? WHERE id=?", (iso(now), error, row["attempt_id"]))
            db.execute("UPDATE jobs SET status=?,lease_until=NULL,updated_at=?,error=? WHERE id=?", (status, iso(now), error, row["id"]))
            db.execute("UPDATE nodes SET job_id=NULL WHERE id=? AND job_id=?", (row["node_id"], row["id"]))

    def claim(self, node_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node_id):
            raise ValueError("Invalid node ID")
        with self.connection(True) as db:
            self._expire(db)
            db.execute("INSERT INTO nodes(id,last_seen) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen", (node_id, time.time()))
            active = db.execute("SELECT id FROM jobs WHERE node_id=? AND status IN ('running','cancel_requested')", (node_id,)).fetchone()
            # Returning the current lease makes worker restart reconciliation possible.
            if active:
                return self._job(db, active[0])
            candidates = db.execute("SELECT id,payload FROM jobs WHERE status='queued' ORDER BY created_at").fetchall()
            def admissible(row):
                payload=json.loads(row['payload'])
                if payload.get('preferred_node') not in (None,'',node_id):return False
                control=db.execute('SELECT data FROM operation_controls WHERE job_id=?',(row['id'],)).fetchone()
                if control and json.loads(control[0]).get('desired_state')=='paused':return False
                if payload.get('exploration_run_id') and getattr(self,'exploration',None):
                    run=self.exploration.get_run(payload['exploration_run_id'])
                    if run.get('cancel_requested') or run.get('control',{}).get('desired_state')=='paused':return False
                return True
            row = next((r for r in candidates if admissible(r)), None)
            if not row:
                return None
            ident, attempt, now = row["id"], uuid4().hex, iso()
            db.execute("UPDATE jobs SET status='running',node_id=?,attempt_id=?,attempts=attempts+1,lease_until=?,updated_at=?,error=NULL,progress=NULL,snapshot_id=NULL,run_id=NULL,result=NULL,delivery_status=NULL WHERE id=?", (node_id, attempt, time.time()+self.lease_seconds, now, ident))
            db.execute("INSERT INTO attempts(id,job_id,node_id,status,started_at) VALUES(?,?,?,'running',?)", (attempt, ident, node_id, now))
            db.execute("UPDATE nodes SET job_id=? WHERE id=?", (ident, node_id))
            return self._job(db, ident)

    def _fence(self, db, job_id, node_id, attempt_id):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        if row["node_id"] != node_id or row["attempt_id"] != attempt_id or row["status"] not in ("running", "cancel_requested") or row["lease_until"] < time.time():
            raise Conflict("执行租约已失效，旧结果不能覆盖当前任务")
        return row

    def heartbeat(self, job_id, node_id, attempt_id, progress=None, snapshot_id=None, run_id=None):
        with self.connection(True) as db:
            row = self._fence(db, job_id, node_id, attempt_id)
            until = time.time() + self.lease_seconds
            db.execute("UPDATE jobs SET lease_until=?,updated_at=?,progress=COALESCE(?,progress),snapshot_id=COALESCE(?,snapshot_id),run_id=COALESCE(?,run_id) WHERE id=?", (until, iso(), dump(progress) if progress is not None else None, snapshot_id, run_id, job_id))
            db.execute("UPDATE nodes SET last_seen=?,progress=? WHERE id=?", (time.time(), dump(progress), node_id))
            return {"cancel_requested": row["status"] == "cancel_requested", "lease_expires_at": iso(until), "status": row["status"]}

    def finish(self, job_id, node_id, attempt_id, *, result=None, artifacts=None, error=None, retryable=False, cancelled=False, delivery_status=None):
        # Hold the transaction until archive is durable: result is never published before its evidence.
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            previous = db.execute("SELECT ended_at FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row and row["attempt_id"] == attempt_id and row["node_id"] == node_id and previous and previous[0]:
                return self._job(db, job_id)  # Acknowledged success/failure is immutable, including pending retries.
            row = self._fence(db, job_id, node_id, attempt_id)
            state = "cancelled" if cancelled or row["status"] == "cancel_requested" else ("queued" if error is not None and retryable and row["attempts"] < self.max_attempts else "failed" if error is not None else "completed")
            attempt_state = "failed" if state == "queued" else state
            delivery_status = (delivery_status or "unknown") if state == "completed" else attempt_state
            archive_path = None
            if result is not None or artifacts is not None:
                archive = {"schema_version": "1", "job_id": job_id, "attempt_id": attempt_id, "node_id": node_id, "created_at": iso(), "task": json.loads(row["payload"]), "result": result, "artifacts": artifacts, "execution_status": attempt_state, "job_status": state, "delivery_status": delivery_status, "error": error}
                data = dump(archive).encode()
                if len(data) > 64 * 1024 * 1024:
                    raise ValueError("结果归档超过 64 MB")
                archive_path = f"archives/{job_id}-{attempt_id}.json"
                path = self.root / archive_path
                temporary = path.with_suffix(".tmp")
                with temporary.open("wb") as file:
                    file.write(data)
                    file.flush()
                    import os
                    os.fsync(file.fileno())
                temporary.replace(path)
            now = iso()
            db.execute("UPDATE attempts SET status=?,ended_at=?,error=?,archive_path=? WHERE id=?", (attempt_state, now, dump(error) if error is not None else None, archive_path, attempt_id))
            db.execute("UPDATE jobs SET status=?,lease_until=NULL,updated_at=?,result=?,error=?,delivery_status=? WHERE id=?", (state, now, dump(result) if result is not None else None, dump(error) if error is not None else None, delivery_status, job_id))
            db.execute("UPDATE nodes SET last_seen=?,job_id=NULL WHERE id=? AND job_id=?", (time.time(), node_id, job_id))
            if state in {'completed','failed','cancelled'}:
                for command in db.execute('SELECT command_id,data FROM operation_commands WHERE job_id=?',(job_id,)).fetchall():
                    value=json.loads(command['data'])
                    if value['status']=='pending':
                        value.update(status='rejected',reason='execution_ended_before_control_applied',rejected_at=now)
                        db.execute('UPDATE operation_commands SET data=? WHERE job_id=? AND command_id=?',(dump(value),job_id,command['command_id']))
            return self._job(db, job_id)

    def cancel(self, ident):
        with self.connection(True) as db:
            self._expire(db)
            row = self._job(db, ident)
            state = "cancelled" if row["status"] == "queued" else "cancel_requested" if row["status"] == "running" else row["status"]
            db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (state, iso(), ident))
            return self._job(db, ident)

    def retry(self, ident):
        with self.connection(True) as db:
            row = self._job(db, ident)
            if row["status"] not in ("failed", "cancelled", "completed"):
                raise Conflict("仅已结束任务可重新执行")
            # Explicit user retry is a new job: preserve the old result and its evidence.
            payload = {k: row.get(k) for k in ("upload_ids", "question", "template_id", "template_version", "mode", "preferred_node")}
            # Explicit retry retains the user's original authorization, never
            # reinterprets a template or generated subtask as user permission.
            for key in ('original_user_input', 'dynamic_methods', 'authorization'):
                if key in row:
                    payload[key] = row[key]
            payload["retry_of"] = ident
            new_id, now = uuid4().hex, iso()
            db.execute("INSERT INTO jobs(id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?)", (new_id, "queued", dump(payload), now, now))
            return self._job(db, new_id)

    def nodes(self):
        with self.connection(True) as db:
            self._expire(db)
            values = []
            for row in db.execute("SELECT * FROM nodes ORDER BY id"):
                value = dict(row)
                value["status"] = "offline" if time.time()-value["last_seen"] > self.lease_seconds else "busy" if value["job_id"] else "idle"
                value["last_seen"] = iso(value["last_seen"])
                value["progress"] = json.loads(value["progress"]) if value["progress"] else None
                metadata = db.execute("SELECT data FROM node_metadata WHERE id=?", (value["id"],)).fetchone()
                if metadata:
                    value.update(json.loads(metadata[0]))
                value["agent_url"] = f'/nodes/{value["id"]}/'
                values.append(value)
            return values

    def node_metadata(self, ident, metadata):
        with self.connection(True) as db:
            db.execute("INSERT INTO node_metadata(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (ident, dump(metadata)))

    def attempts(self, ident):
        with self.connection() as db:
            self._job(db, ident)
            values = []
            for row in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY started_at", (ident,)):
                value = dict(row)
                value["error"] = json.loads(value["error"]) if value["error"] else None
                value["archive_url"] = f"/api/fleet/jobs/{ident}/attempts/{value['id']}/archive" if value.pop("archive_path") else None
                values.append(value)
            return values

    def archive(self, ident, attempt_id=None):
        with self.connection() as db:
            if attempt_id:
                row = db.execute("SELECT archive_path FROM attempts WHERE job_id=? AND id=?", (ident, attempt_id)).fetchone()
            else:
                row = db.execute("SELECT a.archive_path FROM attempts a JOIN jobs j ON a.id=j.attempt_id WHERE j.id=?", (ident,)).fetchone()
            if not row or not row[0]:
                raise KeyError(ident)
            return self.root / row[0]

    def session(self):
        token = secrets.token_urlsafe(40)
        with self.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?)", (sha256(token.encode()).hexdigest(), time.time()+12*3600))
        return token

    def authenticated(self, token):
        if not token:
            return False
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?", (sha256(token.encode()).hexdigest(), time.time())).fetchone())

    def logout(self, token):
        with self.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (sha256(token.encode()).hexdigest(),))
