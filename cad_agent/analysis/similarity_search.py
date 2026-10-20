"""Read-only, bounded native-shape retrieval. Similarity never confirms object identity."""
from collections import Counter, defaultdict
from itertools import islice
from pathlib import Path
import math
import time

import ezdxf
from ezdxf import bbox as cad_bbox
from ezdxf.path import make_path
from pydantic import Field, model_validator
from shapely.geometry import LineString, box

from cad_agent.cache import file_stamp
from cad_agent.cad.reader import walk_entities, decode_text, union_box
from cad_agent.contracts import Record, stable_id

VERSION = "native-shape-similarity-v1"
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_POINTS = 100_000
MAX_OBJECT_POINTS = 4096
MAX_SOURCE_IDS = 256
TEXT_KINDS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
SHAPE_KINDS = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}


class SimilarObjectSearch(Record):
    layout_id: str = Field(min_length=1, max_length=200)
    sample_bbox: list[float] = Field(min_length=4, max_length=4,
        description="框选样本的 DXF WCS [xmin,ymin,xmax,ymax]，须完整包含样本。")
    frame_id: str = Field(default="", max_length=200)
    search_bbox: list[float] | None = Field(default=None, min_length=4, max_length=4,
        description="可选候选范围，DXF WCS [xmin,ymin,xmax,ymax]；与frame交集生效。")
    limit: int = Field(default=12, ge=1, le=50)
    scan_budget: int = Field(default=4000, ge=10, le=20000)
    candidate_budget: int = Field(default=400, ge=1, le=2000)
    min_similarity: float = Field(default=.78, ge=0, le=1)

    @model_validator(mode="after")
    def check_boxes(self):
        for value in (self.sample_bbox, self.search_bbox):
            if value is not None and (not all(math.isfinite(v) for v in value)
                    or value[0] >= value[2] or value[1] >= value[3]):
                raise ValueError("需要有限且有面积的 DXF WCS bbox。")
        return self


