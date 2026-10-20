"""Bounded label-led room workflow. No floor, project or room-type whitelist.

Labels are candidates, never a room census. Units can only come from the drawing
or an explicit HTTP confirmation; the model tool cannot supply a unit override.
"""
import json
import re
import time
from typing import Literal

from pydantic import Field, model_validator
from cad_agent.contracts import Record, stable_id
from cad_agent.analysis.general import DrawingIndex
from .contracts import RoomMeasure
from .service import GeometryService
from .source import UNIT_TO_M, GEOMETRY_VERSION
from .rooms import ROOM_VERSION
from shapely.geometry import Polygon, box, Point
from copy import deepcopy

VERSION = "room-batch-v3.5-unit-provenance"


def normalized(text):
    return re.sub(r"\s+", "", text).casefold()


class RoomBatchQuery(Record):
    floor_title: str = Field(default="", max_length=80)
    frame_id: str = ""
    layout_id: str = ""
    room_label: str = Field(min_length=1, max_length=24)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12, ge=1, le=12)

    @model_validator(mode="after")
    def check(self):
        if not (self.floor_title.strip() or self.frame_id):
            raise ValueError("请指定楼层标题或图框。")
        if not re.fullmatch(r"[\w（）()\- ]+", self.room_label) or not self.room_label.strip():
            raise ValueError("请提供简短的房间名称，不是说明条款。")
        return self


class UnitConfirmation(Record):
    unit: Literal["mm", "cm", "m", "in", "ft"]
    confirmed: Literal[True]


class BatchPage(Record):
    offset: int = Field(ge=0)


class ScopeConfirmation(Record):
    frame_id: str = Field(min_length=1)
    confirmed: Literal[True]


