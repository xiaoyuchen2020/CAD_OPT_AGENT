"""Lossless local records, with explicit, bounded readback and source bindings.

An archive reference is not evidence credit. Original source/measurement tools
remain responsible for registration, coverage, units and publication checks.
"""
import hashlib
import json
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.analysis.efficiency import dumps


class EvidenceRecordRead(Record):
    view: Literal["read", "catalog"] = "read"
    record_id: str = Field(default="", pattern=r"^(archive_[0-9a-f]{64})?$")
    start: int = Field(default=0, ge=0)
    length: int = Field(default=6000, ge=1, le=12000)


def save(workspace, snapshot_ids, kind, value):
    sources = {sid: workspace.store.get("drawing", sid)["sha256"] for sid in sorted(snapshot_ids)}
    text = dumps(value)
    digest = hashlib.sha256(text.encode()).hexdigest()
    key = "archive_" + hashlib.sha256(dumps([sources, kind, digest]).encode()).hexdigest()
    workspace.store.put("evidence_archive", key, dict(
        record_id=key, sources=sources, kind=kind, sha256=digest, text=text,
        total_chars=len(text), snapshot_id=next(iter(sources))))
    return dict(record_id=key, sha256=digest, total_chars=len(text),
                read_tool="read_evidence_record", complete_record=True)


def save_session(session, kind, value):
    ref = save(session.w, session.allowed_snapshots, kind, value)
    refs = session.task.data.setdefault("evidence_records", {})
    refs[ref["record_id"]] = ref
    session.task.save()
    return ref


def read(session, args):
    return read_task_record(session.w, session.task.data, session.allowed_snapshots, args)


def read_task_record(workspace, task, allowed_snapshots, args):
    if args.view == "catalog":
        refs = list(task.get("evidence_records", {}).values())
        end = min(len(refs), args.start + 12)
        return dict(items=refs[args.start:end], total=len(refs), has_more=end < len(refs),
                    next_start=end if end < len(refs) else None, note="记录目录不提供读取或完成信用。")
    # Capability is granted by a result visible in this task, not knowledge of an ID.
    if args.record_id not in task.get("evidence_records", {}):
        raise ValueError("证据记录尚未在本任务开放；请先读取对应来源或几何。")
    record = workspace.store.get("evidence_archive", args.record_id)
    if not set(record["sources"]) <= allowed_snapshots:
        raise ValueError("证据记录不属于本轮选定图纸。")
    for sid, sha in record["sources"].items():
        if workspace.store.get("drawing", sid)["sha256"] != sha:
            raise ValueError("证据记录的图纸版本已改变，请重新读取来源。")
    text = record["text"]
    if hashlib.sha256(text.encode()).hexdigest() != record["sha256"]:
        raise ValueError("证据记录完整性校验失败。")
    if args.start > len(text):
        raise ValueError("分页起点超过记录长度。")
    end = min(len(text), args.start + args.length)
    return dict(record_id=args.record_id, sha256=record["sha256"], kind=record["kind"],
                sources=record["sources"], format="json_text_fragment", start=args.start,
                end=end, total_chars=len(text), content=text[args.start:end],
                has_more=end < len(text), next_start=end if end < len(text) else None,
                note="完整本地记录的字符分页；不是整图覆盖证明，也不新增测量或引用资格。")


