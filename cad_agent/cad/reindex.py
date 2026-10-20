"""Rebuild derived semantics as a new immutable snapshot, without reconversion."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil

from cad_agent.adapters.store import now
from cad_agent.contracts import stable_id
from .reader import PARSER_VERSION, read_drawing
from .regions import build_regions


def reindex_drawing(workspace, snapshot_id):
    with workspace.import_lock:
        previous = workspace.store.get('drawing', snapshot_id)
        original = workspace.store.artifact_dir(snapshot_id)
        dwg = previous.get('source_format') == 'DWG'
        expected = {'source.dxf': previous['sha256']}
        if dwg:
            expected.update({'source.dwg': previous['original_sha256'],
                             'oda.json': previous['conversion']['json_sha256']})
        for name, digest in expected.items():
            path = original/name
            if not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f'原快照 {name} 校验失败，未重建索引。')
        identity = ('oda-dwg-v1', previous['original_sha256']) if dwg else ()
        sid = stable_id('snap', previous['sha256'], PARSER_VERSION, *identity)
        try:
            cached = workspace.store.get('drawing', sid)
        except KeyError:
            pass
        else:
            target = workspace.store.artifact_dir(sid)
            if not all((target/n).is_file() for n in (*expected, 'scene.json', 'entities.json', 'regions.json')):
                raise ValueError('已发布的索引文件缺失，请恢复快照后重试。')
            for name, digest in expected.items():
                if sha256((target/name).read_bytes()).hexdigest() != digest:
                    raise ValueError('已发布索引的源文件校验失败，请恢复快照后重试。')
            return cached
        with TemporaryDirectory(prefix='.reindex-', dir=workspace.settings.data_dir) as temporary:
            folder = Path(temporary)
            for name in expected:
                shutil.copy2(original/name, folder/name)
            texts, scene, rectangles, segments, diagnostics = read_drawing(folder/'source.dxf', sid)
            regions, frames = build_regions(texts, scene, rectangles, segments, sid)
            scene.update(frames=frames, snapshot_id=sid)
            for name, value in [('scene.json', scene), ('entities.json', [t.model_dump() for t in texts]),
                                ('regions.json', [r.model_dump() for r in regions])]:
                workspace.store.write_json(folder/name, value)
            record = dict(previous, id=sid, snapshot_id=sid, created_at=now(), parser_version=PARSER_VERSION,
                          unit=scene['unit'], layouts=scene['layouts'], frames=frames, text_count=len(texts),
                          region_count=len(regions), diagnostics=[d.model_dump() for d in diagnostics],
                          status='partial' if diagnostics else 'ready', latest_fact_set_id=None,
                          reindexed_from=snapshot_id, index_coverage=scene['parse_coverage'])
            destination = workspace.store.root/'drawings'/sid
            with workspace.lock:
                if destination.exists():
                    raise ValueError('目标索引目录已存在但未发布；请先检查中断的重建。')
                # Publish the metadata last. Old snapshots/evidence never move.
                shutil.move(str(folder), str(destination))
                try:
                    references = workspace.store.share_artifacts(sid)
                    if references:
                        record["artifact_references"] = references
                    workspace.store.put('drawing', sid, record)
                except Exception:
                    shutil.rmtree(destination)
                    raise
            return record
