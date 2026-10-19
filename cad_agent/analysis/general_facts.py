"""Small registered extractors. Candidate semantics never replace masonry facts."""

import re
from cad_agent.contracts import GenericFact, FactValue, stable_id
from cad_agent.analysis.general import INDEX_VERSION

EXTRACTORS = ("generic.key_value@1", "generic.table@1")
MAX_FACTS = 2000


def extract_general(index, extractor_ref, frame_ids):
    if extractor_ref not in EXTRACTORS:
        raise ValueError("未注册的通用抽取器")
    for fid in frame_ids:
        index.scope(frame_id=fid)
    sid = stable_id(
        "genericfacts",
        index.snapshot,
        extractor_ref,
        INDEX_VERSION,
        str(sorted(frame_ids)),
    )
    try:
        return index.w.store.get("generic_fact_set", sid) | {"cached": True}
    except KeyError:
        pass
    facts = []
    evidence = {}
    seen = 0
    truncated = False

    def add(
        subject,
        prop,
        value,
        sources,
        subject_sources=(),
        property_sources=(),
        scope_sources=(),
        derived=False,
    ):
        nonlocal truncated
        if len(facts) >= MAX_FACTS:
            truncated = True
            return
        all_sources = (
            list(sources)
            + list(subject_sources)
            + list(property_sources)
            + list(scope_sources)
        )
        for e in all_sources:
            evidence[e["evidence_id"]] = e
        roles = {
            k: list(dict.fromkeys(e["evidence_id"] for e in es))
            for k, es in [
                ("value", sources),
                ("subject", subject_sources),
                ("property", property_sources),
                ("scope", scope_sources),
            ]
        }
        fact = GenericFact(
            fact_id=stable_id("fact", sid, subject, prop, value, str(roles)),
            snapshot_id=index.snapshot,
            fact_schema=extractor_ref.replace("@", ".v"),
            subject=FactValue(
                value=subject,
                state="derived" if subject_sources else "unknown",
                evidence_ids=roles["subject"],
            ),
            property=FactValue(
                value=prop,
                state="derived" if derived else "observed",
                evidence_ids=roles["property"] or roles["value"],
            ),
            value=FactValue(
                value=value or None,
                state="observed" if value else "unknown",
                evidence_ids=roles["value"],
            ),
            scope_evidence_ids=roles["scope"],
            evidence_roles=roles,
            evidence_ids=list(dict.fromkeys(e["evidence_id"] for e in all_sources)),
            extractor_ref=extractor_ref,
            relation_state="derived" if derived or scope_sources else "unknown",
        )
        facts.append(fact.model_dump())

    if extractor_ref == "generic.key_value@1":
        for r in index.regions:
            if frame_ids and r.frame_id not in frame_ids:
                continue
            seen += 1
            for m in re.finditer(
                r"(?:^|\n)([^\n：:]{1,40})[：:]([^\n]{1,1000})", r.text
            ):
                label, value = m.group(1).strip(), m.group(2).strip()
                if not label or not value:
                    continue
                source = index.evidence(r, m.start(1), m.end(2))
                parents = [
                    index.evidence(index.by_id[rid])
                    for rid in r.context_region_ids
                    if rid in index.by_id
                ][:2]
                add(r.region_id, label, value, [source], scope_sources=parents)
    else:
        for t in index.tables:
            headers = t["rows"][0]["cells"]
            for row in t["rows"][1:]:
                seen += 1
                for cell in row["cells"]:
                    if frame_ids and not any(
                        index.by_id[r].frame_id in frame_ids for r in cell["region_ids"]
                    ):
                        continue
                    head = next(
                        (
                            h
                            for h in headers
                            if h["column_index"] == cell["column_index"]
                            and h["column_span"] == cell["column_span"]
                        ),
                        None,
                    )
                    first = row["cells"][0]
                    src = [
                        e
                        for eid in cell["entity_ids"]
                        for e in index.entity_evidence(eid)
                    ]
                    if not src:
                        continue
                    hs = [
                        e
                        for eid in (head or {}).get("entity_ids", [])
                        for e in index.entity_evidence(eid)
                    ]
                    ss = [
                        e
                        for eid in first["entity_ids"]
                        for e in index.entity_evidence(eid)
                    ]
                    add(
                        first["text"] or cell["cell_id"],
                        (head or {}).get("text") or f"column_{cell['column_index']}",
                        cell["text"],
                        src,
                        ss,
                        hs,
                        derived=True,
                    )
    # Duplicate strings in distinct source occurrences remain distinct facts.
    facts = list({f["fact_id"]: f for f in facts}.values())
    data = {
        "fact_set_id": sid,
        "snapshot_id": index.snapshot,
        "extractor_ref": extractor_ref,
        "facts": facts,
        "evidence": list(evidence.values()),
        "cached": False,
        "coverage": {
            "source_units_seen": seen,
            "fact_count": len(facts),
            "truncated": truncated,
            "completeness": "partial",
            "note": "仅键值文本或有边线表格候选；表头、对象及范围关系待核查，不覆盖所有设计事实。",
        },
    }
    index.w.store.put("generic_fact_set", sid, data)
    return data


def query_general(
    index,
    fact_type="generic",
    query="",
    subject="",
    property="",
    frame_id="",
    offset=0,
    limit=8,
):
    index.scope(frame_id=frame_id)
    sets = [
        s
        for s in index.w.store.list("generic_fact_set")
        if s["snapshot_id"] == index.snapshot
        and (
            fact_type == "generic" or s["extractor_ref"].replace("@", ".v") == fact_type
        )
    ]
    items = {}
    for s in sets:
        evid = {e["evidence_id"]: e for e in s["evidence"]}
        for f in s["facts"]:
            if frame_id and not any(
                evid[e]["frame_id"] == frame_id for e in f["evidence_ids"]
            ):
                continue
            if (
                subject
                and subject.casefold() not in str(f["subject"]["value"]).casefold()
            ):
                continue
            if (
                property
                and property.casefold() not in str(f["property"]["value"]).casefold()
            ):
                continue
            text = " ".join(
                str(f[k]["value"]) for k in ("subject", "property", "value")
            )
            if query and query.casefold() not in text.casefold():
                continue
            items[f["fact_id"]] = f
    rows = list(items.values())
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "has_more": offset + limit < len(rows),
        "needs_extraction": not sets,
        "fact_type": fact_type,
        "note": "通用候选事实与砌体做法分开存储；来源绑定不代表表头或范围语义正确。",
    }
