"""Native retrieval controls: source identity, invariant shapes, hard scope and budgets."""
import math

import ezdxf
import pytest
from pydantic import ValidationError

from cad_agent.analysis.similarity_search import (
    SimilarObjectSearch, compute_similarity_search, search_similar_objects,
)
from cad_agent.cache import file_stamp
from cad_agent.contracts import stable_id


def source(tmp_path, *, extra=0, array=False, loose=False):
    doc = ezdxf.new("R2018")
    doc.units = 4
    door = doc.blocks.new("door-shape")
    door.add_line((0, 0), (10, 0))
    door.add_arc((0, 0), 10, 0, 90)
    door.add_line((0, 0), (0, 10))
    square = doc.blocks.new("different-square")
    square.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    model = doc.modelspace()
    handles = []
    if loose:
        for offset in (0, 30):
            for a, b in (((0, 0), (10, 0)), ((0, 0), (0, 10))):
                model.add_line((a[0]+offset, a[1]), (b[0]+offset, b[1]))
            model.add_arc((offset, 0), 10, 0, 90)
    else:
        for point, rotation, scale in (((0, 0), 0, 1), ((40, 0), 90, 1), ((70, 0), 37, 2)):
            item = model.add_blockref("door-shape", point, dxfattribs=dict(
                rotation=rotation, xscale=scale, yscale=scale, layer="A-DOOR"))
            handles.append(item.dxf.handle)
        model.add_blockref("different-square", (110, 0))
    if array:
        item = model.add_blockref("door-shape", (150, 0))
        item.grid(size=(1, 2), spacing=(0, 30))
        handles.append(item.dxf.handle)
    for i in range(extra):
        model.add_blockref("door-shape", (200 + i*20, 0))
    # Identical WCS locations in paper space must never leak into model candidates.
    paper = doc.layouts.new("Other")
    paper.add_blockref("door-shape", (30, 0))
    path = tmp_path / "similarity.dxf"
    doc.saveas(path)
    sid = "snap_similarity_test"
    lid = stable_id("layout", sid, "Model")
    args = SimilarObjectSearch(layout_id=lid, sample_bbox=[-1, -1, 11, 11])
    payload = dict(source_path=str(path), source_stamp=list(file_stamp(path)),
        snapshot_id=sid, source_sha256="fixture", unit="mm", args=args.model_dump(),
        search_bbox=None, index_coverage={})
    return path, payload, handles


def test_repeated_door_geometry_rotation_scale_and_different_shape(tmp_path):
    _, payload, handles = source(tmp_path)
    result = compute_similarity_search(payload)
    assert result["status"] == "ready"
    assert len(result["candidates"]) == 2
    assert {r["instance_path"][0] for r in result["candidates"]} == set(handles[1:3])
    by_root = {r["instance_path"][0]: r for r in result["candidates"]}
    rotated, scaled = by_root[handles[1]], by_root[handles[2]]
    assert rotated["bbox"] == pytest.approx([30, 0, 40, 10])
    assert rotated["source_root_transform"]["rotation_degrees"] == 90
    assert scaled["source_root_transform"]["rotation_degrees"] == 37
    assert scaled["source_root_transform"]["scale"] == [2, 2, 1]
    assert scaled["relative_path_length_scale"] == pytest.approx(2, rel=.001)
    assert scaled["relative_path_length_scale_basis"] == "approximate_flattened_path_lengths"
    assert all(r["similarity"] > .97 for r in result["candidates"])
    assert not any(r["engineering_confirmed"] for r in result["candidates"])
    assert result["method"]["learned_image_embedding"] is False


def test_real_instance_paths_highlight_and_ids_agree(tmp_path):
    _, payload, _ = source(tmp_path, array=True)
    result = compute_similarity_search(payload)
    assert len(result["candidates"]) == 4
    instance_ids = {r["instance_id"] for r in result["candidates"]}
    assert len(instance_ids) == 4
    array_objects = [r for r in result["candidates"] if len(r["instance_path"]) == 2]
    assert len(array_objects) == 2
    assert {r["bbox"][0] for r in array_objects} == {150, 180}
    for candidate in result["candidates"]:
        assert candidate["highlight"]["bbox"] == candidate["bbox"]
        assert candidate["highlight"]["entity_ids"] == candidate["source_entity_ids"]
        expected = {stable_id("ent", payload["snapshot_id"], payload["args"]["layout_id"], "/".join(p))
                    for p in candidate["source_entity_paths"]}
        assert expected == set(candidate["source_entity_ids"])
        x0, y0, x1, y1 = candidate["bbox"]
        assert all(x0-1e-8 <= x <= x1+1e-8 and y0-1e-8 <= y <= y1+1e-8
                   for path in candidate["highlight_paths"] for x, y in path)


