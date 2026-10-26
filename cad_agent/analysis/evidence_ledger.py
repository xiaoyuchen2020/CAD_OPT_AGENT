"""Session-owned observation registration with source-stable identities."""
import json
import re
from cad_agent.contracts import stable_id
from cad_agent.evidence import bind_evidence
from cad_agent.cad.regions import is_heading, clause_level



def register_observation(self, name, result):
    data = None
    if name in ('extract_planning_metrics', 'extract_building_schedule'):
        data = {k:v for k,v in result.items() if k not in ('observation_id', 'cache_hit')}
    if name in ('search_tables','inspect_table','read_table_content','recover_table_region','review_table_region'):
        data = {k:v for k,v in result.items() if k!='observation_id'}
    from cad_agent.analysis.tool_metadata import SPATIAL_METHODS
    if name in SPATIAL_METHODS | {'bind_spatial_object', 'get_spatial_object'}:
        data = {k: v for k, v in result.items() if k != 'observation_id'}
    if name in ('analyze_room_batch','resolve_room_units'):
        data = {k:v for k,v in result.items() if k not in ('scope_evidence', 'instruction')}
        data['items'] = [{k:v for k,v in i.items() if k != 'label_evidence'} for i in result['items']]
    if name in ('query_annotations', 'locate_by_axes', 'trace_detail', 'read_table_semantics', 'read_door_schedule_table', 'trace_door_schedule', 'search_similar_objects'):
        data = {k:v for k,v in result.items() if k != 'observation_id'}
    if name in ("get_drawing_context", "read_dimensions", "query_building_relations", "get_component_evidence"):
        data = {k:v for k,v in result.items() if k != "observation_id"}
    elif name in ('render_region', 'bind_visual_objects', 'prepare_room_boundaries', 'select_room_boundaries'):
        data = {k:v for k,v in result.items() if k != 'delivery'}
        if name=='select_room_boundaries':
            from cad_agent.analysis.boundary_context import observation_payload
            data=observation_payload(data)
    elif name in ("measure_geometry", "measure_room", "get_measurement_evidence"):
        data = {k:v for k,v in result.items() if k not in ("label_evidence", "boundary", "holes", "source_entity_ids")}
    elif name in ("inspect_scope_objects", "read_geometry", "query_spatial", "discover_rooms", "get_room_contours", "measure_room_area"):
        data = {k:v for k,v in result.items() if k != "observation_id"}
    elif name == "get_drawing_overview":
        data = {
            k: result[k]
            for k in ("file_metadata", "total", "view", "has_more")
            if k in result
        }
    elif name in ("extract_facts", "query_facts", "search_sources"):
        data = {
            k: result[k]
            for k in (
                "fact_set_id",
                "extractor_ref",
                "fact_count",
                "practice_count",
                "total",
                "coverage",
                "has_more",
                "needs_extraction",
            )
            if k in result
        }
    elif name == "read_table":
        data = {
            k: result[k]
            for k in ("table_id", "row_count", "column_count", "header_state")
        }
        data["cells"] = [
            {k: c[k] for k in ("cell_id", "column_index", "column_span", "bbox")}
            | {
                "row_index": r["row_index"],
                "display_row": r["row_index"] + 1,
                "display_column": c["column_index"] + 1,
                "empty": not bool(c["text"]),
            }
            for r in result["items"]
            for c in r["cells"]
        ]
    elif name in ("query_entities","locate_space_labels"):
        data = {
            **{k:result[k] for k in ('total','has_more','offset','unit','scope','scope_choices','enumerated','name_counts') if k in result},
            "items": [
                {
                    k: e[k]
                    for k in (
                        "entity_id",
                        "kind",
                        "layer",
                        "block_name",
                        "entity_path",
                        "bbox",
                        "layout_id",
                        "text",
                        "source_region_ids",
                        "table_candidate_ids",
                    )
                    if k in e
                }
                for e in result["items"]
            ],
        }
    elif name == "inspect_region":
        data = {
            k: result[k]
            for k in ("region_id", "bbox", "preview_url", "visual_input_to_model")
        }
        data["nearby_entities"] = [
            {k: e[k] for k in ("entity_id", "kind", "layer", "entity_path", "bbox")}
            for e in result["nearby"]["items"]
        ]
    if name in ("get_analysis_results","analyze_measurements","check_dimension_chain","inspect_enclosure","prepare_space_connections","review_space_connections","prepare_discovery_review","review_discovered_spaces"):
        data={k:v for k,v in result.items() if k not in ("highlight_action",)}
    if name in ('calculate_facts','resolve_drawing_scope','read_project_sources','read_assembly','select_source_records'):data={k:v for k,v in result.items() if k!='observation_id'}
    if data is not None:
        snapshot_id = data.get("snapshot_id", self.request.snapshot_id) if name in ('read_door_schedule_table', 'read_assembly', 'extract_planning_metrics', 'extract_building_schedule') else self.request.snapshot_id
        oid = stable_id(
            "obs", snapshot_id, name, json.dumps(data, sort_keys=True)
        )
        observation = {
            "observation_id": oid,
            "snapshot_id": snapshot_id,
            "tool": name,
            "data": data,
            "basis": "tool_observation",
            "note": "解析器读回或计算的元数据，不是原图原文证据，不证明设计语义。",
        }
        self.observations[oid] = observation
        result["observation_id"] = oid
        result["observation_note"] = observation["note"]



