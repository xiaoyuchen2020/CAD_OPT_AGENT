"""Content-addressed test inputs, with relative links for legacy path consumers.

Only drawing payloads are shared. Databases, reports, traces and code stay local.
Writers must replace links atomically, never edit a shared target in place.
"""
from hashlib import sha256
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from uuid import uuid4

POOL = "_shared"
DRAWING_FILES = frozenset({
    "scene.json", "entities.json", "regions.json", "render.json",
    "geometry.json", "oda.json",
})


def eligible(path):
    path = Path(path)
    return path.suffix.lower() in {".dxf", ".dwg"} or path.name in DRAWING_FILES


def artifact_root(path):
    # Outermost artifacts wins, including frozen-code/artifacts nested inside it.
    path = Path(os.path.abspath(path))
    roots = [p for p in path.parents if p.name == "artifacts"]
    return roots[-1] if roots else None


def file_digest(path):
    h = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def signature(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def publish_blob(source, root, suffix=None):
    """Publish complete, read-only bytes; concurrent writers never overwrite."""
    source, root = Path(source), Path(root)
    before = signature(source)
    digest = file_digest(source)
    extension = source.suffix.lower() if suffix is None else suffix
    target = root / POOL / "sha256" / digest[:2] / (digest + extension)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if file_digest(target) != digest:
            raise ValueError(f"Shared input checksum mismatch: {target}")
    else:
        temporary = None
        try:
            with NamedTemporaryFile(dir=target.parent, prefix=".publish-", delete=False) as stream:
                temporary = Path(stream.name)
                with source.open("rb") as original:
                    shutil.copyfileobj(original, stream, 1024 * 1024)
                stream.flush()
                os.fsync(stream.fileno())
            if file_digest(temporary) != digest or signature(source) != before:
                raise ValueError(f"Input changed during publication: {source}")
            temporary.chmod(0o444)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if file_digest(target) != digest:
                    raise ValueError(f"Shared input checksum mismatch: {target}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if signature(source) != before:
        raise ValueError(f"Input changed during publication: {source}")
    return target, digest


def link_blob(target, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + uuid4().hex + ".ref")
    try:
        temporary.symlink_to(os.path.relpath(target, destination.parent))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def reference_file(source, destination, root=None, expected_digest=None):
    """Return a JSON-ready reference, relative to the input's owning directory."""
    source, destination = Path(source), Path(destination)
    root = Path(root) if root is not None else artifact_root(destination)
    if root is None:
        raise ValueError("A shared artifacts root is required")
    before = signature(source)
    target, digest = publish_blob(source, root, destination.suffix.lower())
    if expected_digest is not None and digest != expected_digest:
        raise ValueError(f"Input changed between audit and publication: {source}")
    size = target.stat().st_size
    if signature(source) != before:
        raise ValueError(f"Input changed before replacement: {source}")
    if os.path.abspath(source) == os.path.abspath(target):
        raise ValueError("Cannot replace the shared object itself with a reference")
    link_blob(target, destination)
    return {"path": os.path.relpath(target, destination.parent),
            "sha256": digest, "size_bytes": size}


def copy_artifact(source, destination, *, follow_symlinks=True):
    """copy2-compatible: share CAD inputs under artifacts, copy other files."""
    source, destination = Path(source), Path(destination)
    if destination.is_dir():
        destination /= source.name
    root = artifact_root(destination)
    if root is not None and eligible(destination):
        reference_file(source, destination, root)
        return str(destination)
    return shutil.copy2(source, destination, follow_symlinks=follow_symlinks)


def copy_artifact_tree(source, destination, **kwargs):
    """Keep directory-local mutable state while sharing drawing input files."""
    kwargs.setdefault("copy_function", copy_artifact)
    return shutil.copytree(source, destination, **kwargs)
