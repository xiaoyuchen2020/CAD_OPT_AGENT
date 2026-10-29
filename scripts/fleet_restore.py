#!/usr/bin/env python3
"""Inspect by default; explicitly apply a validated fleet backup with rollback copies."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from fleet_backup import STATE, CONFIG, CURRENT, require_root, maintenance_lock, stopped_fleet


def validate_archive(archive):
    seen=set()
    for member in archive.getmembers():
        path=PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in {'manifest.json','state','config','release'}:
            raise ValueError('Unsafe archive member')
        if member.name in seen or not (member.isfile() or member.isdir()):
            raise ValueError('Duplicate or non-regular archive member')
        if member.name=='manifest.json' and (not member.isfile() or member.size>65536):
            raise ValueError('Invalid manifest')
        seen.add(member.name)
    with archive.extractfile('manifest.json') as file:
        manifest=json.load(file)
    if manifest.get('schema_version')!=1 or manifest.get('state_path')!=str(STATE) or manifest.get('config_path')!=str(CONFIG):
        raise ValueError('Unsupported backup schema or installation paths')
    required={'state/cad-center/fleet.sqlite3','config/center.env','config/a.env','config/b.env','release/pyproject.toml'}
    if not required <= seen:
        raise ValueError('Backup is missing required fleet state, configuration, or source')
    return manifest


def extract_regular(archive,stage):
    # No tarfile.extractall: reject links/devices, then write only bounded paths.
    directories=[]
    for member in archive.getmembers():
        target=stage/member.name
        if member.isdir():
            target.mkdir(parents=True,exist_ok=True,mode=0o700)
            directories.append((target,member))
        else:
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with archive.extractfile(member) as source,target.open('xb') as output:
                shutil.copyfileobj(source,output)
            os.chown(target,member.uid,member.gid)
            os.chmod(target,member.mode & 0o777)
    for target,member in reversed(directories):
        os.chown(target,member.uid,member.gid)
        os.chmod(target,member.mode & 0o7777)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    parser.add_argument('--apply',action='store_true',help='Replace live state/config and select the archived source release.')
    parser.add_argument('--allow-interrupt',action='store_true')
    parser.add_argument('--allow-dependency-change',action='store_true',help='Use only after preparing the venv for the archived pyproject dependencies.')
    args=parser.parse_args()
    require_root();os.umask(0o077)
    with tarfile.open(args.archive,'r:gz') as archive:
        manifest=validate_archive(archive)
        if not args.apply:
            print(json.dumps({'status':'inspection_only','manifest':manifest,'apply_required':True},indent=2))
            return
        archived_project=archive.extractfile('release/pyproject.toml').read()
        if sha256(archived_project).digest()!=sha256((CURRENT/'pyproject.toml').read_bytes()).digest() and not args.allow_dependency_change:
            raise SystemExit('Archived dependency declaration differs. Prepare a matching venv before using --allow-dependency-change.')
        with maintenance_lock():
            # Staging on the state filesystem avoids a partially copied live database.
            with tempfile.TemporaryDirectory(prefix='.cad-restore-',dir=STATE.parent) as temp:
                stage=Path(temp)
                extract_regular(archive,stage)
                stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                old_state=STATE.with_name(STATE.name+'.before-restore-'+stamp)
                old_config=CONFIG.with_name(CONFIG.name+'.before-restore-'+stamp)
                release=Path('/opt/cad-atlas/releases')/('restore-'+stamp)
                old_current=CURRENT.resolve(strict=True)
                swapped=[]
                with stopped_fleet(args.allow_interrupt):
                    try:
                        # Config may be on another filesystem; stage there before swapping.
                        config_stage=CONFIG.with_name('.cad-config-restore-'+stamp)
                        shutil.copytree(stage/'config',config_stage,copy_function=shutil.copy2)
                        for src in (stage/'config').rglob('*'):
                            dest=config_stage/src.relative_to(stage/'config')
                            st=src.stat();os.chown(dest,st.st_uid,st.st_gid)
                        root_stat=(stage/'config').stat();os.chown(config_stage,root_stat.st_uid,root_stat.st_gid)
                        os.replace(STATE,old_state);swapped.append('state')
                        os.replace(stage/'state',STATE)
                        os.replace(CONFIG,old_config);swapped.append('config')
                        os.replace(config_stage,CONFIG)
                        shutil.copytree(stage/'release',release)
                        link=CURRENT.with_name('.current-restore-'+stamp)
                        link.symlink_to(release);os.replace(link,CURRENT)
                    except BaseException:
                        # Preserve failed new state for diagnosis, restore originals.
                        if 'config' in swapped:
                            if CONFIG.exists(): os.replace(CONFIG,CONFIG.with_name(CONFIG.name+'.failed-restore-'+stamp))
                            os.replace(old_config,CONFIG)
                        if 'state' in swapped:
                            if STATE.exists(): os.replace(STATE,STATE.with_name(STATE.name+'.failed-restore-'+stamp))
                            os.replace(old_state,STATE)
                        link=CURRENT.with_name('.current-rollback-'+stamp)
                        link.symlink_to(old_current);os.replace(link,CURRENT)
                        raise
                print(json.dumps({'status':'restored','previous_state':str(old_state),'previous_config':str(old_config),
                    'source_release':str(release),'note':'Reused existing venv; verify matching dependencies before changing releases.'}))


if __name__=='__main__':
    main()
