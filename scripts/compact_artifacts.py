#!/usr/bin/env python3
"""Audit or compact artifacts; never delete experimental results by age.

python -m scripts.compact_artifacts --root artifacts
python -m scripts.compact_artifacts --root artifacts --apply --clean-caches
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil

from cad_agent.artifact_storage import (
    POOL, eligible, file_digest, reference_file, signature,
)

CACHE_DIRS = {"__pycache__", ".pytest_cache"}


def migrate_cache_manifests(root, backup_root):
    """Exclude cache-only hash entries, retaining original manifests verbatim.

    Some historical snapshots included Finder metadata and Python bytecode in
    file checksums. Update checksum references between affected manifests too;
    never change an actual source/input checksum or an evaluation result.
    """
    documents = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [n for n in dirs if n not in {POOL, "_maintenance", *CACHE_DIRS}
                   and not (Path(directory) / n).is_symlink()]
        for name in files:
            if not name.endswith(".json") or not any(s in name for s in ("manifest", "sha256", "hash")):
                continue
            path = Path(directory) / name
            if path.is_symlink() or path.stat().st_size > 8_000_000:
                continue
            original = path.read_bytes()
            try:
                value = json.loads(original)
            except ValueError:
                continue
            documents[path] = {"original": original, "value": value, "removed": [], "updated": []}

    def visit(value, document):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if isinstance(item, str) and re.fullmatch("[a-f0-9]{64}", item):
                    parts = Path(key).parts
                    if Path(key).name == ".DS_Store" or "__pycache__" in parts or key.endswith(".pyc"):
                        document["removed"].append({"path": key, "sha256": item})
                        del value[key]
                else:
                    visit(item, document)
        elif isinstance(value, list):
            for item in value:
                visit(item, document)

    for doc in documents.values():
        visit(doc["value"], doc)
    # File-hash manifests may contain hashes of other manifests. Propagate only
    # these references, while keeping the immutable original copies for audit.
    from hashlib import sha256
    def encode(doc):
        return (json.dumps(doc["value"], ensure_ascii=False, indent=2) + "\n").encode()
    changed = {p: d for p, d in documents.items() if d["removed"]}
    aliases = {}
    for _ in range(len(documents) + 1):
        for path, doc in changed.items():
            aliases[sha256(doc["original"]).hexdigest()] = path
            aliases[sha256(encode(doc)).hexdigest()] = path
        replacements = {digest: sha256(encode(changed[path])).hexdigest()
                        for digest, path in aliases.items()}
        updated = False
        def update(value, doc):
            nonlocal updated
            if isinstance(value, dict):
                for key, item in value.items():
                    if (key.endswith(".json") and isinstance(item, str) and item in replacements
                            and item != replacements[item]):
                        value[key] = replacements[item]
                        doc["updated"].append({"path": key, "before": item, "after": value[key]})
                        updated = True
                    else:
                        update(item, doc)
            elif isinstance(value, list):
                for item in value:
                    update(item, doc)
        for path, doc in documents.items():
            update(doc["value"], doc)
            if doc["removed"] or doc["updated"]:
                changed[path] = doc
        if not updated:
            break
    else:
        raise ValueError("Cyclic checksum manifests; refusing to modify them")
    receipts = []
    for path, doc in changed.items():
        relative = path.relative_to(root)
        backup = backup_root / "original-manifests" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(doc["original"])
        replacement = encode(doc)
        temporary = path.with_name(path.name + ".storage-tmp")
        temporary.write_bytes(replacement)
        temporary.replace(path)
        receipts.append({"file": str(relative), "backup": str(backup.relative_to(root)),
                         "original_sha256": sha256(doc["original"]).hexdigest(),
                         "updated_sha256": sha256(replacement).hexdigest(),
                         "excluded_cache_hashes": doc["removed"], "updated_manifest_hashes": doc["updated"]})
    if receipts:
        (backup_root / "manifest-migration.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")
    return receipts


def inventory(root):
    """Do not follow directory links or walk the shared store/maintenance logs."""
    files, caches = [], []
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            if path.is_symlink() or (parent == root and name in {POOL, "_maintenance"}):
                dirs.remove(name)
            elif name in CACHE_DIRS or (parent == root and name == "npm-cache"):
                caches.append(path)
                dirs.remove(name)
        for name in names:
            path = parent / name
            if name == ".DS_Store" or path.suffix == ".pyc":
                caches.append(path)
            elif eligible(path):
                files.append(path)
    return sorted(files), sorted(caches)


def allocated_bytes(root):
    seen, total = set(), 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            stat = (Path(directory) / name).lstat()
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                seen.add(key)
                total += stat.st_blocks * 512
    return total


def compact(root, apply=False, clean_caches=False):
    root = Path(root).resolve(strict=True)
    if root.name != "artifacts":
        raise ValueError("Refusing a root not named artifacts")
    paths, caches = inventory(root)
    summary = {
        "schema": "cad-artifacts-storage-v1", "applied": apply,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "allocated_before": allocated_bytes(root), "input_paths": len(paths),
        "replaced": 0, "already_shared": 0, "removed_caches": 0,
        "references": [], "errors": [],
    }
    log = None
    if apply:
        logs = root / "_maintenance"
        logs.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        log_path = logs / (stamp + ".jsonl")
        log = log_path.open("x")
        summary["journal"] = str(log_path.relative_to(root))
    unique, hash_cache = {}, {}
    try:
        for index, path in enumerate(paths):
            relative = str(path.relative_to(root))
            try:
                before = signature(path)
                digest = hash_cache.get(before)
                if digest is None:
                    digest = hash_cache[before] = file_digest(path)
                if signature(path) != before:
                    raise ValueError("Input changed during audit")
                unique[(digest, path.suffix.lower())] = before[2]
                entry = {"file": relative, "sha256": digest, "size_bytes": before[2]}
                shared = path.is_symlink() and path.resolve().is_relative_to(root / POOL / "sha256")
                if apply:
                    if shared:
                        # Check the address as well as existence; don't silently bless corruption.
                        if path.resolve().stem != digest:
                            raise ValueError("Shared input checksum mismatch")
                        entry["path"] = os.readlink(path)
                        summary["already_shared"] += 1
                    else:
                        # Persist intent before destructive replacement; replay is idempotent.
                        log.write(json.dumps({"action": "reference", **entry}) + "\n")
                        log.flush()
                        os.fsync(log.fileno())
                        entry.update(reference_file(path, path, root, expected_digest=digest))
                        summary["replaced"] += 1
                    log.write(json.dumps({"action": "verified", **entry}) + "\n")
                    log.flush()
                summary["references"].append(entry)
            except (OSError, ValueError) as exc:
                summary["errors"].append({"file": relative, "error": str(exc)})
            if (index + 1) % 100 == 0:
                print(f"Checked {index + 1}/{len(paths)} inputs", flush=True)
        if apply and clean_caches:
            summary["manifest_migrations"] = migrate_cache_manifests(root, logs / stamp)
            for path in caches:
                entry = {"action": "remove_cache", "file": str(path.relative_to(root))}
                log.write(json.dumps(entry) + "\n"); log.flush(); os.fsync(log.fileno())
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)
                    summary["removed_caches"] += 1
                except OSError as exc:
                    summary["errors"].append({**entry, "error": str(exc)})
    finally:
        if log:
            log.close()
    summary.update(unique_objects=len(unique), unique_bytes=sum(unique.values()),
                   cache_candidates=len(caches), allocated_after=allocated_bytes(root),
                   finished_at=datetime.now(timezone.utc).isoformat())
    if apply:
        report = log_path.with_suffix(".json")
        report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        summary["report"] = str(report)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("artifacts"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--clean-caches", action="store_true")
    args = parser.parse_args()
    result = compact(args.root, args.apply, args.clean_caches)
    display = {k: v for k, v in result.items() if k not in {"references", "manifest_migrations"}}
    display["manifest_migrations"] = len(result.get("manifest_migrations", []))
    print(json.dumps(display, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