class RoomBatchService:
    def __init__(self, workspace, snapshot_id):
        self.w, self.sid = workspace, snapshot_id
        self.index = DrawingIndex(workspace, snapshot_id)
        self.drawing = self.index.drawing

    def get(self, batch_id):
        record = self.w.store.get("room_batch", batch_id)
        if record["snapshot_id"] != self.sid or record["source_sha256"] != self.drawing["sha256"]:
            raise KeyError(batch_id)
        return record

    def frames(self, args):
        from cad_agent.cad.titles import same_plan_title
        self.index.scope(args.layout_id, args.frame_id)
        return [f for f in self.drawing["frames"]
                if (not args.layout_id or f["layout_id"] == args.layout_id)
                and (not args.frame_id or f["frame_id"] == args.frame_id)
                and (not args.floor_title or same_plan_title(f["name"],args.floor_title))]

    def infer_request(self, question):
        """Conservative routing from actual source vocabulary; otherwise normal agent loop."""
        q = normalized(question)
        if "面积" not in q or not any(x in q for x in ("哪些", "分别", "所有", "各个")):
            return None
        titles = {f["name"] for f in self.drawing["frames"] if "平面图" in f["name"] and normalized(f["name"]) in q}
        # Prevent 一层平面图 matching the suffix of 十一层平面图.
        titles = {t for t in titles if not any(t != u and normalized(t) in normalized(u) for u in titles)}
        if len(titles) != 1:
            return None
        title = next(iter(titles))
        frames = {f["frame_id"] for f in self.drawing["frames"] if f["name"] == title}
        labels = {normalized(r.text) for r in self.index.regions
                  if r.frame_id in frames and len(r.entity_ids) == 1
                  and 2 <= len(normalized(r.text)) <= 16 and normalized(r.text) in q
                  and "平面图" not in r.text and re.fullmatch(r"[\w（）()\- ]+", r.text)}
        labels = {t for t in labels if not any(t != u and t in u for u in labels)}
        if len(labels) != 1:
            return None
        label = next(iter(labels))
        remainder = q.replace(normalized(title), '').replace(label, '')
        remainder = re.sub(r'房间|面积|平方米|高亮|标出|定位|分别|哪些|所有|各个|多少|出来|请问|帮我|请|并|以及|还有|共有|一共|中|有|的|是', '', remainder)
        if re.search(r'[\w\u4e00-\u9fff]', remainder):
            return None  # Mixed tasks keep the regular multi-tool agent loop.
        return RoomBatchQuery(floor_title=title, room_label=label)

    def diagnose_frame(self, frame):
        """Check a bounded scope locally before asking the model/user to resolve it.

        Repeated identical titles (e.g. view title and title block) are supporting
        anchors, not separate floor identities. Different plan titles remain a
        conflict; a user selection cannot override missing/mixed boundaries.
        """
        from cad_agent.cad.titles import same_plan_title
        rows = self.index.scope(frame['layout_id'], frame['frame_id'])
        titles = [r for r in rows if normalized(r.text).endswith('平面图') and same_plan_title(r.text, frame['name'])]
        others = [r for r in rows if len(normalized(r.text)) <= 32
                  and normalized(r.text).endswith('平面图')
                  and not same_plan_title(r.text, frame['name'])]
        reasons = []
        if frame.get('method') != 'closed_border_and_title_candidate': reasons.append('missing_closed_border')
        if not normalized(frame['name']).endswith('平面图') or any(x in frame['name'] for x in ('详图', '剖面', '立面')): reasons.append('not_plan_title')
        if not titles: reasons.append('missing_matching_title')
        if others: reasons.append('conflicting_plan_titles')
        return dict(frame_id=frame['frame_id'], eligible=not reasons, reasons=reasons,
                    matching_title_count=len(titles), title_region_ids=[r.region_id for r in titles],
                    conflicting_titles=[dict(region_id=r.region_id, text=r.text) for r in others]), rows, titles

    def run(self, args, *, confirmation=None, parent_id=None, scope_confirmation=None):
        unit = confirmation.unit if confirmation else self.drawing["unit"]
        key = json.dumps([args.model_dump(), unit, parent_id, scope_confirmation], sort_keys=True)
        bid = stable_id("rooms", self.sid, self.drawing["sha256"], VERSION, GEOMETRY_VERSION, ROOM_VERSION, key)
        try:
            return self.get(bid)
        except KeyError:
            pass
        record = dict(batch_id=bid, snapshot_id=self.sid, source_sha256=self.drawing["sha256"],
                      version=VERSION, request=args.model_dump(), status="needs_scope_confirmation",
                      unit=unit, unit_basis="user_confirmed" if confirmation else "dxf_header",
                      unit_status='assumption' if confirmation else 'source_declared' if unit in UNIT_TO_M else 'unknown',
                      unit_confirmation=confirmation.model_dump() if confirmation else None,
                      parent_batch_id=parent_id, items=[], total=0, has_more=False, scope=None,
                      scope_confirmation=scope_confirmation,
                      scope_choices=self.frames(args), scope_evidence=[],
                      coverage={"scope":"unresolved", "enumeration":"not_started", "measurement":"not_started", "highlight":"not_started"},
                      note="名称文字候选不等于已确认房间总数；图框范围、名称与空间的对应、净边界及面积均需核查。",
                      evidence_url=f"/api/drawings/{self.sid}/room-batches/{bid}")
        choices = record["scope_choices"]
        checked = [self.diagnose_frame(frame) for frame in choices]
        record['scope_diagnostics'] = dict(complete=True, candidates=[d for d, _, _ in checked],
            resolution='multiple_frames' if len(choices)>1 else 'no_matching_frame' if not choices else 'unresolved')
        record['scope_choices'] = [dict(f, eligible=d['eligible'], reasons=d['reasons'])
                                   for f, (d, _, _) in zip(choices, checked)]
        eligible=[(frame,check) for frame,check in zip(choices,checked) if check[0]['eligible']]
        if len(eligible) == 1:
            frame,(diagnostic,rows,titles)=eligible[0]
            if diagnostic['eligible']:
                record["scope"] = frame
                record["scope_evidence"] = [self.index.evidence(t) for t in titles]
                record['scope_diagnostics']['resolution'] = 'user_selected_frame' if scope_confirmation else 'title_and_border'
                record["coverage"]["scope"] = "title_and_border_candidate"
                self._enumerate(record, args, rows, unit)
        if scope_confirmation and not record['scope']:
            raise ValueError('此范围仍缺少独立闭合边界或包含不同平面图标题，不能通过重复确认解除；请先修正范围。')
        self.w.store.put("room_batch", bid, record)
        return record

    def _enumerate(self, record, args, rows, unit):
        # Exact single-entity labels only. Wall lines can spuriously assign a
        # table_row_key; that heuristic is not proof the text is a schedule.
        # Preserve this ambiguity per item instead of silently losing labels.
        pattern = re.compile(r"(?:\d+[号#、.\-]?)?" + re.escape(normalized(args.room_label)) + r"(?:[（(]?\d+[）)]?)?")
        labels = [r for r in rows if len(r.entity_ids) == 1
                  and pattern.fullmatch(normalized(r.text))]
        labels.sort(key=lambda r: (-r.bbox[3], r.bbox[0], r.region_id))
        seen = set()
        labels = [r for r in labels if not (tuple(r.entity_ids) in seen or seen.add(tuple(r.entity_ids)))]
        # Keep all source anchors for cross-page identity checks, without
        # measuring or exposing unread pages as delivered results.
        record['label_anchors']=[dict(region_id=r.region_id,label_evidence={'bbox':r.bbox}) for r in labels]
        record.update(total=len(labels), has_more=args.offset+args.limit < len(labels),
                      next_offset=args.offset+args.limit if args.offset+args.limit < len(labels) else None)
        if args.offset > len(labels):
            raise ValueError("分页起点超过候选总数。")
        known_unit = unit in UNIT_TO_M
        record["status"] = "measured_candidates" if known_unit else "needs_unit_confirmation"
        record["coverage"].update(enumeration="page" if record["has_more"] or args.offset else "all_matching_labels",
                                  measurement="attempted" if known_unit else "blocked_unknown_units", highlight="label_locators")
        geometry = GeometryService(self.w, self.sid)
        texts = {t["entity_id"]: t for t in self.index.scene["texts"]}
        started = time.monotonic()
        for n, r in enumerate(labels[args.offset:args.offset+args.limit], args.offset+1):
            item = dict(number=n, region_id=r.region_id, label=r.text, label_evidence=self.index.evidence(r),
                        status="needs_unit_confirmation", area_m2=None, candidate_area_m2=None,
                        measurement_id=None, measurement_evidence_url=None, diagnostics=["unknown_units"],
                        highlight=dict(kind="label_locator", layout_id=r.layout_id, bbox=r.bbox),
                        identity_status="label_candidate")
            if r.table_row_key:
                item['source_classification'] = 'parser_table_row_candidate'
                item['identity_note'] = '原解析器将文字归为表格候选；平面墙线也可能触发此分类，需核查名称与空间的对应。'
            if known_unit:
                item.update(status="not_measured", diagnostics=["batch_time_budget"])
                if time.monotonic()-started < 20:
                    try:
                        seed = texts[r.entity_ids[0]]["origin"]
                        m = geometry.room(RoomMeasure(region_id=r.region_id, layout_id=r.layout_id, frame_id=r.frame_id,
                              seed=seed, unit_override=unit if record["unit_basis"] == "user_confirmed" else None,
                              other_room_region_ids=[x.region_id for x in labels if x.region_id != r.region_id][:12]))
                        item.update({k:m[k] for k in ("status", "area_m2", "candidate_area_m2", "measurement_id", "diagnostics")})
                        item["measurement_evidence_url"] = m["evidence_url"]
                        if m.get("selected"):
                            shape = m["selected"]
                            item["highlight"] = dict(kind="candidate_boundary", layout_id=r.layout_id,
                                                     bbox=shape["bbox"], boundary=shape["boundary"], holes=shape.get("holes", []))
                    except (ValueError, KeyError):
                        item.update(status="unresolved", diagnostics=["invalid_label_anchor_or_geometry"])
            record["items"].append(item)
        if not labels:
            record["status"] = "no_matching_labels"
            record["coverage"].update(measurement="no_candidates", highlight="no_candidates")
        elif known_unit:
            record["status"] = "needs_review" if any(i["status"] != "measured" for i in record["items"]) else "measured_candidates"
            if any(i["status"] == "not_measured" for i in record["items"]):
                record["coverage"]["measurement"] = "partial_time_budget"
                record['resume_offset'] = next(i['number']-1 for i in record['items'] if i['status']=='not_measured')
            record["coverage"]["highlight"] = "per_item_boundary_or_label"

    def confirm(self, batch_id, confirmation):
        old = self.get(batch_id)
        if old.get("boundary_review_id"):
            if old['unit']!='unknown':raise ValueError('该批次已应用单位，不能覆盖已有测量。')
            new=deepcopy(old)
            new.update(batch_id=stable_id('rooms',batch_id,confirmation.unit),parent_batch_id=batch_id,
                       unit=confirmation.unit,unit_basis='user_confirmed',unit_confirmation=confirmation.model_dump(),
                       unit_status='assumption',
                       unit_resolution=dict(status='resolved',unit=confirmation.unit,basis='user_confirmed',unit_status='assumption',reason='用户确认本次测量假设单位。'))
            new['evidence_url']=f'/api/drawings/{self.sid}/room-batches/{new["batch_id"]}'
            self.measure_selected(new)
            self.w.store.put('room_batch',new['batch_id'],new)
            return new
        if old["status"] != "needs_unit_confirmation":
            raise ValueError("该批次不处于等待单位确认状态。")
        return self.run(RoomBatchQuery.model_validate(old["request"]), confirmation=confirmation, parent_id=batch_id,
                        scope_confirmation=old.get('scope_confirmation'))

    def measure_selected(self, batch):
        """Area and overlay share exactly the same selected polygon, never reseed.

        A visually selected enclosure is not a certified net area. Numerical
        results retain the named basis and closure uncertainty per room.
        """
        if not batch.get('boundary_review_id'):return
        factor=UNIT_TO_M.get(batch['unit'])
        accepted=[]
        for item in batch['items']:
            h=item['highlight']
            if h['kind']!='candidate_boundary':continue
            p=Polygon(h['boundary'],h.get('holes',[]))
            valid=p.is_valid and not p.is_empty and p.area>0 and box(*batch['scope']['bbox']).buffer(1e-7).covers(p)
            item['geometry_check']={'valid_polygon':valid,'area_matches_highlight':valid,'area_drawing_units_squared':p.area if valid else None}
            if not valid:
                item.update(status='unresolved',area_m2=None,candidate_area_m2=None)
                item['diagnostics'].append('invalid_selected_geometry');continue
            accepted.append((item,p))
        for n,(item,p) in enumerate(accepted):
            for other,q in accepted[n+1:]:
                if p.intersection(q).area>min(p.area,q.area)*1e-6:
                    for i in (item,other):
                        i['geometry_check']['overlap']=True
                        i['geometry_check']['valid_polygon']=False
                        i['diagnostics'].append('overlapping_room_candidates')
        for item,p in accepted:
            if item['geometry_check']['valid_polygon'] and factor:
                item.update(status='candidate_measured',area_m2=None,candidate_area_m2=round(p.area*factor**2,6),
                            area_basis='selected_wall_enclosure',area_unit_basis=batch['unit_basis'],
                            area_unit_status=batch.get('unit_status','assumption' if batch['unit_basis'].startswith('user_') else 'source_declared'))
                item['diagnostics']=[d for d in item['diagnostics'] if d!='unknown_units']
            elif not factor:item.update(status='needs_unit_confirmation',area_m2=None,candidate_area_m2=None)
            else:item.update(status='unresolved',area_m2=None,candidate_area_m2=None)
            self.check_area_annotations(batch,item,p)
        batch['status']='needs_unit_confirmation' if not factor else 'needs_review'
        batch['coverage']['measurement']='selected_enclosure_candidates' if factor and accepted else 'blocked_unknown_units' if not factor else 'boundary_pending'

    def check_area_annotations(self,batch,item,polygon):
        """Spatial association is explicit, and incomplete unit text stays unknown."""
        from cad_agent.analysis.text_units import read_unit_candidates
        checks=[]
        for r in self.index.scope(batch['scope']['layout_id'],batch['scope']['frame_id']):
            match=re.fullmatch(r'(?:面积|[ASas])\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*(平方米|㎡|m[²2]|m)?',r.text.strip())
            if not match or not polygon.contains(Point((r.bbox[0]+r.bbox[2])/2,(r.bbox[1]+r.bbox[3])/2)):continue
            value=float(match[1]);known=match[2] in ('平方米','㎡','m²','m2')
            computed=item.get('candidate_area_m2')
            state='unit_text_incomplete' if not known else 'measurement_pending' if computed is None else 'different' if abs(computed-value)>max(.01,value*.005) else 'numerically_close'
            units=read_unit_candidates(self.index,r) if not known else []
            if units:state='compound_unit_candidate'
            recovered=[u for u in units if u.get('unit_resolution')=='source_supported_reading' and u['normalized_unit_candidate']=='m2']
            if len(recovered)==1:
                known=True
                state='source_supported_compound_unit'
            checks.append(dict(value=value,unit='m2' if known else 'unknown',status=state,
                               unit_candidates=units,annotation_basis='independent_source_annotation',
                               unit_basis='native_text_relation' if recovered else 'literal_annotation' if known else 'unresolved',
                               numeric_difference=round(computed-value,6) if known and computed is not None else None,
                               numeric_difference_basis='selected_enclosure_minus_independent_annotation_only' if known and computed is not None else None,
                               measurement_basis=item.get('area_basis','selected_wall_enclosure'),
                               basis_comparability='unconfirmed',semantic_difference=None,
                               quote=r.text,association='inside_selected_outline_not_semantically_confirmed',evidence=self.index.evidence(r)))
        item['area_annotation_checks']=checks[:4]
        if checks:
            item['area_annotation_note']='所选轮廓内另有面积标注：'+ '；'.join(c['quote'] for c in checks[:4])+ '。请核对标注单位、对象和计量口径；计算围合面积不替代原图标注。'
            if any(c['status']=='different' for c in checks):item['diagnostics'].append('area_annotation_difference')
            elif any(c['status']=='unit_text_incomplete' for c in checks):item['diagnostics'].append('area_annotation_unit_incomplete')

    def confirm_scope(self, batch_id, confirmation):
        old = self.get(batch_id)
        if old['status'] != 'needs_scope_confirmation':
            raise ValueError('该批次不处于等待范围确认状态。')
        choices = {f['frame_id']: f for f in old['scope_choices']}
        if confirmation.frame_id not in choices: raise ValueError('范围不属于此批次候选。')
        frame = choices[confirmation.frame_id]
        args = RoomBatchQuery.model_validate(old['request'] | dict(frame_id=frame['frame_id'],
                                            layout_id=frame['layout_id'], offset=0))
        return self.run(args, parent_id=batch_id, scope_confirmation=confirmation.model_dump())

    def page(self, batch_id, offset):
        old = self.get(batch_id)
        args = RoomBatchQuery.model_validate(old['request']).model_copy(update={'offset':offset})
        confirmation = UnitConfirmation.model_validate(old['unit_confirmation']) if old['unit_confirmation'] else None
        new=self.run(args, confirmation=confirmation, parent_id=old['parent_batch_id'],
                     scope_confirmation=old.get('scope_confirmation'))
        if old.get('unit_resolution') and not confirmation:
            from cad_agent.analysis.unit_policy import resolve_units
            new=resolve_units(self.w,self.sid,new,old['unit_resolution'].get('policy'))
        return new

    @staticmethod
    def summary(record):
        # Full polygons and per-edge evidence stay outside model context.
        result = {k:record[k] for k in ("batch_id", "status", "total", "has_more", "next_offset", "unit", "unit_basis", "unit_status", "unit_resolution", "boundary_review_id", "coverage", "note") if k in record}
        if result.get('unit_resolution'):
            result['unit_resolution']=deepcopy(result['unit_resolution'])
            check=result['unit_resolution'].get('dimension_check')
            if check:
                check['diagnostic_codes']=sorted({d.get('code','unknown') for d in check.pop('diagnostics',[])})
                check['numeric_evidence']=check['numeric_evidence'][:3] if not check['native_count'] else []
        result["scope_evidence"] = record["scope_evidence"]
        result['scope_diagnostics'] = record.get('scope_diagnostics', {'complete':False})
        if not record['scope']:
            result['total'] = None  # Not enumerated is not a zero-result search.
        result["scope_choices"] = [{k:f[k] for k in ("frame_id", "name", "layout_id")} for f in record["scope_choices"][:12]]
        result["items"] = [{k:i[k] for k in ("number", "region_id", "label", "label_evidence", "status", "area_m2", "candidate_area_m2", "area_basis", "area_unit_basis", "area_unit_status", "area_annotation_checks", "area_annotation_note", "geometry_check", "measurement_id", "diagnostics", "identity_status", "source_classification", "identity_note") if k in i} for i in record["items"]]
        for row,item in zip(result['items'],record['items']):row['highlight_kind']=item.get('highlight',{}).get('kind')
        result["instruction"] = "范围已在本地核查闭合边界、同名重复标题和异名标题冲突；未解决时按 scope_diagnostics 说明具体原因，不把未枚举说成零候选。逐项表格和高亮由界面展示。名称是候选；单位未知只暂停相关测量，继续独立要求；不重复此批次，分页未完成不能称全部房间。"
        return result
