"""Task-aware wire views backed by the existing task-scoped evidence archive.

Projection only removes pure coordinate arrays. It never selects candidate
rows, truncates quotes, changes pagination, or grants credit for archived data.
The saved record is the complete tool return, not necessarily the underlying
CAD model: any upstream truncation flags remain authoritative.
"""
from collections import defaultdict
import math

from cad_agent.analysis.efficiency import dumps


VIEWS = frozenset({"auto", "summary", "full"})
COORDINATE_FIELDS = frozenset({
    "boundary", "holes", "paths", "vertices", "coordinates", "segments",
    "internal_rings", "geometry",
})
# A direct request to inspect geometry should retain its advertised detail.
DETAIL_TOOLS = frozenset({
    "read_geometry", "get_measurement_evidence", "get_room_contours",
    "inspect_spatial_boundary", "get_spatial_object", "inspect_door_sweep",
})
PROJECTABLE_TOOLS = frozenset({
    "query_spatial", "query_building_relations", "get_component_evidence",
    "get_drawing_context", "read_dimensions", "check_dimension_chain",
    "discover_rooms", "measure_geometry", "measure_room", "measure_room_area",
    "analyze_room_batch", "resolve_room_units", "select_room_boundaries",
    "prepare_room_boundaries", "analyze_measurements", "inspect_enclosure",
    "prepare_space_connections", "review_space_connections",
    "prepare_discovery_review", "review_discovered_spaces",
    "inspect_scope_objects", "bind_spatial_object", "measure_spatial_area",
    "measure_spatial_relation", "measure_door_sweep_relation",
}) | DETAIL_TOOLS
PROTECTED_FIELDS = frozenset({
    "evidence", "label_evidence", "edge_evidence", "source_evidence",
    "operation_receipts", "next_calls", "bind_next_calls", "review_calls",
    "next_actions", "subdivision_windows", "request", "arguments",
})


def _coordinate_count(value):
    """Recognize coordinate-only lists; dictionaries and mixed values stay intact."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) in (2, 3) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in value
    ):
        return 1
    counts = [_coordinate_count(v) for v in value]
    return sum(counts) if all(n is not None for n in counts) else None


def selected_view(session, name, task_ids=(), view="auto"):
    if view not in VIEWS:
        raise ValueError("result_view 必须为 auto、summary 或 full。")
    if view != "auto":
        return view
    if not session.policy.enabled or name in DETAIL_TOOLS:
        return "full"
    owners = set(task_ids)
    parts = session.task.data.get("subtasks", [])
    active = [p for p in parts if p.get("id") in owners] if owners else parts
    # Missing plan ownership is not a reason to hide geometry.
    if not active:
        return "full"
    if any(set(p.get("operations", [])) & {"contours", "geometry"} for p in active):
        return "full"
    return "summary"


def project_result(session, name, args, result, task_ids=(), view="auto"):
    """Return a projected copy or the original; persist only when bytes are saved.

    Call before observation/coverage registration. ``evidence_record`` uses
    read_evidence_record unchanged, including its task, source and hash checks.
    ``omitted_fields[].path`` uses JSON pointer syntax with ``*`` list positions.
    """
    effective_view = selected_view(session, name, task_ids, view)
    if effective_view == "full" or name not in PROJECTABLE_TOOLS:
        return result
    omitted = defaultdict(lambda: dict(occurrences=0, coordinate_points=0, chars=0))

    def walk(value, path=""):
        if isinstance(value, list):
            return [walk(v, path + "/*") for v in value]
        if not isinstance(value, dict) or ("evidence_id" in value and "quote" in value):
            return value
        out = {}
        for key, child in value.items():
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key in PROTECTED_FIELDS or key.endswith("_request"):
                out[key] = child
                continue
            count = _coordinate_count(child) if key in COORDINATE_FIELDS else None
            if count is not None and count >= 8:
                stats = omitted[pointer]
                stats["occurrences"] += 1
                stats["coordinate_points"] += count
                stats["chars"] += len(dumps(child))
            else:
                out[key] = walk(child, pointer)
        return out

    projected = walk(result)
    if not omitted:
        return result
    projected["payload_projection"] = dict(
        version="task-coordinate-view-v1", requested_view=view, effective_view=effective_view,
        omitted_fields=[dict(path=path, **stats) for path, stats in sorted(omitted.items())],
        omitted_fields_read=False, candidate_rows_omitted=0,
        complete_tool_result_archived=True,
        note="仅省略纯坐标数组；归档存在不表示已读或已核查。原工具的截断、单位、候选及覆盖限制仍有效。",
    )
    # Avoid increasing small packets with archive/manifest overhead.
    if len(dumps(result)) - len(dumps(projected)) < 1024:
        return result
    from cad_agent.analysis.evidence_store import save_session
    ref = save_session(session, name, result)
    projected["evidence_record"] = ref
    projected["payload_projection"]["result_id"] = ref["record_id"]
    projected["next_read"] = dict(
        tool="read_evidence_record", arguments=dict(record_id=ref["record_id"], start=0),
    )
    return projected
