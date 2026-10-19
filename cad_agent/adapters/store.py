"""Atomic JSON artifacts and a small SQLite metadata index, local-only."""

import json
from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS records_kind ON records(kind, created)"
            )
            for field in self.FILTERS:
                c.execute(f"CREATE INDEX IF NOT EXISTS records_{field} ON records(kind, json_extract(data, '$.{field}'), created DESC)")

            c.execute("CREATE INDEX IF NOT EXISTS records_status_lease ON records(kind, json_extract(data, '$.status'), json_extract(data, '$.execution_lease_until'))")

    # JSON remains the source of truth. Expression indexes migrate existing rows
    # without rewriting evidence or resetting tasks owned by another process.
    FILTERS = ("status", "snapshot_id", "cache_key", "session_id", "sha256", "owner_id", "lease_owner", "cache_id", "original_sha256", "parser_version", "execution_owner", "execution_token", "execution_lease_until")

    def connect(self):
        return sqlite3.connect(self.root / "index.sqlite3", timeout=20)

    def _write(self, c, kind, key, data):
        c.execute(
            "INSERT INTO records(id,kind,data,created) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data WHERE records.kind=excluded.kind",
            (key, kind, json.dumps(data, ensure_ascii=False), data.get("created_at", now())),
        )
        if c.execute("SELECT kind FROM records WHERE id=?", (key,)).fetchone()[0] != kind:
            raise ValueError("Record id already belongs to another kind")

    def put(self, kind: str, key: str, data: dict):
        with self.connect() as c:
            self._write(c, kind, key, data)

    def get(self, kind: str, key: str):
        with self.connect() as c:
            row = c.execute("SELECT data FROM records WHERE id=? AND kind=?", (key, kind)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def update(self, kind, key, mutator, expected=None):
        """Atomically read/modify/write; return None on comparison failure.

        The callback runs inside a short SQLite write transaction: it must not
        perform IO, model calls, or nested Store writes. Missing keys raise KeyError.
        """
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
            if row is None:
                raise KeyError(key)
            data = json.loads(row[0])
            if any(data.get(k) != v for k, v in (expected or {}).items()):
                return None
            result = mutator(data)
            if result is None:
                return None
            self._write(c, kind, key, result)
            return result

    def compare_and_swap(self, kind, key, expected, updates):
        return self.update(kind, key, lambda data: data | updates, expected)

    def _where(self, kind, filters):
        clauses, values = ["kind=?"], [kind]
        for field, value in (filters or {}).items():
            if field not in self.FILTERS:
                raise ValueError("Unsupported record filter: " + field)
            expr = "json_extract(data, '$." + field + "')"
            if isinstance(value, (list, tuple, set)):
                clauses.append(expr + " IN (" + ",".join("?" for _ in value) + ")")
                values.extend(value)
            elif value is None:
                clauses.append(expr + " IS NULL")
            else:
                clauses.append(expr + "=?")
                values.append(value)
        return " AND ".join(clauses), values

    def list(self, kind: str, *, filters=None, limit=None, offset=0):
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("Pagination must be nonnegative")
        where, values = self._where(kind, filters)
        with self.connect() as c:
            rows = c.execute("SELECT data FROM records WHERE " + where +
                             " ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
                             [*values, -1 if limit is None else limit, offset]).fetchall()
        return [json.loads(x[0]) for x in rows]

    def count(self, kind: str, *, filters=None):
        where, values = self._where(kind, filters)
        with self.connect() as c:
            return c.execute("SELECT count(*) FROM records WHERE " + where, values).fetchone()[0]

    def artifact_dir(self, snapshot_id):
        # IDs are generated by the application, never interpreted as user paths.
        if not snapshot_id.startswith("snap_") or not snapshot_id[5:].isalnum():
            raise ValueError("Invalid snapshot id")
        path = self.root / "drawings" / snapshot_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact(self, snapshot_id, filename):
        if filename not in (
            "source.dxf",
            "source.dwg",
            "oda.json",
            "scene.json",
            "geometry.json",
            "render.json",
            "regions.json",
            "entities.json",
        ):
            raise ValueError("Invalid artifact")
        return self.artifact_dir(snapshot_id) / filename

    def write_json(self, path: Path, value):
        temporary = path.with_suffix("." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