def working_result(value, ref, limit=18000, *, assembly_pages=True):
    """Keep small packets intact; large results have an honest readback cursor."""
    if value.get('kind') in ('planning_metrics','building_schedule') and value.get('mode')=='extraction':
        from cad_agent.analysis.business_table_context import project
        packet=project(value,include_sources=True)
        # Canonical fields and all complete source quotes stay together. Never
        # replace an otherwise bounded business page by a 4000-char JSON prefix.
        if len(dumps(packet))<=45000:
            return packet | {'evidence_record':ref}
    if len(dumps(value)) <= limit:
        return value | {"evidence_record": ref}
    keys = ("observation_id", "measurement_id", "batch_id", "status", "kind",
            "total", "has_more", "next_offset", "next_start",
            "scope_id", "unit", "unit_resolution", "geometry_complete",
            "original_source_record", "full_geometry_record",
            "object_id", "other_object_id", "contour_id", "window_id",
            "snapshot_id", "source_sha256", "layout_id", "frame_id",
            "spatial_fact_scope", "engineering_confirmed", "source_geometry_complete",
            "conditional", "role", "coverage", "measurement_basis")
    out = {k: value[k] for k in keys if k in value}
    # Metadata itself can be large; its full version remains in the archive.
    if len(dumps(out)) > limit // 2:
        out = {k: value[k] for k in (
            "observation_id", "measurement_id", "status", "object_id", "other_object_id",
            "contour_id", "window_id", "scope_id", "snapshot_id", "source_sha256",
            "engineering_confirmed", "source_geometry_complete", "conditional",
        ) if k in value}
    # Preserve usable continuation calls, not fragments of executable arguments.
    # All groups share one small allowance; omitted entries remain in the archive.
    remaining = min(3000, limit // 6)
    continuation = {}
    for key in ("next_calls", "bind_next_calls", "review_calls", "subdivision_windows", "next_actions"):
        entries = value.get(key)
        if not isinstance(entries, list):
            continue
        selected = []
        for item in entries[:3]:
            cost = len(dumps(item))
            if cost > remaining:
                break
            selected.append(item)
            remaining -= cost
        out[key] = selected
        continuation[key] = dict(shown=len(selected), total=len(entries),
                                 has_more=len(selected) < len(entries), record_id=ref["record_id"])
    if continuation:
        out["continuation_excerpt"] = continuation
    out.update(evidence_record=ref, result_excerpt=True, original_result_chars=len(dumps(value)),
               next_read=dict(tool="read_evidence_record", arguments=dict(record_id=ref["record_id"], start=0)),
               note="大结果已完整保存在证据存储。按需分页回读；未读内容不能作为已核对事实。")
    # A context page is a structured field list, not a prefix of arbitrary JSON.
    # Keep its complete visible page when duplicate evidence is the only bulk.
    if value.get('kind') == 'context' and 'items' in value:
        from cad_agent.analysis.efficiency import compact
        packet = compact(value)
        if len(dumps(packet)) <= 45000:
            return packet | {'evidence_record': ref}
    # Assembly pages must not become a truncated JSON prefix: that loses the
    # ownership boundary/units and repeats quotes inside an escaped string.
    if assembly_pages and str(value.get('version', '')).startswith('assembly-source-') and 'items' in value:
        from cad_agent.analysis.delivery_context import assembly_observation
        from cad_agent.analysis.evidence_ledger import compact_evidence
        sources = {}
        def collect_sources(v):
            if isinstance(v, dict):
                if 'evidence_id' in v and 'quote' in v and 'snapshot_id' in v:
                    sources[v['evidence_id']] = compact_evidence(v)
                for child in v.values():
                    collect_sources(child)
            elif isinstance(v, list):
                for child in v:
                    collect_sources(child)
        collect_sources(value)
        packet = assembly_observation({'tool': 'read_assembly', 'data': value})['data']
        packet.update({k: value[k] for k in ('observation_id', 'snapshot_id', 'version') if k in value})
        # Mixed tasks/calculators still need the typed per-layer operands.
        packet['items'] = [dict(item, **{k: original[k] for k in ('fragments',) if k in original})
                           for item, original in zip(packet['items'], value['items'])]
        packet.update(evidence=list(sources.values()), evidence_record=ref,
                      result_projection='assembly_page', original_result_chars=len(dumps(value)))
        return packet
    # Structured tables retain usable cells and whole quotes once, with field
    # references instead of duplicating every cell in a second records tree.
    if 'header_rows' in value and 'records' in value and 'items' in value:
        from cad_agent.analysis.efficiency import compact
        packet = dict(value)
        packet['records'] = [dict(r, fields=[{'cell_id': f['cell_id']} for f in r['fields']])
                             for r in value['records']]
        packet = compact(packet)
        packet.update(evidence_record=ref, cell_reference_basis='items.cells',
                      original_result_chars=len(dumps(value)))
        if len(dumps(packet)) <= 45000:
            return packet
    # A bounded prefix assists navigation without silently claiming completeness.
    out["preview_json"] = dumps(value)[:4000]
    # Whole source quotes are retained whenever the projected packet is used.
    # If they do not fit, keep the existing explicit oversized-result handling.
    from cad_agent.analysis.efficiency import compact
    quotes = {}
    def collect(v):
        if isinstance(v, dict):
            if "evidence_id" in v and "quote" in v:
                quotes[v["evidence_id"]] = compact(v)
            for child in v.values():collect(child)
        elif isinstance(v, list):
            for child in v:collect(child)
    collect(value)
    out["evidence"] = list(quotes.values())
    if len(dumps(out)) > limit:
        return value | {"evidence_record": ref}
    return out
