from concurrent.futures import ThreadPoolExecutor
import json
import shutil

import pytest

from cad_agent.adapters.store import Store
from cad_agent.artifact_storage import copy_artifact, copy_artifact_tree, reference_file
from scripts.compact_artifacts import compact, migrate_cache_manifests


def test_same_bytes_different_names_share_one_drawing_and_move_together(tmp_path):
    root = tmp_path / "artifacts"
    source = tmp_path / "first.DXF"
    source.write_bytes(b"immutable drawing")
    one = root / "run-a/input.dxf"
    two = root / "run-b/source.dxf"
    reference_file(source, one)
    reference_file(source, two)
    assert one.is_symlink() and two.is_symlink()
    assert one.resolve() == two.resolve()
    assert one.resolve().stat().st_mode & 0o222 == 0
    moved = tmp_path / "moved"
    shutil.move(root, moved)
    assert (moved / "run-a/input.dxf").read_bytes() == source.read_bytes()
    assert (moved / "run-b/source.dxf").read_bytes() == source.read_bytes()


def test_concurrent_publish_and_corrupt_object_fail_closed(tmp_path):
    root = tmp_path / "artifacts"
    source = tmp_path / "source.dxf"
    source.write_bytes(b"same")
    paths = [root / f"run-{i}/source.dxf" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda p: reference_file(source, p), paths))
    assert len({p.resolve() for p in paths}) == 1
    blob = paths[0].resolve()
    blob.chmod(0o644)
    blob.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum mismatch"):
        reference_file(source, root / "next/source.dxf")
    assert not (root / "next/source.dxf").exists()


def test_derived_refresh_never_modifies_other_run(tmp_path):
    root = tmp_path / "artifacts"
    a, b = [Store(root / name / "data") for name in ("a", "b")]
    pa, pb = a.artifact("snap_test", "scene.json"), b.artifact("snap_test", "scene.json")
    a.write_json(pa, {"version": 1})
    copy_artifact(pa, pb)
    original = pb.resolve()
    a.write_json(pa, {"version": 2})
    assert json.loads(pb.read_text()) == {"version": 1}
    assert json.loads(pa.read_text()) == {"version": 2}
    assert pb.resolve() == original and pa.resolve() != original


def test_tree_copies_database_but_references_drawings(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.dxf").write_bytes(b"dxf")
    (source / "index.sqlite3").write_bytes(b"database")
    destination = tmp_path / "artifacts/run/data"
    copy_artifact_tree(source, destination)
    assert (destination / "source.dxf").is_symlink()
    assert not (destination / "index.sqlite3").is_symlink()
    (destination / "index.sqlite3").write_bytes(b"changed")
    assert (source / "index.sqlite3").read_bytes() == b"database"


def test_changed_audit_input_does_not_replace_existing_path(tmp_path):
    source = tmp_path / "source.dxf"
    source.write_bytes(b"new drawing")
    target = tmp_path / "artifacts/run/source.dxf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    with pytest.raises(ValueError, match="changed between audit"):
        reference_file(source, target, expected_digest="not-the-expected-hash")
    assert target.read_bytes() == b"original" and not target.is_symlink()


def test_cache_manifest_migration_preserves_originals_and_transitive_hashes(tmp_path):
    from cad_agent.artifact_storage import file_digest
    root = tmp_path / "artifacts"
    root.mkdir()
    leaf = root / "code-manifest.json"
    leaf.write_text(json.dumps({"code.py": "a"*64, ".DS_Store": "b"*64}))
    middle = root / "mid-manifest.json"
    middle.write_text(json.dumps({"code-manifest.json": file_digest(leaf)}))
    outer = root / "outer-manifest.json"
    outer.write_text(json.dumps({"mid-manifest.json": file_digest(middle)}))
    originals = {p: p.read_bytes() for p in (leaf, middle, outer)}
    backup = root / "_maintenance/run"
    receipts = migrate_cache_manifests(root, backup)
    assert len(receipts) == 3
    assert json.loads(leaf.read_text()) == {"code.py": "a"*64}
    assert json.loads(middle.read_text())["code-manifest.json"] == file_digest(leaf)
    assert json.loads(outer.read_text())["mid-manifest.json"] == file_digest(middle)
    for path, original in originals.items():
        assert (backup / "original-manifests" / path.name).read_bytes() == original
    assert migrate_cache_manifests(root, root / "_maintenance/again") == []


def test_compaction_preserves_evidence_is_idempotent_and_cleans_only_caches(tmp_path):
    root = tmp_path / "artifacts"
    for name in ("a", "b"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "source.dxf").write_bytes(b"same")
        (directory / "scene.json").write_text('{"geometry": 1}')
        (directory / "results.json").write_text('{"status": "failed"}')
        (directory / "__pycache__").mkdir()
        (directory / "__pycache__/cached.pyc").write_bytes(b"cache")
    report_bytes = (root / "a/results.json").read_bytes()
    audit = compact(root)
    assert audit["unique_objects"] == 2 and audit["replaced"] == 0
    assert not (root / "_shared").exists()
    result = compact(root, apply=True, clean_caches=True)
    assert result["replaced"] == 4 and not result["errors"]
    assert result["removed_caches"] == 2
    assert (root / "a/results.json").read_bytes() == report_bytes
    assert (root / "b/results.json").exists()
    assert (root / "a/source.dxf").is_symlink()
    again = compact(root, apply=True, clean_caches=True)
    assert again["replaced"] == 0 and again["already_shared"] == 4
    assert not again["errors"]
    references = json.loads(open(result["report"]).read())["references"]
    assert all((root / r["file"]).read_bytes() for r in references)


def test_store_ingestion_in_artifacts_shares_across_workspaces(tmp_path, demo_bytes):
    from cad_agent.config import Settings
    from cad_agent.service import Workspace
    workspaces = [Workspace(Settings(data_dir=tmp_path / "artifacts" / name / "data"))
                  for name in ("a", "b")]
    try:
        records = [w.ingest("fixture.dxf", demo_bytes) for w in workspaces]
        paths = [w.store.artifact(r["id"], "source.dxf") for w, r in zip(workspaces, records)]
        assert paths[0].resolve() == paths[1].resolve()
        assert all(r["artifact_references"]["source.dxf"]["sha256"] == r["sha256"] for r in records)
        assert all(w.regions(r["id"]) for w, r in zip(workspaces, records))
    finally:
        for w in workspaces:
            w.close()
