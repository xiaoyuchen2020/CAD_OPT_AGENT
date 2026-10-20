"""Shared API/tool service for reproducible geometry observations."""
import json
import math
from html import escape


from cad_agent.analysis.general import DrawingIndex
from cad_agent.contracts import stable_id
from cad_agent.adapters.store import now
from .source import geometry_index, finite_point, UNIT_TO_M, GEOMETRY_VERSION, scoped_geometry_index, build_geometry
from .rooms import ROOM_VERSION, ROOM_VOID_VERSION
from .compute import compute
from .discovery import DISCOVERY_VERSION
from .contours import CONTOUR_VERSION


def compact_record(rec):
    out = {k: v for k, v in rec.items() if k not in ("paths", "native")}
    out["point_count"] = sum(len(p) for p in rec["paths"])
    return out


class GeometryService:
    def __init__(self, workspace, snapshot_id):
        self.w, self.sid = workspace, snapshot_id
        self.drawing = workspace.store.get("drawing", snapshot_id)
        self._index = None

    @property
    def index(self):
        if self._index is None:
            self._index = geometry_index(self.w, self.sid)
        return self._index

    def records(self, ids, unit=None):
        if len(set(ids)) != len(ids):
            raise ValueError("图元 ID 不得重复。")
        data = build_geometry(self.w, self.sid, entity_ids=ids, effective_unit=unit)
        by_id = {r["entity_id"]:r for r in data["entities"]}
        if any(eid not in by_id for eid in ids):
            raise ValueError("图元没有可用的计算几何。")
        return [by_id[eid] for eid in ids]

    def read(self, args):
        items = []
        for rec in self.records(args.entity_ids):
            item = compact_record(rec)
            # Full native geometry lives in geometry.json, with bounded excerpts
            # for model calls so a spline cannot exhaust the conversation budget.
            item["native"] = {k: v[:64] if isinstance(v, list) else v for k, v in rec["native"].items()}
            item["native_truncated"] = any(isinstance(v, list) and len(v)>64 for v in rec["native"].values())
            item["paths"] = [p[:64] for p in rec["paths"][:8]]
            item["paths_truncated"] = len(rec["paths"])>8 or any(len(p)>64 for p in rec["paths"])
            items.append(item)
        return dict(items=items, unit=self.drawing["unit"], geometry_version=GEOMETRY_VERSION,
                    source_sha256=self.drawing["sha256"], note="计算几何；曲线保留原参数并显式注明细分误差。图层和闭合状态不证明构件身份。")

    def query(self, args):
        if args.layout_id not in {l["layout_id"] for l in self.drawing["layouts"]}:
            raise ValueError("布局不属于当前快照。")
        index = scoped_geometry_index(self.w, self.sid, args.layout_id, args.bbox, self.drawing["unit"])
        records = index.query(args.layout_id, args.bbox, args.relation)
        return dict(items=[compact_record(r) for r in records[args.offset:args.offset+args.limit]],
                    total=len(records), has_more=args.offset+args.limit<len(records), unit=self.drawing["unit"],
                    geometry_complete=index.data["complete"],
                    approximation_note="曲线空间关系按 1 mm 弦差细分；单位未知时为 1 图纸单位，物理精度未知。近容差关系须复核。")

    def save(self, args, result, algorithm, identity_context=None):
        request = args.model_dump()
        if getattr(args, 'binding_id', ''):
            from .vision import VisionService
            binding = VisionService(self.w, self.sid).binding(args.binding_id)
            if binding['layout_id'] != result['layout_id']:
                raise ValueError('视觉绑定与测量不在同一布局。')
            if result['kind'] == 'geometry' and not set(args.entity_ids) <= set(binding['entity_ids']):
                raise ValueError('测量实体不属于视觉绑定。')
            if result['kind'] == 'room_net':
                a,b,c,d = binding['bbox']; x,y = result['seed']
                if not (a <= x <= c and b <= y <= d):
                    raise ValueError('室内点不在视觉绑定范围。')
            result = result | {'visual_binding': binding}
        identity = ["measure", self.sid, self.drawing["sha256"], GEOMETRY_VERSION, algorithm, json.dumps(request, sort_keys=True)]
        if identity_context is not None:
            identity.append(json.dumps(identity_context, sort_keys=True))
        mid = stable_id(*identity)
        # Identical inputs produce the same immutable observation and timestamp.
        try:
            return self.get(mid)
        except KeyError:
            pass
        record = dict(measurement_id=mid, snapshot_id=self.sid, source_sha256=self.drawing["sha256"],
                      geometry_version=GEOMETRY_VERSION, algorithm_version=algorithm, request=request,
                      coordinate_space="dxf_wcs", unit=self.drawing["unit"], created_at=now(), **result)
        record["evidence_url"] = f"/api/drawings/{self.sid}/measurements/{mid}"
        record["preview_url"] = record["evidence_url"]+"/preview.svg"
        self.w.store.put("measurement", mid, record)
        return record

    def get(self, mid):
        result = self.w.store.get("measurement", mid)
        if result["snapshot_id"] != self.sid:
            raise KeyError(mid)
        return result

    def measure(self, args, unit_policy=None):
        from cad_agent.analysis.unit_policy import geometry_unit_resolution
        resolution = geometry_unit_resolution(self.drawing['unit'], unit_policy, args.unit_override)
        effective_unit = resolution['unit']
        records = self.records(args.entity_ids, effective_unit)
        if len({r["layout_id"] for r in records}) != 1:
            raise ValueError("不能跨布局测量距离或面积。")
        if len(records) != (2 if args.operation == "distance" else 1):
            raise ValueError("距离需要两个图元，长度/面积需要一个图元。")
        if any(not r["paths"] or r["status"] != "ready" for r in records):
            raise ValueError("所选图元没有可用的二维几何。")
        factor = UNIT_TO_M.get(effective_unit)
        result = dict(kind="geometry", operation=args.operation, layout_id=records[0]["layout_id"],
                      status="unresolved", value=None, value_unit="m2" if args.operation=="area" else "m",
                      entity_ids=args.entity_ids, diagnostics=list(resolution['diagnostics']), applied_unit=effective_unit,
                      unit_basis=resolution['basis'], unit_status=resolution['unit_status'],
                      source_unit=self.drawing['unit'], unit_resolution=resolution,
                      identity_status='source_geometry_only',
                      approximate=any(not r["linear"] for r in records))
        if factor is None:
            if 'unknown_units' not in result['diagnostics']:
                result["diagnostics"].append("unknown_units")
            raw = compute(self.w,'geometry_measure',dict(records=records,factor=1.,operation=args.operation))
            result.update({k:v for k,v in raw.items() if k not in ('status', 'value')})
            result['raw_value'] = raw['value']
            result['raw_value_unit'] = 'drawing_units_squared' if args.operation == 'area' else 'drawing_units'
        else:
            result.update(compute(self.w,'geometry_measure',dict(records=records,factor=factor,operation=args.operation)))
        result["note"] = "几何计算，不代表墙体/房间身份或净面积。曲线结果为细分近似，不以此验证工程语义。"
        return self.save(args, result, "geometry-measure-v4-user-policy", identity_context=resolution)

    def room(self, args):
        reading = DrawingIndex(self.w, self.sid)
        reading.scope(args.layout_id, args.frame_id)
        label = reading.by_id.get(args.region_id) if args.region_id else None
        if args.region_id and label is None:
            raise ValueError("房间文字区域不属于当前快照。")
        if label and args.layout_id and label.layout_id != args.layout_id:
            raise ValueError("房间文字与指定布局不匹配。")
        lid = label.layout_id if label else args.layout_id
        frame_id = args.frame_id or (label.frame_id if label else "")
        frame = next((f for f in self.drawing["frames"] if f["frame_id"]==frame_id), None)
        if frame and frame["layout_id"] != lid:
            raise ValueError("图框与房间布局不匹配。")
        seed = args.seed
        if seed is None:
            # Native insertion point avoids treating text extents as room geometry.
            texts = {t["entity_id"]:t for t in reading.scene["texts"]}
            anchors = [texts[eid]["origin"] for eid in label.entity_ids if eid in texts]
            if len(anchors) != 1:
                raise ValueError("文字区域包含多个实体，请提供明确室内 seed 点，或选择单一房间名称。")
            seed = anchors[0]
        seed = finite_point(seed)
        if frame and not (frame["bbox"][0]<=seed[0]<=frame["bbox"][2] and frame["bbox"][1]<=seed[1]<=frame["bbox"][3]):
            raise ValueError("室内点不在所选图框范围。")
        other_seeds = []
        for rid in args.other_room_region_ids:
            other = reading.by_id.get(rid)
            if other is None or other.layout_id != lid or rid == args.region_id:
                raise ValueError("其他房间锚点无效或不在同一布局。")
            other_seeds.append([(other.bbox[0]+other.bbox[2])/2, (other.bbox[1]+other.bbox[3])/2])
        result = dict(kind="room_net", layout_id=lid, frame_id=frame_id, seed=list(seed),
                      label=label.text if label else None, label_evidence=reading.evidence(label) if label else None,
                      identity_status="candidate_from_selected_label_or_seed", status="unresolved", area_m2=None,
                      candidate_area_m2=None, candidates=[], diagnostics=[],
                      measurement_basis="paired_inner_wall_face_candidate",
                      tolerance_m=args.tolerance_m, applied_unit=args.unit_override or self.drawing["unit"],
                      unit_basis="request_override" if args.unit_override else "dxf_header",
                      note="局部房间净边界试点：测量对象身份和墙面解释仍需核查；不作优化或合规判断，不代表整图覆盖。")
        factor = UNIT_TO_M.get(args.unit_override or self.drawing["unit"])
        if not factor:
            result["diagnostics"].append("unknown_units")
        else:
            attempts = []
            local_index = None
            for search_radius in sorted({min(args.radius_m, r) for r in (2., 4., 8., args.radius_m)}):
                radius = search_radius/factor
                bounds = [seed[0]-radius,seed[1]-radius,seed[0]+radius,seed[1]+radius]
                local_index = scoped_geometry_index(self.w, self.sid, lid, bounds, args.unit_override or self.drawing["unit"])
                records = local_index.query(lid, bounds)
                selected_ids = {r["entity_id"] for r in records}
                if not set(args.void_entity_ids) <= selected_ids:
                    if search_radius < args.radius_m:
                        continue
                    raise ValueError("扣除图元必须属于当前布局和局部分析范围。")
                attempt = compute(self.w,'room',dict(records=records,seed=seed,factor=factor,
                    args=args.model_copy(update={"radius_m":search_radius}).model_dump(),
                    frame_bbox=frame["bbox"] if frame else None,other_seeds=other_seeds))
                attempt["scope_bbox"] = bounds
                attempts.append(attempt)
                selected = attempt.get("selected")
                if selected and not {"boundary_reaches_search_or_frame_limit", "insufficient_outside_wall_face_support"}.intersection(selected["issues"]):
                    break
            # Prefer supported bounded candidates; never turn exhausted windows into zero area.
            chosen = min(attempts, key=lambda a: (not bool(a.get("selected")),
                "boundary_reaches_search_or_frame_limit" in a["diagnostics"],
                len(a.get("selected", {}).get("issues", [])),
                a.get("candidate_area_m2") or float("inf")))
            result.update(chosen)
            result["search_attempts"] = [dict(scope_bbox=a["scope_bbox"],status=a["status"],
                input_segment_count=a["input_segment_count"],diagnostics=list(a["diagnostics"])) for a in attempts]
            # Re-evaluate coverage for the chosen window, which may be an earlier attempt.
            local_index = scoped_geometry_index(self.w, self.sid, lid, chosen["scope_bbox"], args.unit_override or self.drawing["unit"])
            if not local_index.data["complete"]:
                result["diagnostics"].append("incomplete_geometry_source")
                if result["status"] == "measured":
                    result.update(status="needs_review", area_m2=None)
            if result.get("selected"):
                result["numerical_area_envelope_m2"] = round(result["selected"]["perimeter_m"]*args.tolerance_m*math.sqrt(2)+math.pi*args.tolerance_m**2,6)
                result["uncertainty_note"] = "数值误差包络仅估算坐标量化影响，不包括边界识别、单位或对象语义错误。"
        source_diagnostics = local_index.data["diagnostics"] if factor else []
        result["source_diagnostics"] = source_diagnostics[:30]
        result["source_diagnostics_truncated"] = len(source_diagnostics)>30
        return self.save(args,result,ROOM_VOID_VERSION if args.void_entity_ids else ROOM_VERSION)

    def discover(self, args, source_bundle=None):
        canonical = args.model_copy(update={"offset":0,"limit":8})
        mid = stable_id("measure", self.sid, self.drawing["sha256"], GEOMETRY_VERSION,
                        DISCOVERY_VERSION, json.dumps(canonical.model_dump(), sort_keys=True))
        try:
            return self.get(mid)
        except KeyError:
            pass
        reading = DrawingIndex(self.w, self.sid)
        reading.scope(args.layout_id, args.frame_id)
        bounds = list(args.bbox)
        if args.frame_id:
            frame = next(f for f in self.drawing["frames"] if f["frame_id"] == args.frame_id)
            if frame["layout_id"] != args.layout_id:
                raise ValueError("图框与识别布局不匹配。")
            bounds = [max(bounds[0],frame["bbox"][0]),max(bounds[1],frame["bbox"][1]),
                      min(bounds[2],frame["bbox"][2]),min(bounds[3],frame["bbox"][3])]
            if bounds[0]>=bounds[2] or bounds[1]>=bounds[3]:
                raise ValueError("识别范围与图框不相交。")
        unit = args.unit_override or self.drawing["unit"]
        factor = UNIT_TO_M.get(unit)
        result = dict(kind="room_discovery", status="needs_review", layout_id=args.layout_id,
                      view_kind=args.view_kind,
                      frame_id=args.frame_id, scope_bbox=bounds, applied_unit=unit,
                      unit_basis="request_override" if args.unit_override else "dxf_header",
                      identity_status="unconfirmed_enclosed_spaces", candidates=[], diagnostics=[],
                      coverage="bounded_scope_only_not_whole_drawing", complete=False,
                      note="按围合拓扑发现任意用途及未命名空间，不使用房间名称或图层白名单。候选可能是墙带、设备或标注；文字仅为空间关联，房间身份和净边界须复核。面积为外壳几何面积，不是确认净面积。")
        if args.view_kind not in ("unknown","floor_plan"):
            result.update(status="unresolved", diagnostics=["non_plan_view"])
        elif factor is None:
            result.update(status="unresolved", diagnostics=["unknown_units"])
        elif max(bounds[2]-bounds[0],bounds[3]-bounds[1])*factor>200:
            result.update(status="unresolved", diagnostics=["discovery_scope_too_large"])
        else:
            if source_bundle is None:
                index = scoped_geometry_index(self.w,self.sid,args.layout_id,bounds,unit)
                scoped = dict(records=index.query(args.layout_id,bounds), **{k:index.data[k] for k in ('complete','diagnostics')})
            else:
                scoped = source_bundle.get(args.layout_id,bounds,unit)
            result.update(compute(self.w,'discover',dict(records=scoped['records'],
                bounds=bounds,factor=factor,args=args.model_dump())))
            if not scoped["complete"]:
                result["diagnostics"].append("incomplete_geometry_source")
                result["complete"] = False
            result["source_diagnostics"] = scoped["diagnostics"][:30]
            result["source_diagnostics_truncated"] = len(scoped["diagnostics"])>30
            # Names are optional evidence, never a prerequisite for discovering a face.
            texts = {t["entity_id"]:t for t in reading.scene["texts"]}
            anchors = [(r, t["origin"]) for r in reading.scope(args.layout_id,args.frame_id)
                       for eid in r.entity_ids if (t:=texts.get(eid)) is not None
                       and bounds[0]<=t["origin"][0]<=bounds[2] and bounds[1]<=t["origin"][1]<=bounds[3]]
            anchor_hits = compute(self.w,'discovery_anchors',dict(
                anchors=[(r.region_id,point) for r,point in anchors],candidates=result["candidates"]))
            for c, region_ids in zip(result["candidates"],anchor_hits):
                hits = {rid:reading.by_id[rid] for rid in region_ids}
                c["text_regions"] = [dict(region_id=r.region_id,text=r.text[:160]) for r in hits.values()][:12]
                c["text_region_count"] = len(hits)
                c["text_regions_truncated"] = len(hits)>12
                c["name_status"] = "unassigned" if not hits else "spatial_text_candidates_not_confirmed_name"
                if len(hits)>1:
                    c["diagnostics"].append("multiple_text_candidates")
                # A follow-up uses exactly the discovered interior point, never an invented name.
                radius = max(abs(c["bbox"][i]-c["seed"][i%2]) for i in range(4))*factor+1
                if radius<=50:
                    c["measurement_request"] = dict(layout_id=args.layout_id,frame_id=args.frame_id,seed=c["seed"],
                        radius_m=max(1.,radius),unit_override=args.unit_override,
                        tolerance_m=args.tolerance_m,wall_min_m=args.wall_min_m,wall_max_m=args.wall_max_m,
                        close_openings=args.close_openings,max_opening_m=args.max_opening_m)
                else:
                    c["diagnostics"].append("room_exceeds_measurement_radius_limit")
            if not result["candidates"]:
                result["status"] = "unresolved"
            if args.view_kind == "unknown":
                result["diagnostics"].append("plan_view_unconfirmed")
        # Pagination is a view of one immutable discovery run, not a new computation.
        return self.save(canonical,result,DISCOVERY_VERSION)

    @staticmethod
    def discovery_summary(record, offset=0, limit=8):
        keys = ("candidate_id","status","identity_status","boundary_support","candidate_role","seed","bbox","candidate_shell_area_m2",
                "area_m2","outside_face_fraction","diagnostics","text_regions","name_status",
                "text_region_count","text_regions_truncated","measurement_request")
        candidates = record["candidates"]
        result = GeometryService.summary(record)
        result.update(items=[{k:c[k] for k in keys if k in c} for c in candidates[offset:offset+limit]],
                      total=len(candidates),offset=offset,has_more=offset+limit<len(candidates),
                      paired_face_candidate_count=sum(c["boundary_support"]=="paired_faces" for c in candidates),
                      room_candidate_count=sum(c["candidate_role"]=="room_candidate" for c in candidates),
                      scope_bbox=record["scope_bbox"],complete=record["complete"],
                      view_kind=record["view_kind"],
                      coverage_note="complete 仅指本范围直线拓扑计算完成，不代表房间召回完整或身份确认。文字关联受已解析文字覆盖限制。")
        if record.get("subdivision"):result["subdivision"]=record["subdivision"]
        for item, c in zip(result["items"], candidates[offset:offset+limit]):
            if model := c.get("geometry_model"):
                item["contour_summary"] = dict(outer_contour_id=model["outer_contour_id"],
                    interior_contour_count=len(model["interior_contour_ids"]),
                    topological_hole_count=len(model["topological_hole_ids"]),
                    diagnostics=model["diagnostics"],
                    read_request=dict(discovery_id=record["measurement_id"],candidate_id=c["candidate_id"]))
        return result

    def _room_candidate(self, args):
        record = self.get(args.discovery_id)
        if record["kind"] != "room_discovery":
            raise ValueError("需要房间识别记录 ID。")
        candidate = next((c for c in record["candidates"] if c["candidate_id"] == args.candidate_id), None)
        if candidate is None or not candidate.get("geometry_model"):
            raise ValueError("候选不存在或缺少轮廓模型，请重新识别。")
        return record, candidate

    def room_contours(self, args):
        record, candidate = self._room_candidate(args)
        model = candidate["geometry_model"]
        by_id = {c["contour_id"]: c for c in record["contours"]}
        ids = model["interior_contour_ids"]
        def compact(cid):
            contour = by_id[cid]
            bounded = {k:(v[:64] if isinstance(v, list) else v) for k,v in contour.items() if k not in ("boundary", "edge_evidence")}
            return bounded | dict(
                boundary=contour["boundary"][:128], edge_evidence=contour["edge_evidence"][:24],
                geometry_truncated=len(contour["boundary"])>128 or len(contour["edge_evidence"])>24,
                relation_or_source_ids_truncated=any(len(v)>64 for k,v in contour.items() if isinstance(v,list) and k not in ("boundary","edge_evidence")),
                is_topological_hole=cid in model["topological_hole_ids"])
        compact_model = {k:v for k,v in model.items() if k not in ("interior_contour_ids","face_variants","topological_hole_ids","boundary_intersection_contour_ids")}
        compact_model.update(interior_contour_count=len(ids), topological_hole_count=len(model["topological_hole_ids"]),
            boundary_intersection_count=len(model["boundary_intersection_contour_ids"]),
            boundary_intersection_contour_ids=model["boundary_intersection_contour_ids"][:64],
            relations_truncated=len(model["boundary_intersection_contour_ids"])>64)
        return dict(discovery_id=record["measurement_id"], candidate_id=candidate["candidate_id"],
            snapshot_id=self.sid, source_sha256=record["source_sha256"], version=CONTOUR_VERSION,
            coordinate_space="dxf_wcs", applied_unit=record["applied_unit"], unit_basis=record["unit_basis"],
            status="needs_review", geometry_model=compact_model, outer_contour=compact(model["outer_contour_id"]),
            items=[compact(cid) for cid in ids[args.offset:args.offset+args.limit]], total=len(ids),
            offset=args.offset, has_more=args.offset+args.limit<len(ids), diagnostics=record["diagnostics"]+candidate["diagnostics"],
            evidence_url=record["evidence_url"], preview_url=record["preview_url"],
            note="轮廓身份未确认。parent_contour_ids 是整个识别范围内的严格包含关系；相交或接触边界另行记录。拓扑内环不等于已确认应扣除的空洞。")

    def room_area(self, args):
        record, candidate = self._room_candidate(args)
        values = compute(self.w,'contour_area',dict(candidate=candidate,library=record["contours"],
            factor=UNIT_TO_M[record["applied_unit"]],basis=args.basis,exclusions=args.exclusion_contour_ids))
        result = dict(kind="room_area_basis", status="needs_review", area_m2=None,
            identity_status="unconfirmed_enclosed_space", measurement_basis=args.basis,
            discovery_id=record["measurement_id"], candidate_id=candidate["candidate_id"],
            source_evidence_url=record["evidence_url"], geometry_model=candidate["geometry_model"],
            layout_id=record["layout_id"], frame_id=record["frame_id"],
            applied_unit=record["applied_unit"], unit_basis=record["unit_basis"],
            tolerance_m=record["request"]["tolerance_m"],
            boundary=candidate["boundary"], internal_rings=candidate["internal_rings"],
            diagnostics=list(dict.fromkeys(record["diagnostics"]+candidate["diagnostics"]+["area_basis_requires_semantic_review"])),
            note="口径面积是候选轮廓的几何计算，不是确认净面积。外轮廓口径不扣除；拓扑面口径扣除直接内环，内环中的岛属于其他面；指定扣除口径对所选内部轮廓求并集后扣除，重叠和嵌套不重复扣除。", **values)
        if values["approximate"]:
            result["diagnostics"].append("tessellated_exclusion_contours")
        return self.save(args, result, CONTOUR_VERSION)

    @staticmethod
    def summary(record):
        keys = ("measurement_id", "snapshot_id", "source_sha256", "algorithm_version", "kind", "operation",
                "status", "area_m2", "candidate_area_m2", "value", "value_unit", "raw_value", "raw_value_unit", "approximate", "label", "layout_id", "frame_id",
                "seed", "measurement_basis", "identity_status", "diagnostics", "coverage", "tolerance_m",
                "numerical_area_envelope_m2", "applied_unit", "unit_basis", "unit_status", "source_unit", "unit_resolution", "note", "evidence_url", "preview_url", "visual_binding")
        out = {k:record[k] for k in keys if k in record}
        if record["kind"] == "room_area_basis":
            out.update({k:record[k] for k in ("discovery_id","candidate_id","shell_area_m2","excluded_union_area_m2",
                "basis_area_m2","perimeter_m","selected_contour_ids","source_evidence_url","maximum_curve_approximation_m")})
        out['claim_contract'] = {'measurement_result': 'derived/unknown' if record['status']=='measured' else 'unknown',
                                 'measurement_diagnostic': 'observed; only status/diagnostics/units/links, no measurement values',
                                 'identity': 'candidate, not confirmed by calculation'}
        if 'raw_value' in record:
            out['claim_contract']['raw_measurement'] = 'drawing-coordinate candidate only; no physical unit or object identity confirmed'
        if record["kind"] == "room_area_basis":
            out['claim_contract']['measurement_result'] = 'unknown; basis_area_m2 is candidate geometry under an explicit basis, not confirmed net area'
        selected = record.get("selected")
        if selected:
            out["boundary_summary"] = dict(bbox=selected["bbox"], edge_count=len(selected["edge_evidence"]),
                source_entity_count=len(selected["entity_ids"]), inferred_closure_count=len(selected["inferred_closures"]),
                unresolved_interior_object_count=len(selected["unresolved_interior_objects"]), issues=selected["issues"])
        return out

    def svg(self, mid):
        r = self.get(mid)
        if r['kind'] in ('connection_review','discovery_review'):
            from cad_agent.cad.render import render_svg
            action=r.get('highlight_action',{})
            bounds=r['scope_bbox'];render=self.w.rendering(self.sid)
            svg=render_svg(render,r['layout_id'],bounds,width=1200,height=900,raster=True)
            paths=list(action.get('paths',[]))+[i['boundary'] for i in action.get('regions',[])]
            scale=max(bounds[2]-bounds[0],bounds[3]-bounds[1])/900
            overlays=[]
            for points in paths:
                path='M '+' L '.join(f'{x},{-y}' for x,y in points)
                overlays.append(f'<path d="{path}" fill="none" stroke="#126ac2" stroke-width="{2*scale}"/>')
            return svg.replace('</svg>',''.join(overlays)+'</svg>')
        discovery_shapes = r.get("candidates", []) if r["kind"] == "room_discovery" else []
        selected = r.get("selected", {})
        boundary = selected.get("boundary", r.get("boundary", []))
        points = boundary or r.get("measurement_line", [])
        if r["kind"] == "room_discovery":
            b = r["scope_bbox"]
            points = [b[:2],b[2:]]
        if not points:
            return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 80"><text x="10" y="40">No measurable boundary; see diagnostics.</text></svg>'
        x,y,xx,yy = min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)
        margin=max(xx-x, yy-y, 1)*.15
        bounds=[x-margin,y-margin,xx+margin,yy+margin]
        parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bounds[0]} {-bounds[3]} {bounds[2]-bounds[0]} {bounds[3]-bounds[1]}">', '<rect x="-1e12" y="-1e12" width="2e12" height="2e12" fill="white"/>']
        def path(points):
            return 'M '+' L '.join(f'{p[0]},{-p[1]}' for p in points)
        nearby=scoped_geometry_index(self.w, self.sid, r["layout_id"], bounds, r["applied_unit"]).query(r["layout_id"], bounds)
        count=0
        for rec in nearby:
            for pts in rec["paths"]:
                if count>=2000:
                    break
                parts.append(f'<path d="{path(pts)}" fill="none" stroke="#aab1b8" stroke-width="1" vector-effect="non-scaling-stroke"/>')
                count+=1
        rings=[boundary]+selected.get("holes",[]) if boundary else []
        for c in discovery_shapes[:50]:
            parts.append(f'<path d="{path(c["boundary"])} Z" fill="none" stroke="#087d65" stroke-width="2" vector-effect="non-scaling-stroke"/>')
            for ring in c.get("internal_rings", []):
                parts.append(f'<path d="{path(ring)} Z" fill="none" stroke="#b45309" stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>')
        if r["kind"] == "room_area_basis":
            rings = []
            for polygon in r["result_polygons"]:
                parts.append(f'<path d="{" ".join(path(p)+" Z" for p in [polygon["boundary"]]+polygon["holes"])}" fill="#22aa8860" fill-rule="evenodd" stroke="#087d65" stroke-width="2" vector-effect="non-scaling-stroke"/>')
            source = self.get(r["discovery_id"])
            for c in source["contours"]:
                if c["contour_id"] in r["geometry_model"]["interior_contour_ids"]:
                    parts.append(f'<path d="{path(c["boundary"])} Z" fill="none" stroke="#b45309" stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>')
        if rings:
            parts.append(f'<path d="{" ".join(path(p)+" Z" for p in rings)}" fill="#22aa8860" fill-rule="evenodd" stroke="#087d65" stroke-width="2" vector-effect="non-scaling-stroke"/>')
        for closure in selected.get("inferred_closures",[]):
            parts.append(f'<path d="{path(closure["points"])}" stroke="#d46b08" stroke-width="3" stroke-dasharray="6 4" vector-effect="non-scaling-stroke"/>')
        if r.get("measurement_line"):
            parts.append(f'<path d="{path(r["measurement_line"])}" stroke="#087d65" stroke-width="3" vector-effect="non-scaling-stroke"/>')
        caption=f'{r.get("label") or "Geometry"} · {r["status"]} · {r.get("area_m2",r.get("value"))} · source {r["source_sha256"][:12]}'
        if r["kind"] == "room_area_basis":
            caption=f'{r["measurement_basis"]} · candidate {r["basis_area_m2"]} m2 · needs review · source {r["source_sha256"][:12]}'
        if r["kind"] == "room_discovery":
            caption=f'Enclosure candidates (identity unconfirmed): {len(discovery_shapes)} · source {r["source_sha256"][:12]}'
            if len(discovery_shapes)>50:
                caption+=' · first 50 candidates only'
        if count>=2000:
            caption+=' · background truncated'
        parts.append(f'<text x="{x}" y="{-yy-margin*.3}" font-size="{margin*.22}" fill="#172b36">{escape(caption)}</text></svg>')
        return ''.join(parts)