def _contains(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def _intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _intersection(a, b):
    result = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError("搜索范围与所选图框没有面积交集。")
    return result


def search_similar_objects(workspace, snapshot, args):
    """API/agent entry point. ``snapshot`` is a snapshot ID; no stores are mutated."""
    args = SimilarObjectSearch.model_validate(args) if isinstance(args, dict) else args
    drawing = workspace.store.get("drawing", snapshot)
    if args.layout_id not in {r["layout_id"] for r in drawing["layouts"]}:
        raise ValueError("布局不属于当前快照。")
    bounds = args.search_bbox
    frame_bbox = None
    if args.frame_id:
        frame = next((f for f in drawing.get("frames", []) if f["frame_id"] == args.frame_id), None)
        if frame is None or frame["layout_id"] != args.layout_id:
            raise ValueError("图框不属于当前快照和布局。")
        frame_bbox = frame["bbox"]
        if not _contains(frame_bbox, args.sample_bbox):
            raise ValueError("样本框必须位于所选图框内。")
        bounds = _intersection(bounds, frame_bbox) if bounds else frame_bbox
    if bounds is not None and not _contains(bounds, args.sample_bbox):
        raise ValueError("样本框必须位于有效搜索范围内。")
    path = workspace.store.artifact(snapshot, "source.dxf")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("源DXF超过相似搜索32 MiB读取预算，未启动扫描。")
    from cad_agent.geometry.compute import compute
    return compute(workspace, "similarity_search", dict(source_path=str(path.resolve()),
        source_stamp=list(file_stamp(path)), snapshot_id=snapshot,
        source_sha256=drawing["sha256"], unit=drawing["unit"],
        index_coverage=drawing.get("index_coverage", {}),
        args=args.model_dump(), search_bbox=bounds, frame_bbox=frame_bbox), timeout=20)


def _shape(entity, path, sid, lid):
    """Actual source extents plus bounded approximate paths, never broad-phase circle boxes."""
    ext = cad_bbox.extents([entity], fast=False)
    if not ext.has_data:
        return None
    bounds = [float(ext.extmin.x), float(ext.extmin.y), float(ext.extmax.x), float(ext.extmax.y)]
    if not all(math.isfinite(v) for v in bounds):
        return None
    extent = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    if extent <= 0:
        return None
    points = [[float(v.x), float(v.y)] for v in
              islice(make_path(entity).flattening(distance=extent / 128, segments=8), 1025)]
    if len(points) < 2 or len(points) > 1024 or not all(math.isfinite(v) for p in points for v in p):
        return None
    return dict(entity_id=stable_id("ent", sid, lid, "/".join(path)), entity_path=path,
                kind=entity.dxftype(), layer=entity.dxf.get("layer", "0"), bbox=bounds,
                points=points)


def _groups(records):
    """Group INSERT leaves by their exact instance prefix; loose paths by shared endpoints."""
    blocks, loose = defaultdict(list), []
    for record in records:
        if len(record["entity_path"]) > 1:
            blocks[tuple(record["entity_path"][:-1])].append(record)
        else:
            loose.append(record)
    parents = list(range(len(loose)))
    def root(i):
        while i != parents[i]:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    endpoints = {}
    for i, record in enumerate(loose):
        for point in (record["points"][0], record["points"][-1]):
            key = tuple(round(v, 6) for v in point)
            if key in endpoints:
                parents[root(i)] = root(endpoints[key])
            else:
                endpoints[key] = i
    connected = defaultdict(list)
    for i, record in enumerate(loose):
        connected[root(i)].append(record)
    return list(blocks.items()) + [((), rs) for rs in connected.values()]


def _descriptor(records):
    """32 arc-length samples; sorted pair distances are translation/rotation/scale invariant."""
    segments = []
    for record in records:
        for a, b in zip(record["points"], record["points"][1:]):
            length = math.dist(a, b)
            if length > 0:
                segments.append((a, b, length))
    total = sum(s[2] for s in segments)
    if not total:
        return None
    sampled, segment, offset = [], 0, 0.
    for i in range(32):
        distance = total * (i + .5) / 32
        while segment < len(segments)-1 and offset + segments[segment][2] < distance:
            offset += segments[segment][2]
            segment += 1
        a, b, length = segments[segment]
        fraction = (distance - offset) / length
        sampled.append([a[0] + fraction*(b[0]-a[0]), a[1] + fraction*(b[1]-a[1])])
    distances = sorted(math.dist(a, b) for i, a in enumerate(sampled) for b in sampled[i+1:])
    scale = distances[-1]
    if scale <= 0:
        return None
    # Source curve-kind counts complement distributional geometry; they are not semantics.
    return dict(distances=[d/scale for d in distances], kinds=Counter(r["kind"] for r in records),
                perimeter=total)


def _score(sample, candidate, a, b):
    delta = sum(abs(x-y) for x, y in zip(a["distances"], b["distances"])) / len(a["distances"])
    geometry = math.exp(-14 * delta)
    kinds = set(a["kinds"]) | set(b["kinds"])
    composition = sum(min(a["kinds"][k], b["kinds"][k]) for k in kinds) / max(
        1, sum(max(a["kinds"][k], b["kinds"][k]) for k in kinds))
    same_block = bool(sample["block_name"] and sample["block_name"] == candidate["block_name"])
    shared_layer = bool(set(sample["layers"]) & set(candidate["layers"]))
    shared_text = bool(set(sample["texts"]) & set(candidate["texts"]))
    # Weak metadata cannot turn a geometrically poor match into a high-scoring candidate.
    base = .85 * geometry + .15 * composition
    score = base * (.95 + .025*same_block + .015*shared_layer + .01*shared_text)
    return round(min(1., score), 6), dict(geometry=round(geometry, 6),
        primitive_composition=round(composition, 6), same_block_name=same_block,
        shared_layer=shared_layer, exact_shared_text=shared_text)


def _object(records, instance_path, sid, lid, root_info, texts):
    bounds = union_box(r["bbox"] for r in records)
    ids = sorted(r["entity_id"] for r in records)
    oid = stable_id("similar_object", sid, lid, "/".join(instance_path), ",".join(ids))
    transform = root_info.get(instance_path[0], {}) if instance_path else {}
    return dict(object_id=oid, instance_id=stable_id("instance", sid, lid, "/".join(instance_path))
                if instance_path else None, instance_path=list(instance_path),
        source_entity_ids=ids, entity_ids=ids,
        source_entity_paths=[r["entity_path"] for r in records],
        snapshot_id=sid, layout_id=lid, bbox=bounds, coordinate_space="dxf_wcs",
        bbox_basis="native_source_curve_extents", actual_size_wcs=[bounds[2]-bounds[0], bounds[3]-bounds[1]],
        source_root_transform=transform, block_name=transform.get("block_name", ""),
        layers=sorted({r["layer"] for r in records}),
        texts=texts.get(tuple(instance_path), [])[:12],
        identity_status="similarity_candidate_unverified", engineering_confirmed=False,
        highlight=dict(layout_id=lid, bbox=bounds, entity_ids=ids, coordinate_space="dxf_wcs"),
        component_boxes=[r["bbox"] for r in records],
        highlight_paths=[r["points"] for r in records])


def compute_similarity_search(payload):
    """Pure isolated worker operation; trusted path originates in Store.artifact."""
    source_path = Path(payload["source_path"])
    stamp = payload["source_stamp"]
    if list(file_stamp(source_path)) != stamp:
        raise ValueError("相似搜索来源文件已变化，请重新读取。")
    if source_path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("源DXF超过相似搜索读取预算。")
    args = SimilarObjectSearch.model_validate(payload["args"])
    sid, lid = payload["snapshot_id"], args.layout_id
    started = time.monotonic()
    doc = ezdxf.readfile(source_path)
    layout = next((l for l in doc.layouts if stable_id("layout", sid, l.name) == lid), None)
    if layout is None:
        raise ValueError("源DXF中不存在所选布局。")
    records, texts, root_info, diagnostics = [], defaultdict(list), {}, []
    counter, point_count, roots_scanned = [0], 0, 0
    reasons, incomplete_prefixes = set(), set()
    total_roots = len(layout)
    bounds = payload.get("search_bbox")
    if bounds is not None and not _contains(bounds, args.sample_bbox):
        raise ValueError("样本框必须位于有效搜索范围内。")
    for ordinal, source_root in enumerate(layout):
        if counter[0] >= args.scan_budget or time.monotonic()-started > 12:
            reasons.add("scan_budget" if counter[0] >= args.scan_budget else "time_budget")
            break
        roots_scanned += 1
        token = str(source_root.dxf.get("handle") or f"{ordinal}:{source_root.dxftype()}")
        if source_root.dxftype() == "INSERT":
            root_info[token] = dict(block_name=source_root.dxf.name,
                rotation_degrees=float(source_root.dxf.get("rotation", 0)),
                scale=[float(source_root.dxf.get(k, 1)) for k in ("xscale", "yscale", "zscale")],
                insert=list(source_root.dxf.insert),
                basis="source_root_INSERT; nested/array leaf geometry already transformed to WCS")
        current, issues = [], []
        for entity, path in walk_entities((source_root,), issues, layout.name,
                counter=counter, entity_limit=args.scan_budget, start_index=ordinal):
            kind = entity.dxftype()
            if kind in TEXT_KINDS:
                if len(path) > 1 and len(texts[tuple(path[:-1])]) < 12:
                    texts[tuple(path[:-1])].append(decode_text(entity)[:160])
                continue
            if kind not in SHAPE_KINDS:
                diagnostics.append(dict(code="UNSUPPORTED_SIMILARITY_GEOMETRY", kind=kind))
                if len(path) > 1:
                    incomplete_prefixes.add(tuple(path[:-1]))
                continue
            try:
                rec = _shape(entity, path, sid, lid)
            except (ValueError, TypeError, AttributeError, ZeroDivisionError, ArithmeticError):
                rec = None
            if rec is None:
                diagnostics.append(dict(code="SHAPE_UNAVAILABLE", entity_path=path))
                incomplete_prefixes.add(tuple(path[:-1]) or (path[0],))
                continue
            point_count += len(rec["points"])
            if point_count > MAX_POINTS:
                reasons.add("point_budget")
                break
            # Keep the whole group: dropping out-of-scope siblings would invent smaller objects.
            current.append(rec)
        if issues:
            diagnostics.extend(i.model_dump() for i in issues)
            current = []  # Expansion failures/cycles also leave incomplete groups.
            if any(i.code == "ENTITY_LIMIT" for i in issues):
                reasons.add("scan_budget")
        if "point_budget" in reasons:
            break
        records.extend(current)
    grouped = _groups(records)
    usable, skipped = [], 0
    for prefix, members in grouped:
        if (len(members) > MAX_SOURCE_IDS or sum(len(r["points"]) for r in members) > MAX_OBJECT_POINTS
                or (prefix and prefix in incomplete_prefixes)
                # Unvisited loose geometry could extend any component. Complete INSERT roots
                # can still be compared when a later root exhausts the global scan budget.
                or (not prefix and reasons)):
            skipped += 1
            continue
        obj = _object(members, prefix, sid, lid, root_info, texts)
        usable.append((obj, members))
    selected = [(obj, rs) for obj, rs in usable if _contains(args.sample_bbox, obj["bbox"])]
    sample_region = box(*args.sample_bbox)
    crossing = [obj for obj, rs in usable if _intersects(args.sample_bbox, obj["bbox"])
                and not _contains(args.sample_bbox, obj["bbox"])
                and any(LineString(r["points"]).intersects(sample_region) for r in rs)]
    coverage = dict(roots_total=total_roots, roots_scanned=roots_scanned,
        entities_visited=counter[0], scan_budget=args.scan_budget, point_count=point_count,
        candidate_budget=args.candidate_budget, grouped_objects=len(grouped), objects_skipped=skipped,
        truncated=bool(reasons or skipped), truncation_reasons=sorted(reasons),
        diagnostics=diagnostics[:20], diagnostics_truncated=len(diagnostics)>20,
        source_index_coverage=payload.get("index_coverage", {}), full_drawing_enumeration=False)
    result = dict(version=VERSION, snapshot_id=sid, source_sha256=payload["source_sha256"],
        layout_id=lid, frame_id=args.frame_id, search_bbox=bounds, sample_bbox=args.sample_bbox,
        coordinate_space="dxf_wcs", unit=payload["unit"], status="ready", candidates=[], coverage=coverage,
        method=dict(name="normalized_native_geometry_distance_distribution",
            learned_image_embedding=False, rotation_invariant=True, uniform_scale_invariant=True,
            geometry_approximation="source curves flattened at object extent/128",
            metadata="block name, primitive composition, layer and exact in-instance text are auxiliary"),
        limitations=[
            "相似分数是候选排序，不能确认门、设备、型号或其他工程对象身份，也不代表同规格。",
            "归一化忽略平移、旋转和等比缩放；实际WCS包络尺寸、源块旋转及比例单独保留。镜像可能相似。",
            "块内叶图元按实际实例路径分组；非块只按端点连接分组，不自动连接断裂线或解释墙体遮挡。",
            "形状使用有限采样的距离分布，可能存在不同形状同分；没有使用学习型图像embedding。",
            "未解析外部参照、代理图元、填充和标注不能据此确认覆盖；布局/图框范围采用WCS框包含。",
            "文字仅作为同实例内的原文辅助，未解释或确认型号。"])
    if not selected or crossing:
        result["status"] = "sample_unresolved"
        result["sample_issue"] = "样本框没有完整对象或穿过对象；请紧贴一个完整块实例或连接形状重新框选。"
        result["sample_crossing_object_count"] = len(crossing)
    else:
        sample_records = [r for _, rs in selected for r in rs]
        if len(sample_records) > MAX_SOURCE_IDS or sum(len(r["points"]) for r in sample_records) > MAX_OBJECT_POINTS:
            result["status"] = "sample_unresolved"
            result["sample_issue"] = "样本超出单对象来源或点数预算，请缩小框选。"
        else:
            sample = selected[0][0] if len(selected) == 1 else _object(sample_records, (), sid, lid, root_info, texts)
            result["sample"] = sample
            descriptor = _descriptor(sample_records)
            sample_ids = set(sample["entity_ids"])
            eligible = [(obj, rs) for obj, rs in usable
                if not sample_ids.intersection(obj["entity_ids"]) and (bounds is None or _contains(bounds, obj["bbox"]))]
            coverage["eligible_objects"] = len(eligible)
            if len(eligible) > args.candidate_budget:
                reasons.add("candidate_budget")
            matches = []
            for obj, members in eligible[:args.candidate_budget]:
                other = _descriptor(members)
                if descriptor is None or other is None:
                    continue
                score, components = _score(sample, obj, descriptor, other)
                if score >= args.min_similarity:
                    matches.append(obj | dict(similarity=score, score_components=components,
                        relative_path_length_scale=other["perimeter"]/descriptor["perimeter"],
                        relative_path_length_scale_basis="approximate_flattened_path_lengths"))
            matches.sort(key=lambda item: (-item["similarity"], item["object_id"]))
            result["candidates"] = matches[:args.limit]
            coverage.update(candidates_evaluated=min(len(eligible), args.candidate_budget),
                matches_above_threshold=len(matches), returned=len(result["candidates"]),
                results_truncated=len(matches)>args.limit)
    coverage["truncated"] = bool(reasons or skipped or diagnostics or coverage.get("results_truncated"))
    coverage["truncation_reasons"] = sorted(reasons)
    if skipped:
        coverage["truncation_reasons"].append("object_budget_or_incomplete_geometry")
    if diagnostics:
        coverage["truncation_reasons"].append("unsupported_or_unavailable_geometry")
    if coverage.get("results_truncated"):
        coverage["truncation_reasons"].append("result_limit")
    if list(file_stamp(source_path)) != stamp:
        raise ValueError("相似搜索期间来源文件已变化，结果未发布。")
    return result
