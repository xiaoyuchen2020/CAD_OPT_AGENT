"""Independent SQLite storage for immutable rulebook sources and effective sets."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .parser import MAX_SOURCE_BYTES, RuleLibraryError, digest, extract_rules, open_archive, parse_xlsx


BUILTIN_KEY = "architecture-cn"
BUILTIN_PATH = Path(__file__).parent / "builtin" / "architecture-cn.xlsx"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 512:
        raise RuleLibraryError("invalid_project", "A non-empty project_id of at most 512 characters is required")
    return project_id


class RuleLibraryStore:
    """Sources never change in place. Imports and snapshots are content-addressed.

    Project authorization belongs to the caller; this store additionally prevents
    a project lookup from resolving another project's library or snapshot.
    """

    def __init__(self, root: str | Path, *, seed_builtin: bool = True, max_source_bytes: int = MAX_SOURCE_BYTES):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "rule_libraries.sqlite3"
        self.max_source_bytes = max_source_bytes
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rule_libraries (
                    library_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    project_id TEXT NOT NULL, library_key TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    document TEXT NOT NULL, source BLOB NOT NULL,
                    UNIQUE(scope, project_id, library_key, source_sha256)
                );
                CREATE TABLE IF NOT EXISTS effective_rule_sets (
                    effective_set_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS rule_libraries_project ON rule_libraries(project_id);
                CREATE INDEX IF NOT EXISTS effective_rule_sets_project ON effective_rule_sets(project_id);
            """)
        if seed_builtin:
            self.seed_builtin()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed_builtin(self) -> dict:
        if not BUILTIN_PATH.is_file():
            raise RuleLibraryError("builtin_missing", "Packaged architecture-cn.xlsx is missing")
        return self._import("20260320 优化规则库-建筑.xlsx", BUILTIN_PATH.read_bytes(), "builtin", "", BUILTIN_KEY)

    def import_xlsx(self, filename: str, content: bytes, project_id: str) -> dict:
        project_id = _project(project_id)
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 255 or "/" in filename or "\\" in filename:
            raise RuleLibraryError("invalid_filename", "Provide a plain .xlsx filename without directory components")
        if not filename.lower().endswith(".xlsx"):
            raise RuleLibraryError("invalid_filename", "Only .xlsx rule workbooks are supported")
        key = "temporary:" + filename.strip().casefold()
        return self._import(filename, content, "project", project_id, key)

    def _import(self, filename: str, content: bytes, scope: str, project_id: str, key: str) -> dict:
        if not isinstance(content, bytes) or not content or len(content) > self.max_source_bytes:
            # The parser produces the precise diagnostic without persisting anything.
            parse_xlsx(content, self.max_source_bytes)
        source_sha = digest(content)
        identity = _json([scope, project_id, key, source_sha])
        library_id = "lib_" + digest(identity.encode())[:32]
        with self._connect() as db:
            existing = db.execute("SELECT document FROM rule_libraries WHERE library_id=?", (library_id,)).fetchone()
        if existing:
            return json.loads(existing["document"])
        workbook = parse_xlsx(content, self.max_source_bytes)
        rules, diagnostics = extract_rules(workbook)
        for rule in rules:
            rule["library_id"] = library_id
            rule["qualified_ref"] = f"{library_id}:{rule['rule_id']}"
            rule["source_sha256"] = source_sha
            rule["version"] = source_sha
        created_at = _now()
        document = {"schema_version": 1, "library_id": library_id, "library_key": key, "scope": scope,
                    "project_id": project_id or None, "filename": filename, "source_sha256": source_sha,
                    "version": source_sha, "created_at": created_at, "status": "source_only", "readiness": "needs_interpretation",
                    "rule_count": len(rules), "active_rule_count": sum(r["status"] == "active" for r in rules),
                    "disabled_rule_count": sum(r["status"] == "disabled" for r in rules),
                    "image_count": workbook["image_count"], "cell_count": workbook["cell_count"],
                    "diagnostics": diagnostics, "rules": rules, "workbook": workbook}
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO rule_libraries VALUES(?,?,?,?,?,?,?,?)", (library_id, scope, project_id, key, source_sha, created_at, _json(document), content))
            stored = db.execute("SELECT document FROM rule_libraries WHERE library_id=?", (library_id,)).fetchone()
        return json.loads(stored["document"])

    @staticmethod
    def _summary(document: dict) -> dict:
        return {key: value for key, value in document.items() if key not in {"rules", "workbook"}}

    def list_libraries(self, project_id: str | None = None) -> list[dict]:
        if project_id is not None:
            _project(project_id)
        with self._connect() as db:
            if project_id is None:
                rows = db.execute("SELECT document FROM rule_libraries WHERE scope='builtin' ORDER BY library_key,created_at").fetchall()
            else:
                rows = db.execute("SELECT document FROM rule_libraries WHERE scope='builtin' OR project_id=? ORDER BY scope,library_key,created_at", (project_id,)).fetchall()
        return [self._summary(json.loads(row["document"])) for row in rows]

    def _get_row(self, library_id: str, project_id: str | None = None):
        with self._connect() as db:
            row = db.execute("SELECT * FROM rule_libraries WHERE library_id=?", (library_id,)).fetchone()
        if row is None:
            raise RuleLibraryError("library_not_found", "Rule library was not found")
        if row["scope"] != "builtin" and row["project_id"] != project_id:
            raise RuleLibraryError("project_mismatch", "Rule library does not belong to this project")
        return row

    def get_library(self, library_id: str, project_id: str | None = None) -> dict:
        return json.loads(self._get_row(library_id, project_id)["document"])

    def get_rule(self, library_id: str, rule_id: str, project_id: str | None = None) -> dict:
        document = self.get_library(library_id, project_id)
        for rule in document["rules"]:
            if rule["rule_id"] == rule_id:
                return rule
        raise RuleLibraryError("rule_not_found", "Rule was not found in this library version")

    def get_source(self, library_id: str, project_id: str | None = None) -> bytes:
        row = self._get_row(library_id, project_id)
        content = bytes(row["source"])
        if digest(content) != row["source_sha256"]:
            raise RuleLibraryError("source_corrupt", "Stored workbook source hash does not match")
        return content

    def get_image(self, library_id: str, image_id: str, project_id: str | None = None) -> tuple[bytes, str]:
        document = self.get_library(library_id, project_id)
        picture = next((image for sheet in document["workbook"]["sheets"] for image in sheet["images"] if image["image_id"] == image_id), None)
        if picture is None or not picture.get("part"):
            raise RuleLibraryError("image_not_found", "Image source was not found")
        with open_archive(self.get_source(library_id, project_id), self.max_source_bytes) as archive:
            content = archive.read(picture["part"])
        if digest(content) != picture["sha256"]:
            raise RuleLibraryError("source_corrupt", "Stored image hash does not match")
        return content, picture["mime_type"]

    def _resolve_ref(self, rule_ref: str, project_id: str) -> dict:
        if not isinstance(rule_ref, str) or ":" not in rule_ref:
            raise RuleLibraryError("invalid_rule_ref", "Use the rule's full qualified_ref")
        library_id, rule_id = rule_ref.split(":", 1)
        return self.get_rule(library_id, rule_id, project_id)

    def create_effective_set(self, project_id: str, selections: list, overrides: list | None = None) -> dict:
        project_id = _project(project_id)
        if not isinstance(selections, list) or not selections or len(selections) > 100:
            raise RuleLibraryError("invalid_selection", "Select between 1 and 100 library versions")
        overrides = [] if overrides is None else overrides
        if not isinstance(overrides, list) or len(overrides) > 1000:
            raise RuleLibraryError("invalid_overrides", "Overrides must be a list of at most 1000 explicit operations")
        active, disabled, libraries = {}, [], {}

        def remember(library_id):
            library = self.get_library(library_id, project_id)
            libraries[library_id] = {key: library[key] for key in ("library_id", "library_key", "scope", "project_id", "source_sha256", "version")}
            return library

        normalized_selections = []
        for selection in selections:
            if not isinstance(selection, dict) or not isinstance(selection.get("library_id"), str):
                raise RuleLibraryError("invalid_selection", "Each selection must contain a library_id")
            library = remember(selection["library_id"])
            ids = selection.get("rule_ids")
            if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids)):
                raise RuleLibraryError("invalid_selection", "rule_ids must be a non-empty list of rule IDs")
            chosen = library["rules"] if ids is None else [self.get_rule(library["library_id"], identifier, project_id) for identifier in ids]
            normalized_selections.append({"library_id": library["library_id"], "rule_ids": sorted(set(rule["rule_id"] for rule in chosen))})
            for rule in chosen:
                if rule["status"] == "disabled":
                    disabled.append({"rule_ref": rule["qualified_ref"], "reason": "disabled_in_source"})
                else:
                    active[rule["qualified_ref"]] = rule
        normalized_overrides = []
        for operation in overrides:
            if not isinstance(operation, dict) or operation.get("op") not in {"add", "disable", "replace"}:
                raise RuleLibraryError("invalid_override", "Supported operations are add, disable and replace")
            op = operation["op"]
            target = operation.get("target_ref")
            if op in {"disable", "replace"} and not isinstance(target, str):
                raise RuleLibraryError("invalid_override", "target_ref must be a qualified rule reference string")
            if op in {"disable", "replace"} and target not in active:
                raise RuleLibraryError("override_target_missing", "Override target must be an active selected qualified_ref")
            replacement = None
            if op in {"add", "replace"}:
                ref = operation.get("rule_ref") if op == "add" else operation.get("replacement_ref")
                replacement = self._resolve_ref(ref, project_id)
                remember(replacement["library_id"])
                if replacement["status"] != "active":
                    raise RuleLibraryError("disabled_rule", "A rule disabled in its source cannot be activated by an override")
                if op == "replace" and ref == target:
                    raise RuleLibraryError("invalid_override", "Replacement must reference a different rule version")
                if op == "add" and ref in active:
                    raise RuleLibraryError("duplicate_rule_ref", "Rule is already selected")
            if op in {"disable", "replace"}:
                del active[target]
                disabled.append({"rule_ref": target, "reason": operation.get("reason") or op, **({"replacement_ref": replacement["qualified_ref"]} if replacement else {})})
            if replacement:
                active[replacement["qualified_ref"]] = replacement
            normalized = {"op": op}
            for key in ("target_ref", "rule_ref", "replacement_ref", "reason"):
                if key in operation:
                    if not isinstance(operation[key], str):
                        raise RuleLibraryError("invalid_override", f"{key} must be a string")
                    normalized[key] = operation[key]
            normalized_overrides.append(normalized)
        by_id = {}
        for rule in active.values():
            if rule["rule_id"] in by_id:
                raise RuleLibraryError("rule_conflict", "Rules with the same ID require an explicit disable or replace operation", [{"rule_id": rule["rule_id"], "rule_refs": [by_id[rule["rule_id"]], rule["qualified_ref"]]}])
            by_id[rule["rule_id"]] = rule["qualified_ref"]
        if not active:
            raise RuleLibraryError("empty_effective_set", "The selection contains no active rules")
        snapshot = {"schema_version": 1, "project_id": project_id,
                    "selections": sorted(normalized_selections, key=lambda x: (x["library_id"], x["rule_ids"])),
                    "overrides": normalized_overrides, "libraries": sorted(libraries.values(), key=lambda x: x["library_id"]),
                    "rules": sorted(active.values(), key=lambda x: x["qualified_ref"]),
                    "disabled": sorted(disabled, key=lambda x: (x["rule_ref"], x["reason"])),
                    "readiness": "needs_interpretation", "diagnostics": [{"code": "source_only_rules", "message": "Selection freezes source records; it does not establish executable or validated rule semantics"}]}
        effective_hash = digest(_json(snapshot).encode())
        set_id = "eff_" + effective_hash
        snapshot.update({"effective_set_id": set_id, "effective_hash": effective_hash, "created_at": _now()})
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO effective_rule_sets VALUES(?,?,?,?)", (set_id, project_id, snapshot["created_at"], _json(snapshot)))
            row = db.execute("SELECT document FROM effective_rule_sets WHERE effective_set_id=?", (set_id,)).fetchone()
        return json.loads(row["document"])

    def get_effective_set(self, effective_set_id: str, project_id: str) -> dict:
        _project(project_id)
        with self._connect() as db:
            row = db.execute("SELECT project_id,document FROM effective_rule_sets WHERE effective_set_id=?", (effective_set_id,)).fetchone()
        if row is None:
            raise RuleLibraryError("effective_set_not_found", "Effective rule set was not found")
        if row["project_id"] != project_id:
            raise RuleLibraryError("project_mismatch", "Effective rule set does not belong to this project")
        return json.loads(row["document"])

    def list_effective_sets(self, project_id: str) -> list[dict]:
        _project(project_id)
        with self._connect() as db:
            rows = db.execute("SELECT document FROM effective_rule_sets WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
        return [{key: value for key, value in json.loads(row["document"]).items() if key not in {"rules"}} for row in rows]