def test_nested_instances_keep_paths_and_transformed_wcs(tmp_path):
    path, payload, _ = source(tmp_path)
    doc = ezdxf.readfile(path)
    nested = doc.blocks.new("nested-wrapper")
    nested.add_blockref("door-shape", (5, 0), dxfattribs={"rotation": 90})
    doc.modelspace().add_blockref("nested-wrapper", (160, 0))
    doc.saveas(path)
    payload["source_stamp"] = list(file_stamp(path))
    result = compute_similarity_search(payload)
    candidate = next(c for c in result["candidates"] if c["block_name"] == "nested-wrapper")
    assert len(candidate["instance_path"]) == 2
    assert all(len(p) == 3 for p in candidate["source_entity_paths"])
    assert candidate["bbox"] == pytest.approx([155, 0, 165, 10])
    # The root's transform is explicitly root-only; nested paths are already transformed.
    assert candidate["source_root_transform"]["rotation_degrees"] == 0
    assert "nested/array" in candidate["source_root_transform"]["basis"]


def test_unsupported_member_does_not_publish_partial_block(tmp_path):
    path, payload, _ = source(tmp_path)
    doc = ezdxf.readfile(path)
    doc.blocks["door-shape"].add_solid([(0, 0), (1, 0), (1, 1), (0, 1)])
    doc.saveas(path)
    payload["source_stamp"] = list(file_stamp(path))
    result = compute_similarity_search(payload)
    assert result["status"] == "sample_unresolved"
    assert result["candidates"] == []
    assert result["coverage"]["truncated"]
    assert "unsupported_or_unavailable_geometry" in result["coverage"]["truncation_reasons"]


def test_layout_and_search_bounds_isolation_preserves_whole_object(tmp_path):
    _, payload, handles = source(tmp_path)
    payload["search_bbox"] = [-1, -1, 50, 11]
    result = compute_similarity_search(payload)
    assert [r["instance_path"][0] for r in result["candidates"]] == [handles[1]]
    payload["search_bbox"] = [-1, -1, 35, 11]  # Cuts rotated candidate; cannot invent a clipped object.
    assert compute_similarity_search(payload)["candidates"] == []
    payload["search_bbox"] = None
    payload["args"]["layout_id"] = stable_id("layout", payload["snapshot_id"], "Other")
    assert compute_similarity_search(payload)["status"] == "sample_unresolved"


def test_loose_connected_geometry_repeats(tmp_path):
    _, payload, _ = source(tmp_path, loose=True)
    result = compute_similarity_search(payload)
    assert result["status"] == "ready"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["instance_id"] is None
    assert len(result["candidates"][0]["source_entity_ids"]) == 3
    assert result["candidates"][0]["bbox"] == pytest.approx([30, 0, 40, 10])


def test_scan_candidate_result_budgets_are_explicit(tmp_path):
    _, payload, _ = source(tmp_path, extra=12)
    payload["args"]["scan_budget"] = 10
    result = compute_similarity_search(payload)
    assert result["coverage"]["entities_visited"] <= 10
    assert result["coverage"]["truncated"]
    assert "scan_budget" in result["coverage"]["truncation_reasons"]
    # The third INSERT was partially expanded; it cannot become a false complete object.
    assert len(result["candidates"]) == 1
    payload["args"].update(scan_budget=4000, candidate_budget=2, limit=1)
    result = compute_similarity_search(payload)
    assert result["coverage"]["candidates_evaluated"] == 2
    assert len(result["candidates"]) == 1
    assert {"candidate_budget", "result_limit"} <= set(result["coverage"]["truncation_reasons"])
    assert not result["coverage"]["full_drawing_enumeration"]