def numbered_continuations(peers, target):
    if not target.text.rstrip().endswith(('：', ':')):
        return []
    ordered = sorted(peers, key=lambda r: (-r.bbox[3], r.bbox[0], r.region_id))
    rows = []
    expected = 1
    for row in ordered[ordered.index(target)+1:]:
        match = re.match(r'\s*(\d+)[）)]', row.text)
        if not match or int(match[1]) != expected:
            break
        rows.append(row)
        expected += 1
        if expected > 24:
            break
    return rows


def source_context(workspace, snapshot_id, region_id, neighbors=1, start=0, bounded=False):
    """Only source-derived IDs/spans/boxes; never infer scope from proximity."""
    drawing = workspace.store.get("drawing", snapshot_id)
    regions = workspace.regions(snapshot_id)
    by_id = {r.region_id: r for r in regions}
    order = {r.region_id: i for i, r in enumerate(regions)}
    target = by_id.get(region_id)
    if target is None:
        raise ValueError("来源区域不属于当前图纸。")
    if not 0 <= neighbors <= 2 or not 0 <= start < len(target.text):
        raise ValueError("上下文范围或字符起点无效。")
    peers = [
        r
        for r in regions
        if (r.layout_id, r.frame_id, r.column_key)
        == (target.layout_id, target.frame_id, target.column_key)
    ]
    index = peers.index(target)
    related = [
        by_id[rid]
        for rid in target.context_region_ids
        if rid in by_id
        and by_id[rid].layout_id == target.layout_id
        and by_id[rid].frame_id == target.frame_id
    ]
    candidates = [(target, "target")] + [(r, "parser_context") for r in related[:4]]
    # An introductory colon followed by an ordered list is a source continuation,
    # not an arbitrary sibling/nearest-title attribution. Read its entries before
    # unrelated same-section siblings consume the bounded response.
    candidates += [(row, 'numbered_list_continuation') for row in numbered_continuations(peers, target)]
    if is_heading(target.text):
        candidates += [
            (r, "section_child")
            for r in peers
            if target.region_id in r.context_region_ids
        ]
    if target.context_region_ids and not bounded:
        candidates += [
            (r, "sibling")
            for r in peers
            if r != target and r.context_region_ids == target.context_region_ids
        ]
    candidates += [
        (r, "neighbor")
        for r in peers[max(0, index - neighbors) : index + neighbors + 1]
        if r != target
    ]
    # Include each visible neighbor's own parent, so a following heading cannot
    # silently take ownership of the preceding section's last clause.
    visible = list(candidates)
    for r, _ in visible:
        candidates += [
            (by_id[rid], "parser_context")
            for rid in r.context_region_ids[:4]
            if rid in by_id
            and by_id[rid].layout_id == target.layout_id
            and by_id[rid].frame_id == target.frame_id
        ]
    seen, items = set(), []
    for r, relationship in candidates:
        if r.region_id in seen:
            continue
        if len(items) >= 12:
            break
        seen.add(r.region_id)
        begin = start if relationship == "target" else 0
        budget = (2000 if relationship == "target" else 600) if bounded else (6000 if relationship == "target" else 1500)
        end = min(len(r.text), begin + budget)
        if end <= begin:
            continue
        evidence = bind_evidence(
            snapshot_id, r, begin, end, drawing["unit"]
        ).model_dump()
        items.append(
            {
                "region_id": r.region_id,
                "kind": r.kind,
                "frame_id": r.frame_id,
                "frame_name": r.frame_name,
                "relationship": relationship,
                "relation_kind": (
                    "parent"
                    if relationship == "parser_context"
                    and r.region_id in target.context_region_ids
                    else relationship
                ),
                "relation_basis": (
                    "spatial_reading_order"
                    if relationship == "neighbor"
                    else "parser_structure"
                ),
                "relation_state": "candidate",
                "reading_order": order[r.region_id],
                "position_relative_to_target": (
                    "target"
                    if r == target
                    else (
                        "before"
                        if order[r.region_id] < order[target.region_id]
                        else "after"
                    )
                ),
                "parser_context_region_ids": r.context_region_ids,
                "is_heading": is_heading(r.text),
                "clause_level": clause_level(r.text),
                "text": evidence["quote"],
                "text_span": [begin, end],
                "truncated": begin > 0 or end < len(r.text),
                "next_start": end if end < len(r.text) else None,
                "evidence": evidence,
            }
        )
    next_actions = [
        {"tool": "read_source_context", "arguments": {
            "region_id": item["region_id"], "neighbors": 0, "start": item["next_start"]}}
        for item in items if item["next_start"] is not None
    ]
    unit_candidates = []
    from cad_agent.analysis.text_units import read_elevation_annotation
    elevation_annotation = read_elevation_annotation(target)
    if re.search(r'\d\s*(?:(?:kg|g)\s*/\s*)?(?:mm|cm|m)(?![A-Za-z])', target.text):
        from cad_agent.analysis.general import DrawingIndex
        from cad_agent.analysis.text_units import read_unit_candidates
        unit_candidates = read_unit_candidates(DrawingIndex(workspace, snapshot_id), target)
    omitted = list(dict.fromkeys(r.region_id for r, _ in candidates if r.region_id not in seen))
    next_actions += [{"tool": "read_source_context", "arguments": {
        "region_id": rid, "neighbors": 0, "start": 0}} for rid in omitted[:4]]
    if omitted:
        next_actions.append({"tool": "read_assembly", "arguments": {"region_id": region_id, "offset": 0, "limit": 12}})
    # Reading a thickness can discover a unit convention without changing the
    # numerical source quote. The model must explicitly bind this evidence ID.
    default_units = []
    if re.search(r'\d+\s*厚|厚度|长度|宽度|高度', target.text):
        from cad_agent.analysis.general import DrawingIndex
        from cad_agent.analysis.default_units import candidates as unit_clauses
        idx = DrawingIndex(workspace, snapshot_id)
        clauses = unit_clauses(idx, target)
        for row in clauses[:3]:
            default_units.append(dict(evidence=idx.evidence(row),
                applicability='requires_explicit_unit_evidence_binding',
                next_action=dict(tool='read_source_context', arguments=dict(region_id=row.region_id, neighbors=0))))
    return {
        "snapshot_id": snapshot_id,
        "target_region_id": region_id,
        "items": items,
        "omitted_context_region_count": len(omitted),
        "unit_candidates": unit_candidates,
        "default_unit_clauses": default_units,
        "elevation_annotation": elevation_annotation,
        "has_more": bool(next_actions),
        "next_actions": next_actions,
        "note": "邻近关系不证明适用范围；表头与条款之间的语义关系仍需核查。",
    }


def compact_evidence(e):
    from cad_agent.cad.numbers import evidence_readings, VERSION
    result = {k:e[k] for k in ("evidence_id","snapshot_id","region_id","quote","text_span","entity_ids")}
    atoms = evidence_readings(e)
    if atoms:
        # The full provenance stays on the evidence record. Do not repeat raw
        # text, identities and source geometry in every model context.
        result['numeric_readings'] = dict(version=VERSION, items=[
            {k:t[k] for k in ('number_id','kind','value','text_span','status')} for t in atoms])
    return result


def compact_coverage(coverage):
    result = {
        k: coverage[k]
        for k in (
            "completeness",
            "source_units_seen",
            "source_units_resolved",
            "candidate_units",
            "note",
        )
        if k in coverage
    }
    for key in ("unresolved_region_ids", "skipped_region_ids"):
        result[key.replace("_ids", "_count")] = len(coverage.get(key, []))
    return result
