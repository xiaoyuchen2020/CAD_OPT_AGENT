"""Version/source-bound canonical page cache. Never stores answers or read credit."""
from copy import deepcopy
import hashlib
import json
import time
from cad_agent.cache import workspace_cache, file_stamp
from cad_agent.contracts import stable_id
from cad_agent.analysis import assembly
from cad_agent.cad.numbers import VERSION as NUMBER_VERSION
from cad_agent.evidence import verify_source_evidence

# v1 pages can contain evidence slices that lost an ordinal or list thickness.
# Rebuild those pages while retaining the stable source and number identities.
VERSION = 'source-record-store-v2'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_sources(value, index):
    if isinstance(value, dict):
        if {'evidence_id', 'snapshot_id', 'region_id', 'text_span', 'quote'} <= value.keys():
            region = index.by_id.get(value['region_id'])
            verify_source_evidence(value, snapshot_id=index.snapshot, region=region,
                                   drawing_unit=index.drawing['unit'])
        for child in value.values():
            verify_sources(child, index)
    elif isinstance(value, list):
        for child in value:
            verify_sources(child, index)


def read_table_page(workspace, index, args):
    """Business adapters share the canonical source validation/cache discipline.

    This cache stores semantic pages, never business conclusions or read credit.
    Every caller still registers its own observation and pagination coverage.
    """
    from cad_agent.semantics.tables import TABLE_VERSION, read_semantic_table
    paths = [workspace.store.artifact(index.snapshot, n) for n in ('regions.json', 'scene.json')]
    stamps = tuple(file_stamp(p) for p in paths)
    if stamps != index.signature[2:]:
        raise ValueError('SOURCE_RECORD_INDEX_CHANGED')
    hashes = workspace_cache(workspace, '_source_record_hashes', max_entries=16).get_or_build(
        stamps, lambda: [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
    signature = dict(version=VERSION, parser=TABLE_VERSION, snapshot=index.snapshot,
                     source_hashes=hashes, arguments=args.model_dump())
    key = stable_id('source_table_page', digest(signature))
    hit = False
    try:
        saved = workspace.store.get('source_table_page', key)
        page = saved['page']
        if saved['signature'] != signature or saved['digest'] != digest(page):
            raise ValueError('SOURCE_RECORD_CACHE_MISMATCH')
        verify_sources(page, index)
        hit = True
    except (KeyError, ValueError, TypeError):
        page = read_semantic_table(index, args)
        verify_sources(page, index)
        workspace.store.put('source_table_page', key,
                            dict(signature=signature, digest=digest(page), page=page))
    if tuple(file_stamp(p) for p in paths) != stamps:
        raise ValueError('SOURCE_RECORD_INDEX_CHANGED')
    return deepcopy(page), hit


def read_page(session, index, args):
    started = time.monotonic()
    session._remaining()
    paths = [session.w.store.artifact(index.snapshot, n) for n in ('regions.json', 'scene.json')]
    if tuple(file_stamp(p) for p in paths)!=index.signature[2:]:
        raise ValueError('SOURCE_RECORD_INDEX_CHANGED')
    cache = workspace_cache(session.w, '_source_record_hashes', max_entries=16)
    source_hashes = cache.get_or_build(tuple(file_stamp(p) for p in paths),
        lambda: [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
    signature = dict(version=VERSION, parser=assembly.VERSION, numbers=NUMBER_VERSION,
                     snapshot=index.snapshot, source_hashes=source_hashes,
                     arguments=args.model_dump() | {'snapshot_id': index.snapshot})
    key = stable_id('source_record_page', digest(signature))
    hit = False
    try:
        saved = session.w.store.get('source_record_page', key)
        result = saved['page']
        if saved['signature'] != signature or saved['digest'] != digest(result):
            raise ValueError('SOURCE_RECORD_CACHE_MISMATCH')
        verify_sources(result, index)
        hit = True
    except (KeyError, ValueError, TypeError):
        result = assembly.read(index, args)
        verify_sources(result, index)
        session.w.store.put('source_record_page', key,
            dict(signature=signature, digest=digest(result), page=result))
    session._remaining()
    stats = getattr(session, 'source_record_cache', None)
    if stats is None:
        stats = session.source_record_cache = dict(hits=0, builds=0, seconds=0.0)
    stats['hits' if hit else 'builds'] += 1
    stats['seconds'] += time.monotonic()-started
    return deepcopy(result)
