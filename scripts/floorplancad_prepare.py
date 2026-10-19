"""Read only explicitly allowed training archives; never discover/open test data."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

ARCHIVES = ("train-00.tar.xz", "train-01.tar.xz")
SPLIT_SEED = "floorplancad-vector-v1-20261016"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def partition(group):
    return "validation" if int(digest((SPLIT_SEED + ':' + group).encode())[:8], 16) % 5 == 0 else "development"


def prepare(source, output):
    output.mkdir(parents=True, exist_ok=True)
    policy = dict(version=1, allowed_archives=list(ARCHIVES), test_access=False,
        test_policy="Do not open, extract, inspect, score or tune against test archives in this development command.",
        grouping="filename stem before first hyphen; conservative filename family, NOT verified project identity",
        split_seed=SPLIT_SEED, validation_fraction_target=.2,
        selection="Stable SHA256 order of sample names; never select by labels or performance.",
        tuning="Development only; freeze source and config hashes before validation; no per-file/layer/ID exceptions.",
        limits="No claim of project-independent generalization until source-group mapping is verified.",
        recognition_inputs="Geometry only; semantic/instance annotations and presentation styles excluded.",
        room_ground_truth="Symbol labels do not establish room/net-area ground truth.")
    policy_path = output / "protocol.json"
    if policy_path.exists():
        if json.loads(policy_path.read_text()) != policy:
            raise ValueError("Existing protocol differs; use a new versioned directory")
    else:
        policy_path.write_text(json.dumps(policy, indent=2, ensure_ascii=False))
    rows = []; counts = Counter(); seen = {}
    for name in ARCHIVES:
        path = source / name
        # Exact allowlist, not a directory glob; test is never opened, even for hashing.
        if path.is_symlink():
            raise ValueError("Archive symlinks are forbidden")
        print(f"Reading allowed archive {name}", flush=True)
        h = hashlib.sha256()
        with path.open('rb') as f:
            while block := f.read(8 * 1024 * 1024):
                h.update(block)
        with tarfile.open(path, 'r|xz') as archive:
            for member in archive:
                if not member.isfile():
                    continue
                p = PurePosixPath(member.name)
                counts[p.suffix.lower()] += 1
                if p.suffix.lower() != '.svg':
                    continue
                if p.is_absolute() or '..' in p.parts or member.size > 32 * 1024 * 1024:
                    raise ValueError(f"Unsafe SVG member: {member.name}")
                raw = archive.extractfile(member).read()
                sha = digest(raw)
                sample_id = name.removesuffix('.tar.xz') + '/' + p.stem
                if sample_id in seen:
                    raise ValueError(f"Duplicate member in archive: {member.name}")
                seen[sample_id] = sha
                group = p.stem.split('-')[0]
                split = partition(group)
                dest = output / 'svg' / split / name.removesuffix('.tar.xz') / p.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                rows.append(dict(sample_id=sample_id, group_id=group, split=split,
                    archive=name, archive_sha256=h.hexdigest(), member=member.name,
                    sha256=sha, path=str(dest.resolve()), selection_key=digest((SPLIT_SEED+':'+sample_id).encode())))
                if len(rows) % 1000 == 0:
                    print(f"Saved {len(rows)} training-source SVGs", flush=True)
        print(f"Completed {name}: {len(rows)} SVGs total", flush=True)
    # Exact source duplicates across family names must never leak across partitions.
    hashes = {}
    for row in rows:
        hashes.setdefault(row['sha256'], []).append(row)
    cross_duplicates = [v for v in hashes.values() if len({r['split'] for r in v}) > 1]
    quarantine_groups = {r['group_id'] for group in cross_duplicates for r in group}
    for row in rows:
        if row['group_id'] in quarantine_groups:
            row['split'] = 'quarantine'
            row['quarantine_reason'] = 'family contains cross-split exact source duplicate'
    rows.sort(key=lambda r:r['selection_key'])
    (output/'manifest.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    summary = dict(samples=len(rows), splits=dict(Counter(r['split'] for r in rows)),
        groups={s:len({r['group_id'] for r in rows if r['split']==s}) for s in ('development','validation')},
        archive_member_extensions=dict(counts), test_opened=False,
        initial_exact_cross_split_duplicate_sets=len(cross_duplicates),
        quarantine_groups=sorted(quarantine_groups), exact_cross_split_duplicates_after_quarantine=0,
        manifest_sha256=digest((output/'manifest.json').read_bytes()))
    (output/'inventory.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
