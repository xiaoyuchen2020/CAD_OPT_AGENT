"""Snapshot-scoped lexical/dense retrieval. Hits are pointers, never evidence."""

from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import re
import threading
import time
from uuid import uuid4
from zipfile import BadZipFile
from cad_agent.cache import BoundedCache
from cad_agent.contracts import stable_id
from cad_agent.adapters.embedding import get_embedder, EmbeddingUnavailable

VERSION = "source-chunks-v1"
_LEXICAL = BoundedCache(max_entries=32, max_bytes=64 * 1024 * 1024)
_CHUNKS = BoundedCache(max_entries=8, max_bytes=64 * 1024 * 1024)
_VECTOR_LOCK = threading.Lock()
_VECTOR_PENDING = {}


def tokens(text):
    result = re.findall(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            result.append(run)
    return result


def source_context_path(index, region, limit=12):
    """Parser ancestry pointers only; never select the nearest visible heading."""
    seen = {region.region_id}
    pending = [(rid, region.region_id, 1) for rid in region.context_region_ids]
    parents = []
    while pending and len(parents) < limit:
        rid, child, depth = pending.pop(0)
        parent = index.by_id.get(rid)
        if rid in seen or parent is None:
            continue
        seen.add(rid)
        if (parent.layout_id, parent.frame_id) != (region.layout_id, region.frame_id):
            continue
        parents.append(dict(region_id=rid, child_region_id=child, depth=depth,
            text_preview=parent.text[:180], text_span=[0, len(parent.text)],
            relationship='parser_parent', attribution_state='unverified'))
        pending.extend((ancestor, rid, depth + 1) for ancestor in parent.context_region_ids)
    return parents


def _build_chunks(index):
    out = []
    for r in index.regions:
        parents = [
            index.by_id[rid]
            for rid in r.context_region_ids
            if rid in index.by_id
            and index.by_id[rid].layout_id == r.layout_id
            and index.by_id[rid].frame_id == r.frame_id
        ]
        context = " | ".join(p.text[:80] for p in parents[:2])[:120]
        # Existing clause/table-row grouping is retained. Long regions overlap, and
        # every indexed span still maps exactly to the original source string.
        for start in range(0, len(r.text), 240):
            end = min(start + 300, len(r.text))
            if not r.text[start:end].strip():
                continue
            out.append(
                {
                    "chunk_id": stable_id(
                        "chunk",
                        index.snapshot,
                        VERSION,
                        r.region_id,
                        str(start),
                        str(end),
                    ),
                    "region_id": r.region_id,
                    "start": start,
                    "end": end,
                    "text": r.text[start:end],
                    "context": context,
                    "index_text": (context + "\n" if context else "")
                    + r.text[start:end],
                }
            )
            if end == len(r.text):
                break
    return out


def chunks(index):
    signature = getattr(index, 'signature', None)
    if signature is None:
        return _build_chunks(index)
    return _CHUNKS.get_or_build((VERSION, signature), lambda: _build_chunks(index),
                                lambda rows: sum(len(c['index_text'].encode()) + 512 for c in rows) * 4)


def _lexical_documents(items):
    docs = [Counter(tokens(c["index_text"])) for c in items]
    df = Counter(t for d in docs for t in d)
    lengths = [sum(d.values()) for d in docs]
    avg = sum(lengths) / max(1, len(lengths)) or 1
    return docs, df, lengths, avg


def lexical_rank(items, query):
    query_tokens = set(tokens(query))
    # Scope is part of the signature: BM25 document frequencies must remain
    # identical to the former per-query, scope-local calculation.
    signature = sha256(json.dumps([VERSION, [c["index_text"] for c in items]], ensure_ascii=False).encode()).digest()
    docs, df, lengths, avg = _LEXICAL.get_or_build(signature, lambda: _lexical_documents(items),
        lambda result: sum(sum(len(t.encode()) + 96 for t in d) for d in result[0]) + 128 * len(items))
    scored = []
    for i, d in enumerate(docs):
        score = 0.0
        for t in query_tokens.intersection(d):
            idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
            tf = d[t]
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[i] / avg))
        if query.strip().casefold() in items[i]["text"].casefold():
            score += 20
        if score > 0:
            scored.append((i, score))
    return sorted(scored, key=lambda x: (-x[1], items[x[0]]["chunk_id"]))


@contextmanager
def _vector_lock(key):
    with _VECTOR_LOCK:
        entry = _VECTOR_PENDING.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _VECTOR_LOCK:
            entry[1] -= 1
            if entry[1] == 0:
                del _VECTOR_PENDING[key]