def test_scan_truncation_cannot_publish_partial_loose_component(tmp_path):
    path, payload, _ = source(tmp_path, loose=True)
    doc = ezdxf.readfile(path)
    # A chain reaches the sample only after the scan budget; its early fragment must not
    # be claimed as a complete object when the rest of the layout remains unread.
    for i in range(20):
        doc.modelspace().add_line((100+i, 0), (101+i, 0))
    doc.saveas(path)
    payload["source_stamp"] = list(file_stamp(path))
    payload["args"]["scan_budget"] = 10
    result = compute_similarity_search(payload)
    assert result["status"] == "sample_unresolved"
    assert result["coverage"]["objects_skipped"] > 0
    assert result["candidates"] == []


def test_worker_rejects_sample_outside_search_scope(tmp_path):
    _, payload, _ = source(tmp_path)
    payload["search_bbox"] = [30, -1, 50, 11]
    with pytest.raises(ValueError, match="有效搜索范围"):
        compute_similarity_search(payload)


def test_sample_crossing_and_empty_samples_are_unresolved(tmp_path):
    _, payload, _ = source(tmp_path)
    payload["args"]["sample_bbox"] = [-1, -1, 5, 5]
    assert compute_similarity_search(payload)["status"] == "sample_unresolved"
    payload["args"]["sample_bbox"] = [200, 200, 201, 201]
    assert compute_similarity_search(payload)["status"] == "sample_unresolved"


def test_surrounding_frame_does_not_invalidate_inside_sample(tmp_path):
    path, payload, _ = source(tmp_path)
    doc = ezdxf.readfile(path)
    doc.modelspace().add_lwpolyline([(-10, -10), (200, -10), (200, 100), (-10, 100)], close=True)
    doc.saveas(path)
    payload["source_stamp"] = list(file_stamp(path))
    result = compute_similarity_search(payload)
    assert result["status"] == "ready" and len(result["candidates"]) == 2


def test_stale_file_and_invalid_boxes_fail_closed(tmp_path):
    path, payload, _ = source(tmp_path)
    payload["source_stamp"] = [0, 0]
    with pytest.raises(ValueError, match="来源文件"):
        compute_similarity_search(payload)
    for bounds in ([0, 0, 0, 1], [0, 0, math.inf, 1], [1, 0, 0, 1]):
        with pytest.raises(ValidationError):
            SimilarObjectSearch(layout_id="model", sample_bbox=bounds)


def test_workspace_worker_frame_validation_and_read_only(tmp_path, workspace):
    path, _, _ = source(tmp_path)
    drawing = workspace.ingest("similarity.dxf", path.read_bytes(), synthetic=True)
    sid = drawing["snapshot_id"]
    lid = next(r["layout_id"] for r in drawing["layouts"] if r["name"] == "Model")
    drawing["frames"] = [dict(frame_id="frame_test", layout_id=lid, bbox=[-2, -2, 50, 12])]
    workspace.store.put("drawing", sid, drawing)
    original = workspace.store.get("drawing", sid)
    result = search_similar_objects(workspace, sid, SimilarObjectSearch(
        layout_id=lid, frame_id="frame_test", sample_bbox=[-1, -1, 11, 11]))
    assert len(result["candidates"]) == 1
    assert result["search_bbox"] == [-2, -2, 50, 12]
    assert workspace.store.get("drawing", sid) == original
    assert workspace.geometry_executor.last_worker_pid is not None
    for changes in (dict(layout_id="missing"), dict(frame_id="missing"),
                    dict(sample_bbox=[-3, -3, 11, 11]), dict(search_bbox=[100, 100, 110, 110]),
                    dict(search_bbox=[20, -1, 40, 11])):
        args = dict(layout_id=lid, frame_id="frame_test", sample_bbox=[-1, -1, 11, 11])
        args.update(changes)
        with pytest.raises(ValueError):
            search_similar_objects(workspace, sid, args)


def test_metadata_does_not_rescue_different_shape(tmp_path):
    _, payload, _ = source(tmp_path)
    payload["args"]["min_similarity"] = 0
    result = compute_similarity_search(payload)
    square = next(c for c in result["candidates"] if c["block_name"] == "different-square")
    assert square["similarity"] < .78


def test_worker_timeout_respects_existing_request_budget():
    from types import SimpleNamespace
    from cad_agent.geometry.compute import compute, remaining_budget
    calls = []
    class Recorder:
        timeout = 120
        def compute(self, operation, payload, timeout=None):
            calls.append(timeout)
            return {"ok": True}
    w = SimpleNamespace(geometry_executor=Recorder())
    with remaining_budget(lambda: 12):
        assert compute(w, "similarity_search", {}, timeout=20)["ok"]
    assert calls == [7]