def document_vectors(index, items, embedder):
    import numpy as np

    signature = sha256(
        json.dumps(
            [
                VERSION,
                index.snapshot,
                embedder.identity,
                [c["index_text"] for c in items],
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    root = index.w.store.root / "retrieval"
    root.mkdir(exist_ok=True)
    path = root / (signature + ".npz")
    # Only callers constructing this exact on-disk index serialize. Unrelated
    # drawings and models can build concurrently.
    with _vector_lock(str(path)):
        if path.exists():
            try:
                with np.load(path, allow_pickle=False) as data:
                    vectors = data["vectors"]
                    if (
                        vectors.ndim != 2
                        or vectors.shape[0] != len(items)
                        or not np.isfinite(vectors).all()
                    ):
                        raise ValueError("bad cache")
                    return vectors, {
                        "cache_hit": True,
                        "input_tokens": 0,
                        "seconds": 0.0,
                        "index_id": signature,
                    }
            except (OSError, ValueError, KeyError, BadZipFile, EOFError):
                pass  # Regenerate invalid derived cache, never alter source evidence.
        vectors, usage = embedder.encode([c["index_text"] for c in items])
        if (
            vectors.ndim != 2
            or vectors.shape[0] != len(items)
            or not np.isfinite(vectors).all()
        ):
            raise EmbeddingUnavailable("embedding_invalid_vectors")
        temporary = root / (signature + "." + uuid4().hex + ".tmp")
        with temporary.open("wb") as f:
            np.savez_compressed(f, vectors=vectors)
        temporary.replace(path)
        return vectors, usage | {"index_id": signature}


def search(
    index,
    query="",
    keywords=None,
    match="any",
    layout_id="",
    frame_id="",
    section_id="",
    offset=0,
    limit=8,
    mode="hybrid",
):
    started = time.monotonic()
    if mode not in ("lexical", "semantic", "hybrid"):
        raise ValueError("无效检索模式")
    allowed = {r.region_id for r in index.scope(layout_id, frame_id, section_id)}
    terms = list(
        dict.fromkeys(t.strip() for t in [query] + (keywords or []) if t.strip())
    )
    if match == "all" and terms:
        allowed &= {
            r.region_id
            for r in index.regions
            if all(t.casefold() in r.text.casefold() for t in terms)
        }
    if not terms or not allowed:
        result = index.search(
            query,
            keywords,
            match,
            layout_id,
            frame_id,
            section_id,
            offset,
            limit,
            mode="keyword",
        )
        return result | {
            "retrieval": {
                "requested_mode": mode,
                "effective_mode": "keyword",
                "fallback_reason": "empty_query_or_scope",
                "seconds": round(time.monotonic() - started, 4),
            }
        }
    all_chunks = chunks(index)
    items = [c for c in all_chunks if c["region_id"] in allowed]
    text = " ".join(terms)
    lexical = lexical_rank(items, text)
    meta = {
        "requested_mode": mode,
        "effective_mode": mode,
        "chunk_version": VERSION,
        "scope_chunk_count": len(items),
        "candidate_limit": 60 if mode == 'semantic' else None,
        "lexical_candidate_limit": None,
        "semantic_candidate_limit": 60 if mode != 'lexical' else None,
        "score_is_confidence": False,
    }
    dense = []
    if mode != "lexical":
        try:
            embedder = get_embedder(str(index.w.settings.embedding_model_dir))
            # Embed only scoped sources. Scope filters apply before both ranking paths.
            vectors, indexing = document_vectors(index, items, embedder)
            q, query_usage = embedder.encode([text], query=True)
            import numpy as np

            similarities = vectors @ q[0]
            if not np.isfinite(similarities).all():
                raise EmbeddingUnavailable("embedding_invalid_similarity")
            dense = sorted(
                enumerate(similarities.tolist()),
                key=lambda x: (-x[1], items[x[0]]["chunk_id"]),
            )
            meta.update(
                model=embedder.identity, indexing=indexing, query_embedding=query_usage
            )
        except (
            EmbeddingUnavailable,
            OSError,
            ValueError,
            RuntimeError,
            ImportError,
        ) as exc:
            # Explicit and observable fail-open into the unchanged keyword implementation.
            result = index.search(
                query,
                keywords,
                match,
                layout_id,
                frame_id,
                section_id,
                offset,
                limit,
                mode="keyword",
            )
            return result | {
                "retrieval": meta
                | {
                    "effective_mode": "keyword",
                    "fallback_reason": "embedding_unavailable",
                    "seconds": round(time.monotonic() - started, 4),
                }
            }
    scores, ranks = {}, {}
    for name, ranking in (
        ("lexical", lexical if mode != "semantic" else []),
        ("semantic", dense),
    ):
        # Lexical hits are pageable in full; the old top-60 cutoff made later
        # literal object names impossible to recover with any offset.
        for rank, (i, raw_score) in enumerate(ranking if name == 'lexical' else ranking[:60], 1):
            scores[i] = scores.get(i, 0.0) + 1 / (60 + rank)
            ranks.setdefault(i, {})[name] = {"rank": rank, "score": round(raw_score, 6)}
    ordered = sorted(scores, key=lambda i: (-scores[i], items[i]["chunk_id"]))
    hits, seen = [], set()
    for i in ordered:
        c = items[i]
        if c["region_id"] in seen:
            continue
        seen.add(c["region_id"])
        r = index.by_id[c["region_id"]]
        hits.append(
            {
                "region_id": r.region_id,
                "frame_id": r.frame_id,
                "layout_id": r.layout_id,
                "kind": r.kind,
                "snippet": c["text"],
                "snippet_start": c["start"],
                "text_length": len(r.text),
                "matched_terms": [
                    t for t in terms if t.casefold() in r.text.casefold()
                ],
                "match_basis": mode,
                "chunk_id": c["chunk_id"],
                "text_span": [c["start"], c["end"]],
                "parser_context_region_ids": r.context_region_ids,
                "retrieval_ranks": ranks[i],
            }
        )
    meta["seconds"] = round(time.monotonic() - started, 4)
    from cad_agent.analysis.recall import page
    return page(index, {
        "items": hits[offset : offset + limit],
        "total": len(hits),
        "offset": offset,
        "has_more": offset + limit < len(hits),
        "retrieval": meta,
        "note": "相关候选不证明答案存在；分数不是置信度。请用 read_source_context 按 snippet_start 读取原文并核对适用范围。total 是有限候选数，不是全图匹配数量。",
    }, 'search_sources', dict(query=query,keywords=keywords or [],match=match,layout_id=layout_id,
        frame_id=frame_id,section_id=section_id,offset=offset,limit=limit,mode=mode),pool_complete=mode=='lexical')
