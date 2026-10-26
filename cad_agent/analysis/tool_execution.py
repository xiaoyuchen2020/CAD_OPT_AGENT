"""CAD tool execution and result bookkeeping."""
import json
import re
from copy import deepcopy
import time
from cad_agent.contracts import ExtractionRequest
from cad_agent.analysis.overview import drawing_overview
from cad_agent.semantics.service import SemanticService
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.room_batch import RoomBatchService
from cad_agent.analysis.enclosure_inspection import inspect as inspect_enclosure
from cad_agent.analysis.result_sets import restore_previous, analyze as analyze_values
from cad_agent.analysis.focus import ToolsetRequest, CONTROL
from cad_agent.components.service import ComponentService
from cad_agent.components.dimension_chain import check_chain
from cad_agent.geometry.spatial_labels import locate as locate_space_labels
from cad_agent.geometry.connection_review import ConnectionReviewService
from cad_agent.geometry.discovery_review import DiscoveryReviewService
from cad_agent.geometry.vision import VisionService
from cad_agent.geometry.room_boundary import RoomBoundaryService
from cad_agent.analysis.general_facts import extract_general, query_general
from cad_agent.analysis.efficiency import compact, dumps, evidence_ids
from cad_agent.analysis.unit_policy import resolve_units
from cad_agent.analysis.fact_tools import calculate_facts, read_assembly, resolve_scope, project_read

from cad_agent.analysis.tool_registry import TOOL_SPECS


def component_page_call_budget(session):
    """Current page included, one global call reserved for finish; never extend quotas."""
    budget=max(0,session.w.settings.agent_max_tool_calls-session.calls)
    ids=set(session.trace[-1].get('task_ids',[])) if session.trace else set()
    active=[s for s in session.task.data['subtasks'] if s['id'] in ids or (not ids and s['status']=='running')]
    if active:budget=min(budget,min(max(0,12-s['calls']+1) for s in active))
    return budget


def execute(self, name, raw_args, *, bounded=False):
    from cad_agent.analysis.evidence_ledger import source_context, compact_coverage
    if name not in TOOL_SPECS:
        raise ValueError("未知工具；此工作台不支持代码执行或修改文件。")
    from cad_agent.analysis.tool_registry import require_supported
    require_supported(name, self.request.runtime, self.w.settings.provider)
    args = TOOL_SPECS[name][0].model_validate(raw_args, strict=True)
    if self.journal.tool_stack:
        self.journal.section(self.journal.tool_stack[-1], 'execution', args.model_dump(),
                             '工具执行入口参数（含已验证默认值）')
    if name == 'search_source_records':
        from cad_agent.analysis.source_candidates import search
        return search(self, args)
    from cad_agent.tables.tools import NAMES as TABLE_TOOLS, execute as execute_table
    if name in TABLE_TOOLS:
        return execute_table(self,name,args)
    if name == 'read_evidence_record':
        from cad_agent.analysis.evidence_store import read
        return read(self, args)
    if name == 'query_property_evidence':
        from cad_agent.analysis.property_evidence import query_property_evidence
        return query_property_evidence(self, **args.model_dump())
    if name == 'batch_read_sources':
        raise ValueError('批量读取须通过有界调用入口，检查批量参数大小。')
    from cad_agent.analysis.scope_workflow import bind_scope_call
    args = bind_scope_call(self, name, args)
    snapshot = self.request.snapshot_id
    if name in ('extract_planning_metrics', 'extract_building_schedule'):
        from cad_agent.analysis.business_tables import extract_planning_metrics, extract_building_schedule
        from cad_agent.analysis.business_table_ledger import attach_read_coverage
        method = extract_planning_metrics if name == 'extract_planning_metrics' else extract_building_schedule
        target = args.snapshot_id or snapshot
        if target not in self.allowed_snapshots:
            raise ValueError('业务表目标图纸未加入本轮用户选定资料。')
        result = method(self.w, target, args, self.index if target==snapshot else None)
        return self._register_tree(attach_read_coverage(self, name, result))
    if name == 'read_requirement_sources':
        from cad_agent.analysis.requirement_sources import read
        return self._register_tree(read(self, args))
    if name == 'search_similar_objects':
        from cad_agent.analysis.similarity_search import search_similar_objects
        return search_similar_objects(self.w, snapshot, args)
    if name in ('bind_spatial_object', 'get_spatial_object'):
        from cad_agent.analysis.spatial_evidence import bind_spatial_object, get_spatial_object
        return (bind_spatial_object if name == 'bind_spatial_object' else get_spatial_object)(self, args)
    from cad_agent.analysis.tool_metadata import SPATIAL_METHODS
    if name in SPATIAL_METHODS:
        from cad_agent.analysis.spatial_workflow import execute_spatial_method
        return execute_spatial_method(self, name, args)
    if name=='select_source_records':
        from cad_agent.analysis.source_record_selection import select
        return select(self,args)
    if name=='read_assembly':return self._register_tree(read_assembly(self,args))
    if name=='calculate_facts':return calculate_facts(self,args)
    if name=='resolve_drawing_scope':return self._register_tree(resolve_scope(self,args))
    if name=='inspect_scope_objects':
        from cad_agent.analysis.scope_objects import inspect
        return inspect(self,args)
    if name=='read_project_sources':return self._register_tree(project_read(self,args))
    if name=='locate_space_labels':return locate_space_labels(self.w,snapshot,args)
    if name in ('prepare_space_connections','review_space_connections','prepare_discovery_review','review_discovered_spaces'):
        if self.w.settings.provider != 'responses_subscription' or self.request.runtime not in ('responses_subscription','codex'):
            raise ValueError('当前通道不支持局部图像审查。')
        service=(ConnectionReviewService if 'connections' in name else DiscoveryReviewService)(self.w,snapshot)
        if name.startswith('prepare_'):
            if any(k not in getattr(self,'reviewed_image_ids',set()) for k,v in self.visual_images.items() if v.get('kind') in ('geometry_review_sheet','boundary_contact_sheet')):raise ValueError('先完成当前已准备图像的逐项审查。')
            if name=='prepare_space_connections':
                seen={rid for o in self.observations.values() if o['tool'] in ('query_entities','locate_space_labels') for i in o['data'].get('items',[]) for rid in i.get('source_region_ids',[])}
                if any(p.source_region_id not in seen or p.target_region_id not in seen for p in args.pairs) or not set(args.excluded_region_ids)<=seen:raise ValueError('先query_entities读取双方名称及来源区域。')
            else:
                if not any(o['tool']=='discover_rooms' and o['data'].get('measurement_id')==args.discovery_id for o in self.observations.values()):raise ValueError('先读取本轮空间发现结果。')
                if not args.prior_review_id:
                    prior=next((o['data']['review_id'] for o in reversed(list(self.observations.values())) if o['tool']=='review_discovered_spaces' and o['data'].get('discovery_id')==args.discovery_id),None)
                    if prior:args=args.model_copy(update={'prior_review_id':prior})
            record=service.prepare(args)
            self.geometry_reviews=getattr(self,'geometry_reviews',{})|{record['review_id']:record}
            self.visual_images.update({i['image_id']:i for i in record['images']})
        else:
            if args.review_id not in getattr(self,'geometry_reviews',{}):raise ValueError('本轮尚未准备该审查。')
            record=service.review(args,self.sent_image_ids)
            self.reviewed_image_ids=getattr(self,'reviewed_image_ids',set())|{i['image_id'] for i in record['images']}
            self.highlight_action=record['highlight_action'];self.task.data['highlight_action']=self.highlight_action
            summary=service.summary(record)
            # Public rows survive reload independently of which observations
            # the model cites; immutable full measurements remain linked.
            public={k:summary[k] for k in ('kind','review_id','evidence_url','ranking_complete','selected','items') if k in summary}
            if record['kind']=='connection_review':
                public['items']=[{k:i[k] for k in ('item_id','number','labels','decision','selected_measurements')} for i in record['items']]
            else:public.pop('items',None)
            previous=self.task.data.get('spatial_results',[])
            self.task.data['spatial_results']=[v for v in previous if v['kind']!=public['kind']]+[public]
        return service.summary(record)
    if name == "inspect_enclosure":
        return inspect_enclosure(self,args)
    if name == "check_dimension_chain":
        result=check_chain(self.w,snapshot,args)
        if result.get('parts'):
            dims=result['parts']+[result['total']]
            paths=[[p[:2] for p in d['definition_points']] for d in dims]
            points=[p for path in paths for p in path]
            bounds=[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
            self.highlight_action=dict(mode='replace',kind='geometry',snapshot_id=snapshot,
                scope=dict(layout_id=args.layout_id,bbox=bounds),paths=paths,regions=[],object_ids=result['highlight_entity_ids'])
            self.task.data['highlight_action']=self.highlight_action
            result['operation_receipts'].append(dict(operation='highlight',fulfilled=True))
        return result
    if name == "get_analysis_results":
        return self._register_tree(restore_previous(self))
    if name == "analyze_measurements":
        return analyze_values(self,args)
    if name == 'plan_task':
        return self.task.plan(args)
    if name == "analyze_room_batch":
        service = RoomBatchService(self.w, snapshot)
        self.room_batch = service.run(args)
        # Unit inspection is a local prerequisite, not another model round.
        # Its source observations remain explicit in the returned batch.
        if self.room_batch['scope'] and self.room_batch['items'] and any(s['kind']=='measure' for s in self.task.data['subtasks']):
            self.room_batch=resolve_units(self.w,snapshot,self.room_batch,self.task.data.get('unit_policy'))
        self.room_batches[self.room_batch['batch_id']]=self.room_batch
        return self._register_tree(service.summary(self.room_batch))
    if name=='resolve_room_units':
        if args.batch_id not in self.room_batches:raise ValueError('先读取当前任务的房间批次。')
        batch=resolve_units(self.w,snapshot,self.room_batches[args.batch_id],self.task.data.get('unit_policy'))
        self.room_batches.pop(args.batch_id,None)
        self.room_batches[batch['batch_id']]=batch
        self.room_batch=batch
        return self._register_tree(RoomBatchService.summary(batch))
    if name in ('prepare_room_boundaries', 'select_room_boundaries'):
        if self.w.settings.provider != 'responses_subscription' or self.request.runtime not in ('responses_subscription','codex'):
            raise ValueError('当前通道不支持边界图像输入。')
        service=RoomBoundaryService(self.w,snapshot)
        if name=='prepare_room_boundaries':
            if args.batch_id not in self.room_batches:raise ValueError('先读取当前会话的房间批次。')
            pending_images={k:v for k,v in self.visual_images.items() if k not in getattr(self,'reviewed_image_ids',set()) and v.get('kind') in ('geometry_review_sheet','boundary_contact_sheet')}
            if pending_images:raise ValueError('先完成已准备的局部图像审查。')
            record=service.prepare(args,remaining=self._remaining)
            self.boundary_reviews=getattr(self,'boundary_reviews',{})|{record['review_id']:record}
            self.visual_images.update({i['image_id']:i for i in record['images']})
            return service.summary(record)
        if args.review_id not in getattr(self,'boundary_reviews',{}):raise ValueError('候选未在本次会话准备。')
        record,batch=service.select(args,self.sent_image_ids)
        self.reviewed_image_ids=getattr(self,'reviewed_image_ids',set())|{i['image_id'] for i in record['images']}
        old=record['batch_id']
        self.room_batches.pop(old,None)
        self.room_batches[batch['batch_id']]=batch
        self.room_batch=batch
        return self._register_tree(service.summary(record)|{'batch_id':batch['batch_id'],'boundary_review_complete':True,
                                        'measurement':RoomBatchService.summary(batch)})
    if name in ('read_door_schedule_table', 'trace_door_schedule'):
        from cad_agent.analysis.door_schedule import read_door_schedule_table, trace_door_schedule
        method = read_door_schedule_table if name == 'read_door_schedule_table' else trace_door_schedule
        return self._register_tree(method(self, args))
    if name=='trace_detail' and not set(args.target_snapshot_ids)<=self.allowed_snapshots:
        raise ValueError('跨图目标未加入本轮关联图纸清单。')
    if name in ('query_annotations', 'locate_by_axes', 'trace_detail', 'read_table_semantics'):
        service = SemanticService(self.w, snapshot, self.index)
        method = {'query_annotations': service.query, 'locate_by_axes': service.locate,
                  'trace_detail': service.trace, 'read_table_semantics': service.table}[name]
        result = method(args)
        if name == 'locate_by_axes' and result.get('boundary'):
            self.highlight_action = dict(mode='replace', kind='geometry', snapshot_id=snapshot,
                scope=dict(layout_id=args.layout_id, bbox=result['bbox']), paths=[result['boundary']],
                regions=[], object_ids=list(args.axis_ids))
            self.task.data['highlight_action'] = self.highlight_action
            public = {k: result[k] for k in ('snapshot_id', 'layout_id', 'state', 'boundary', 'bbox', 'unit', 'diagnostics')}
            public.update(kind='axis_region', tool=name, axis_ids=list(args.axis_ids), spatial_fact_scope='candidate_axis_region')
            rows = self.task.data.setdefault('spatial_results', [])
            rows[:] = [row for row in rows if row.get('axis_ids') != list(args.axis_ids)]
            rows.append(public)
            result['operation_receipts'] = [dict(operation='highlight', fulfilled=True)]
        return self._register_tree(result)
    if name in ("get_drawing_context", "read_dimensions", "query_building_relations", "get_component_evidence"):
        service = ComponentService(self.w, snapshot)
        if name == "get_component_evidence":
            return self._register_tree(service.page(service.get(args.record_id), args.view, args.offset, args.limit,
                page_call_budget=component_page_call_budget(self)))
        kind = {"get_drawing_context":"context", "read_dimensions":"dimensions", "query_building_relations":"relations"}[name]
        record = service.build(kind, args)
        return self._register_tree(service.page(record, getattr(args, "view", "dimensions"), args.offset, args.limit,
            page_call_budget=component_page_call_budget(self)))
    if name in ('render_region', 'bind_visual_objects'):
        if self.w.settings.provider != 'responses_subscription' or self.request.runtime not in ('responses_subscription', 'codex'):
            raise ValueError('当前通道尚未支持图片输入。')
        vision = VisionService(self.w, snapshot)
        if name == 'render_region':
            overview_count=sum(v.get('kind') not in ('geometry_review_sheet','boundary_contact_sheet') for v in self.visual_images.values())
            if overview_count >= (4 if self.task.complex else 2):
                raise ValueError('本次局部图像额度已用完。')
            image = vision.render(args)
            self.visual_images[image['image_id']] = image
            return image | {'delivery': 'attached_to_next_model_request',
                'view_scope': 'local_source_preview', 'engineering_scope_confirmed': False,
                'scope_note': '此图只展示当前快照和布局的明确选区；不确认楼层归属、房间身份或测量范围。局部名称原文可单独摘录，工程范围疑义另列。'}
        if args.image_id not in self.sent_image_ids:
            raise ValueError('图像尚未发送到当前模型。')
        binding = vision.bind(args)
        self.visual_bindings[binding['binding_id']] = binding
        self.reviewed_image_ids=getattr(self,'reviewed_image_ids',set())|{args.image_id}
        return binding
    if name in ("read_geometry", "query_spatial", "measure_geometry", "measure_room", "get_measurement_evidence", "discover_rooms", "get_room_contours", "measure_room_area"):
        geometry = GeometryService(self.w, snapshot)
        if name == "get_room_contours":
            return geometry.room_contours(args)
        if name == "measure_room_area":
            return geometry.summary(geometry.room_area(args))
        if name == "discover_rooms":
            from cad_agent.geometry.tiled_discovery import discover_with_subdivision
            return geometry.discovery_summary(discover_with_subdivision(geometry,args), args.offset, args.limit)
        if name == "read_geometry":
            result = geometry.read(args)
            from cad_agent.analysis.evidence_store import save_session
            result["full_geometry_record"] = save_session(self, "full_geometry",
                dict(snapshot_id=snapshot, source_sha256=self.drawing['sha256'],
                     items=geometry.records([item["entity_id"] for item in result["items"]]), unit=self.drawing["unit"],
                     note="完整源几何记录；曲线仍按解析器细分，单位及对象身份规则不变。"))
            return result
        if name == "query_spatial":
            return geometry.query(args)
        if getattr(args, 'binding_id', '') and args.binding_id not in self.visual_bindings:
            raise ValueError('绑定尚未在本次会话中读取。')
        record = (geometry.room(args) if name == "measure_room" else geometry.measure(args, unit_policy=self.task.data.get('unit_policy'))
                  if name == "measure_geometry" else geometry.get(args.measurement_id))
        if record["kind"] == "room_discovery":
            return geometry.discovery_summary(record)
        result = geometry.summary(record)
        if record.get("label_evidence"):
            result["label_evidence"] = self._register(record["label_evidence"])
        if name == "get_measurement_evidence":
            selected = record.get("selected", {})
            result["boundary"] = selected.get("boundary", record.get("boundary", []))[:128]
            result["edge_evidence"] = selected.get("edge_evidence", [])[:24]
            result["holes"] = [ring[:128] for ring in selected.get("holes", [])[:12]]
            result["source_entity_ids"] = selected.get("entity_ids", [])[:64]
            result["inferred_closures"] = selected.get("inferred_closures", [])[:12]
            result["evidence_truncated"] = len(selected.get("boundary",[]))>128 or len(selected.get("edge_evidence",[]))>24 or len(selected.get("inferred_closures",[]))>12 or len(selected.get("entity_ids",[]))>64 or len(selected.get("holes",[]))>12 or any(len(r)>128 for r in selected.get("holes",[]))
            result["measurement_line"] = record.get("measurement_line")
        return result
    if name == "get_drawing_overview":
        if args.view != "titles":
            return self._register_tree(
                self.index.outline(
                    args.view,
                    args.layout_id,
                    args.frame_id,
                    args.offset,
                    args.limit,
                )
            )
        self.index.scope(args.layout_id, args.frame_id)
        result = drawing_overview(
            self.w,
            self.drawing,
            args.layout_id,
            args.frame_id,
            args.offset,
            args.limit,
        )
        return self._register_tree(result)
    if name == "search_sources":
        if any(len(k) > 150 for k in args.keywords):
            raise ValueError("关键词过长")
        result = self.index.search(**args.model_dump(), mode=self.request.retrieval_mode,
            prioritize_headings=bool(re.search('做法|构造|面层|砂浆|层次', self.request.question)))
        if result.get("retrieval"):
            self.retrieval_usage.append(result["retrieval"])
        return result
    if name == "read_table":
        return self._register_tree(self.index.read_table(**args.model_dump()))
    if name == "query_entities":
        return self.index.entities(**args.model_dump())
    if name == "inspect_region":
        return self._register_tree(self.index.inspect(**args.model_dump()))
    if name == "read_source_context":
        result = source_context(self.w, snapshot, **args.model_dump(), bounded=bounded)
        from cad_agent.analysis.evidence_store import save_session
        result["original_source_record"] = save_session(self, "original_source",
            self.index.by_id[args.region_id].model_dump())
        return self._register_tree(result)
    if name == "extract_facts":
        if args.extractor_ref != "building.masonry@1":
            data = extract_general(self.index, args.extractor_ref, args.frame_ids)
            return {
                k: data[k]
                for k in ("fact_set_id", "extractor_ref", "cached", "coverage")
            } | {"fact_count": len(data["facts"])}
        job = self.w.start_extract(
            ExtractionRequest(snapshot_id=snapshot, frame_ids=args.frame_ids)
        )
        while job["status"] in ("queued", "running"):
            self._remaining()
            time.sleep(0.05)
            job = self.w.store.get("job", job["id"])
        if job["status"] == "failed":
            raise ValueError("本地抽取失败，请检索原文。")
        self.fact_set_id = job["result"]["fact_set_id"]
        data = self._facts()
        return {
            "fact_set_id": self.fact_set_id,
            "practice_count": len(data["practices"]),
            "coverage": compact_coverage(data["coverage"]),
            "cached": job.get("cached", False),
        }
    if name == "query_facts":
        if args.fact_type != "building.masonry.v1":
            if args.wall_role:
                raise ValueError("通用事实不支持砌体墙类筛选")
            return query_general(
                self.index, **args.model_dump(exclude={"wall_role"})
            )
        if args.subject or args.property:
            raise ValueError("砌体事实请用 query 或 wall_role 筛选")
        if not self.fact_set_id:
            return {"items": [], "total": 0, "needs_extraction": True}
        data = self._facts()
        if args.frame_id and args.frame_id not in {
            r.frame_id for r in self.w.regions(snapshot)
        }:
            raise ValueError("图框不属于当前图纸。")
        result = self.w.facts(
            self.fact_set_id,
            **args.model_dump(exclude={"fact_type", "subject", "property"}),
        )
        result["items"] = [
            {
                k: p[k]
                for k in (
                    "practice_id",
                    "source_region_id",
                    "original_text",
                    "attributes",
                    "evidence_ids",
                    "missing_fields",
                    "conflicts",
                )
            }
            for p in result["items"]
        ]
        # Leave room for further tool calls on very large drawings.
        for p in result["items"]:
            p["original_text"] = p["original_text"][:1500]
        result.pop("manifest", None)
        result["coverage"] = compact_coverage(result["coverage"])
        result["diagnostic_count"] = len(result["diagnostics"])
        result["diagnostics"] = result["diagnostics"][:5]
        result["fact_set_id"] = self.fact_set_id
        result["has_more"] = args.offset + len(result["items"]) < result["total"]
        return result
    if name == "get_evidence":
        available = dict(self.evidence)
        for data in self.w.store.list("generic_fact_set"):
            if data["snapshot_id"] == snapshot:
                available.update({e["evidence_id"]: e for e in data["evidence"]})
        if self.fact_set_id:
            available.update(
                {e["evidence_id"]: e for e in self._facts()["evidence"]}
            )
        if any(eid not in available for eid in args.evidence_ids):
            raise ValueError("证据不存在或不属于当前事实集合。")
        return {
            "items": [self._register(available[eid]) for eid in args.evidence_ids]
        }


def invoke_tool(self, name, raw_args):
    self._remaining()
    if self.calls >= self.w.settings.agent_max_tool_calls or self.policy.stop_reason:
        return json.dumps(
            {
                "error": {
                    "code": "AGENT_TOOL_LIMIT" if self.calls >= self.w.settings.agent_max_tool_calls else "AGENT_POLICY_STOP",
                    "message": "工具额度已用完，请根据已有证据回答。" if self.calls >= self.w.settings.agent_max_tool_calls else "探索策略停止："+str(self.policy.stop_reason),
                    "stop_reason": self.policy.stop_reason,
                }
            },
            ensure_ascii=False,
        )
    if name == 'batch_read_sources' and self.request.source_read_batching:
        from cad_agent.analysis.batch_reads import BatchReadSources, invoke_batch
        try:
            if len(dumps(raw_args)) > 16000:
                raise ValueError('批量参数过长')
            batch = BatchReadSources.model_validate(json.loads(raw_args) if isinstance(raw_args, str) else raw_args, strict=True)
        except (ValueError, TypeError):
            pass  # Invalid envelopes use the normal counted error path below.
        else:
            return invoke_batch(self, batch)
    self.calls += 1
    from cad_agent.analysis.failure_guard import blocked
    if blocked(self, name, raw_args):
        error = dict(code="REPEATED_TOOL_FAILURE", message="相同参数已连续两次触发同一校验错误且无新证据。已阻止再次执行；改查缺口或按已知事实部分交付。")
        self.trace.append(dict(tool=name, status="failed", error=error, repeated_execution_blocked=True))
        packet = deepcopy(getattr(self, '_blocked_failure_feedback', {}))
        packet['error'] = error
        owners = set(packet.get('affected_task_ids', []))
        # A stalled branch must not terminate unrelated executable work.
        independent = [sid for sid in self.task.runnable() if sid not in owners]
        if not independent:
            self.policy.stop_reason = "repeated_tool_failure"
        packet['repair_context'] = dict(state_unchanged=True)
        encoded = dumps(packet)
        self.policy.response_chars += len(encoded)
        return encoded
    tick = time.monotonic()
    previous_evidence = dict(self.evidence)
    previous_observations = dict(self.observations)
    registry_fields = ('query_intent', 'source_candidates', 'query_searches', 'query_context_observation_ids', 'object_registry', 'object_workflows', 'source_recall', 'source_sequences', 'spatial_objects', 'spatial_results', 'table_reads')
    registry_before = {k: deepcopy(self.task.data[k]) for k in registry_fields if k in self.task.data}
    def rollback_registry():
        for key in registry_fields:
            if key in registry_before: self.task.data[key] = registry_before[key]
            else: self.task.data.pop(key, None)
    entry = {"tool": name, "status": "running"}
    duplicate = False
    cache_key = None
    new_evidence = set()
    full_result = {}
    args = {}
    result_view = "auto"
    task_ids=[]
    tool_supported = False
    batch_allowed = getattr(self, '_batch_read_allowed_tools', None)
    self.trace.append(entry)
    self.progress(stage=f"正在执行 {name}", trace=self.trace, usage=self.usage)
    try:
        from cad_agent.analysis.tool_registry import require_supported
        if name == 'batch_read_sources' and not self.request.source_read_batching:
            raise ValueError('SOURCE_READ_BATCHING_DISABLED')
        require_supported(name, self.request.runtime, self.w.settings.provider)
        tool_supported = True
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        args = dict(args)
        from cad_agent.analysis.query_intent import prepare_call
        intent = prepare_call(self, name, args)
        if intent:
            entry['query_intent_id'] = intent['intent_id']
        from cad_agent.analysis.source_candidates import check_read
        check_read(self, name, args)
        from cad_agent.analysis.payload_projection import VIEWS, PROJECTABLE_TOOLS, selected_view
        result_view = args.pop('result_view', 'auto')
        if result_view not in VIEWS:
            raise ValueError('result_view 必须为 auto、summary 或 full。')
        task_ids = args.pop('task_ids',None)
        entry.update(arguments=args,task_ids=task_ids or [], result_view=result_view)
        from cad_agent.analysis.batch_reads import validate_batch_owner
        validate_batch_owner(self)
        if task_ids and name not in CONTROL:
            # Already delivered owners can be cited alongside active owners;
            # do not reject the whole operation because of that redundant tag.
            active=[i for i in task_ids if any(s['id']==i and not (s['status']=='completed' and s['delivered']) for s in self.task.data['subtasks'])]
            if active:task_ids=active
        if self.focused_enabled and name not in CONTROL and name != 'calculate_facts':
            from cad_agent.analysis.calculation_workflow import execution_reviews
            reviews = execution_reviews(self.task.data)
            if reviews:
                raise ValueError('CALCULATION_REVIEW_REQUIRED：'+dumps(reviews[0]))
        if self.focused_enabled and name in ('search_sources', 'query_entities'):
            from cad_agent.analysis.requirement_sources import required_reviews
            source_reviews = required_reviews(self.task.data)
            if source_reviews:
                raise ValueError('SOURCE_REQUIREMENT_REVIEW_REQUIRED：'+dumps(source_reviews[0]))
        if name not in CONTROL:
            if not self.task.complex:self.task.auto_plan()
            task_ids = task_ids or ([self.task.parts[0]['id']] if not self.task.complex else [])
            self.task.select(task_ids)
        entry["task_ids"] = task_ids or []
        batch_allowed = getattr(self, '_batch_read_allowed_tools', None)
        if batch_allowed is not None and name not in batch_allowed:
            if name not in CONTROL:
                self.task.start(task_ids, action_key=self.policy.key(name, args))
            raise ValueError('此批次开始时该读取工具尚未开放。')
        from cad_agent.analysis.scope_workflow import bind_scope_call, record_scope_call, selected_for_call
        call_scope = selected_for_call(self, name)
        if call_scope is not None:
            original = TOOL_SPECS[name][0].model_validate(args, strict=True)
            bound = bind_scope_call(self, name, original)
            # Preserve omitted defaults until the existing budget policy applies.
            args.update({k:v for k,v in bound.model_dump().items() if v != getattr(original,k)})
        if self.policy.enabled:
            if name not in TOOL_SPECS:
                raise ValueError("Unknown tool")
            # Apply the same defaults advertised to the model.
            args = dict(args)
            if "limit" in TOOL_SPECS[name][0].model_fields:
                args.setdefault("limit", 12 if name in {"analyze_room_batch", "read_assembly"} else 5)
            args = TOOL_SPECS[name][0].model_validate(args, strict=True).model_dump()
            cache_key = self.policy.key(name, args)
            if result_view != 'auto' or name in PROJECTABLE_TOOLS:
                cache_key += ':result_view=' + result_view + ':' + selected_view(self, name, task_ids or [], result_view)
            if call_scope is not None:
                # A cached observation belongs to its requirement owners.
                cache_key += ':' + call_scope['scope_id'] + ':' + ','.join(sorted(task_ids or []))
        if name == 'read_assembly' and self.focused_enabled:
            from cad_agent.analysis.read_context import matching_read_page
            prior = matching_read_page(self.observations, args)
            if prior:
                raise ValueError('SOURCE_PAGE_ALREADY_READ：此构造页已返回；复用 observation_id='+prior['observation_id']+'，补读 next_offset 或其他未读对象，不要改变页大小重读同一页。')
        if name not in CONTROL:
            self.task.start(task_ids, action_key=self.policy.key(name, args))
        from cad_agent.analysis.tool_metadata import SPATIAL_METHODS
        revalidates_source = SPATIAL_METHODS | {
            'bind_spatial_object', 'get_spatial_object', 'resolve_drawing_scope',
            'query_property_evidence', 'trace_door_schedule', 'search_source_records',
            'search_tables', 'inspect_table', 'read_table_content', 'recover_table_region', 'review_table_region',
        }
        if name not in CONTROL and name not in revalidates_source and cache_key in self.policy.cache:
            duplicate = True
            full_result = self.policy.cache[cache_key]
            result = {
                "already_returned": True,
                "note": "相同参数已执行，原结果在前面的工具消息中；不重复检索或读取。",
                "evidence_ids": sorted(evidence_ids(full_result)),
                "observation_id": full_result.get("observation_id"),
                "items": [{k: i[k] for k in ("region_id", "snippet_start", "next_start") if k in i}
                          for i in full_result.get("items", [])],
            }
            if full_result.get('kind')=='relations':
                result.update({k:full_result[k] for k in ('record_id','view','offset','total','requested_limit','effective_limit','safe_limit','next_offset','has_more','pagination','pagination_budget') if k in full_result})
        else:
            if name=='request_toolset':
                result=self.focus.expand(ToolsetRequest.model_validate(args),task_ids or [self.task.parts[0]['id']])
            elif name=='finish_analysis':
                previous_stop=self.policy.stop_reason
                self.policy.stop_reason='evidence_sufficient'
                try:
                    self.prepared_answer=self.finish(dumps(args))
                    if self.prepared_answer['task']['status']!='completed' or any(c['state']=='unknown' for c in self.prepared_answer['claims']):
                        self.policy.stop_reason='partial_delivery'
                        self.prepared_answer['efficiency']['stop_reason']='partial_delivery'
                    result={'accepted':True,'stop_reason':self.policy.stop_reason,'task_status':self.prepared_answer['task']['status']}
                except ValueError:
                    self.policy.stop_reason=previous_stop
                    raise
            else:
                if batch_allowed is not None and name in ('read_source_context', 'read_assembly'):
                    from cad_agent.analysis.batch_reads import execute_sized
                    result = execute_sized(self, name, args)
                    # Adaptive pagination changes the executed cursor's page size.
                    # Cache only that exact page, never the original larger one.
                    if self.policy.enabled:
                        cache_key = self.policy.key(name, args)
                        if result_view != 'auto' or name in PROJECTABLE_TOOLS:
                            cache_key += ':result_view=' + result_view + ':' + selected_view(self, name, task_ids or [], result_view)
                        if call_scope is not None:
                            cache_key += ':' + call_scope['scope_id'] + ':' + ','.join(sorted(task_ids or []))
                else:
                    result = self.execute(name, args, bounded=self.policy.enabled)
            if name=='plan_task' and self.focused_enabled:result=self.focus.state()
            from cad_agent.analysis.payload_projection import project_result
            result = project_result(self, name, args, result, task_ids or [], result_view)
            from cad_agent.analysis.object_registry import record_tool_result
            registry_update = record_tool_result(self, name, args, result)
            if registry_update:
                result.update(registry_update)
            record_scope_call(self, name, args, result)
            self.register_observation(name, result)
            from cad_agent.analysis.source_candidates import record_read
            record_read(self, name, args, result)
            full_result = result
            new_evidence = evidence_ids(result) - self.policy.seen_evidence
        if duplicate and name in ('search_sources','query_entities'):
            from cad_agent.analysis.recall import record as record_recall
            result.update(record_recall(self,name,args,full_result))
        entry.update(
            status="completed",
            arguments=args,
            task_ids=task_ids or [],
            result_summary={
                k: result[k]
                for k in (
                    "total",
                    "has_more",
                    "practice_count",
                    "fact_set_id",
                    "needs_extraction",
                    "measurement_id", "status", "area_m2", "candidate_area_m2", "basis_area_m2", "measurement_basis", "value", "diagnostics",
                    "retrieval",
                    "image_id", "binding_id", "identity_status", "delivery",
                )
                if k in result
            },
            source_ids=[
                i.get("region_id")
                or i.get("source_region_id")
                or i.get("evidence_id")
                for i in result.get("items", [])
            ],
        )
    except (ValueError, KeyError, TypeError) as error:
        rollback_registry()
        self.evidence = previous_evidence
        self.observations = previous_observations
        result = {
            "error": {
                "code": "INVALID_TOOL_INPUT",
                "message": "工具参数、来源或范围无效；仅允许当前图纸的 CAD 工具。",
            }
        }
        if str(error) == 'SOURCE_READ_BATCHING_DISABLED':
            result['error']={'code':'SOURCE_READ_BATCHING_DISABLED','message':'批量读取已关闭，请逐项调用 read_source_context/read_assembly；仅用户可在问答设置中开启。'}
        elif str(error).startswith('COMPONENT_ITEM_TOO_LARGE'):
            result['error']={'code':'COMPONENT_ITEM_TOO_LARGE','message':str(error)[:400]}
        elif name != 'finish_analysis' and str(error).startswith('QUERY_'):
            result['error']={'code':str(error).split(':',1)[0], 'message':str(error)[:700]}
        elif name=='finish_analysis':
            self.record_validation_issue(error)
            result['error']['message']=self.validation_issue
            if getattr(self,'scope_recovery_feedback',None):
                result['recovered_batches']=self.scope_recovery_feedback
        elif name in ('inspect_scope_objects','read_assembly','calculate_facts','resolve_drawing_scope','read_project_sources','request_toolset','prepare_space_connections','review_space_connections','prepare_discovery_review','review_discovered_spaces','prepare_room_boundaries','select_room_boundaries','analyze_measurements','get_analysis_results','check_dimension_chain','inspect_enclosure','render_region','bind_visual_objects','get_drawing_context','query_building_relations','discover_rooms','read_dimensions','measure_geometry','get_room_contours','measure_room_area') and not str(error).startswith(('SOURCE_PAGE_ALREADY_READ','CALCULATION_REVIEW_REQUIRED')):
            result['error']['message']=str(error)[:400]
        elif str(error).startswith('SOURCE_PAGE_ALREADY_READ'):
            result['error']['code']='SOURCE_PAGE_ALREADY_READ'
            result['error']['message']=str(error)
            from cad_agent.analysis.recall import pending_calls
            result['next_source_actions']=pending_calls(self.task.data)[:4]
        elif str(error).startswith('SOURCE_REQUIREMENT_REVIEW_REQUIRED'):
            result['error']['code']='SOURCE_REQUIREMENT_REVIEW_REQUIRED'
            result['error']['message']='继续扩大检索前，先补读问题中明确要求但尚未读取的条件条款。'
            result['next_source_actions']=source_reviews[:2]
        elif str(error).startswith('CALCULATION_REVIEW_REQUIRED'):
            result['error']['code']='CALCULATION_REVIEW_REQUIRED'
            result['error']['message']='已有可用单位和来源数值。先调用 calculate_facts 明确执行或 missing_operands；之后继续其他来源读取。'
            result['required_calculation_review']=reviews[0]
        elif name=='plan_task':
            from cad_agent.analysis.planning import plan_error_feedback
            result['error']=plan_error_feedback(error)
        elif self.task.complex and not task_ids:
            result['error']['message']='先通过 plan_task 覆盖所有 requirement_id；工具参数须包含有效 task_ids。'
        from cad_agent.analysis.batch_reads import batch_error_feedback
        result['error'] = batch_error_feedback(self, error, result['error'])
        if name == 'render_region':
            from cad_agent.analysis.scope_workflow import record_scope_failure
            record_scope_failure(self, name, args, result)
        from cad_agent.analysis.failure_guard import register_failure
        from cad_agent.analysis.error_feedback import field_issues, build_feedback
        result['repair_context'] = dict(field_issues=field_issues(error, self, name))
        if not result['repair_context']['field_issues']:
            result['error']['detail'] = str(error)[:600]
        register_failure(self, name, args, result)
        if self.policy.enabled and tool_supported:
            result = build_feedback(self, name, args, result, error, task_ids or [])
            self._last_error_feedback = result
            self._failed_calls[self._last_failure_call_key]['feedback'] = deepcopy(result)
        elif result.get('recovered_batches'):
            from cad_agent.analysis.evidence_store import save_session
            result['recovered_batches'] = save_session(self, 'scope_recovery', result['recovered_batches'])
        entry.update(status="failed", error=result["error"])
        if result.get('error_record'):
            entry.update(error_record=result['error_record'],
                         affected_task_ids=result['affected_task_ids'], retry=result.get('retry'))
    archive_ref = None
    if entry["status"] == "completed" and not duplicate and name not in CONTROL:
        from cad_agent.analysis.evidence_store import save_session, working_result
        if result.get('payload_projection'):
            # This archive contains the pre-projection tool return. Never replace
            # it with the smaller observation, or excerpt away candidate rows.
            archive_ref = result['evidence_record']
        else:
            archive_ref = save_session(self, name, result)
            result = working_result(result, archive_ref,
                assembly_pages=getattr(self.w.settings, 'model_context_codec', True)
                ) if self.stable_prefix_enabled and result_view != 'full' else result | {"evidence_record": archive_ref}
    # The deterministic host retrieves the complete canonical observation
    # directly. Do not serialize geometry/duplicate citations into its receipt.
    from cad_agent.analysis.source_records import fixed_read_receipt
    if entry['status']=='completed' and (receipt:=fixed_read_receipt(self,name,result)) is not None:
        result=receipt
    before_chars = len(json.dumps(result, ensure_ascii=False))
    visible = set(self.policy.seen_evidence)
    encoded = dumps(compact(result, visible)) if self.policy.enabled else json.dumps(result, ensure_ascii=False)
    if entry["status"] == "failed" and result.get('error_record') and len(encoded) > getattr(self, '_tool_response_limit', 50000):
        ref = result['error_record']
        encoded = dumps(dict(error={k: str(result['error'][k])[:300] for k in ('code','message')},
                             error_record=ref, affected_task_ids=result['affected_task_ids'],
                             next_read=dict(tool='read_evidence_record',arguments=dict(record_id=ref['record_id']))))
    if len(encoded) > getattr(self, '_tool_response_limit', 50000):
        rollback_registry()
        self.evidence = previous_evidence
        self.observations = previous_observations
        error = {
            "code": "RESULT_TOO_LARGE",
            "message": "结果过大，请缩小范围或减小 limit。",
        }
        encoded = json.dumps({"error": error, **({"evidence_record":archive_ref,
            "next_read":{"tool":"read_evidence_record","arguments":{"record_id":archive_ref["record_id"]}}}
            if archive_ref else {})}, ensure_ascii=False)
        if batch_allowed is not None:
            fallback = dict(error=error, retry_via='original_tool',
                next_actions=[dict(tool=name, arguments=args, task_ids=task_ids or [])])
            if len(dumps(fallback)) <= getattr(self, '_tool_response_limit', 50000):
                encoded = dumps(fallback)
        entry.update(status="failed", error=error)
    core_encoded = encoded
    if entry["status"] == "failed":
        # Failed calls consume model context too; never disguise them as progress.
        self.policy.response_chars += len(encoded)
        if task_ids:
            from cad_agent.analysis.gap_routing import record_result
            record_result(self, name, task_ids, result | {"error": entry["error"]})
            self.task.save()
    if entry["status"] == "completed":
        self.coverage.record(name, full_result, task_ids=task_ids or [])
        self.task.data['source_read_queue']=[dict(a,task_ids=self.coverage.owners(a)) for a in self.coverage.pending()]
        if self.policy.enabled:
            self.policy.seen_evidence = visible
            if not duplicate and name not in CONTROL and name != "extract_facts":
                self.policy.cache[cache_key] = full_result
        if full_result.get("kind") in ("relations", "scope_object_candidates", "spatial_object", "spatial_method"):
            self.policy.geometry_work = True
        from cad_agent.analysis.source_records import fixed_page_progress
        fresh_source_page = fixed_page_progress(self, name, full_result)
        if name not in CONTROL:self.policy.record(name, full_result, new_evidence, duplicate, len(encoded))
        else:self.policy.response_chars += len(encoded)
        if fresh_source_page:
            entry['source_traversal_progress']='new_canonical_page'
            if self.policy.stop_reason=='no_new_information':self.policy.stop_reason=None
        if name not in CONTROL:
            batch=self.room_batches.get(full_result.get('batch_id')) if name in ('analyze_room_batch','select_room_boundaries','resolve_room_units') else None
            if name=='resolve_room_units' or (batch and batch.get('unit_resolution')):
                task_ids=list(set(task_ids)|{s['id'] for s in self.task.data['subtasks'] if s['kind']=='constraint'})
            self.task.record(task_ids,name,full_result,
                {eid:self.evidence[eid] for eid in evidence_ids(full_result) if eid in self.evidence},self.observations,batch,duplicate, action_key=self.policy.key(name, args),
                track_progress=self.policy.enabled, defer_stall=batch_allowed is not None or fresh_source_page)
            deltas = [s['last_progress'] for s in self.task.data['subtasks'] if s['id'] in task_ids and s.get('last_progress')]
            if deltas and self.policy.enabled and batch_allowed is None:
                entry['requirement_progress'] = deltas
                self.policy.stalled = min(d['stalled'] for d in deltas)
                if all(d['kind'] == 'no_progress' and d['stalled'] >= 3 for d in deltas) and not self.task.runnable():
                    self.policy.stop_reason = 'no_requirement_progress'
                elif self.policy.stop_reason == 'no_new_information':
                    self.policy.stop_reason = None
            self.focus.record(name,task_ids,full_result)
            if self.policy.stop_reason=='no_new_information' and self.task.runnable():
                self.policy.stop_reason=None
            if name=='analyze_room_batch' and not self.task.runnable() and not (self.focused_enabled and self.focus.pending_pages()):
                self.policy.stop_reason='room_batch_'+full_result['status']
            if self.task.complex and batch_allowed is None:
                result['task']=self.focus.state() if self.focused_enabled else self.task.context()
                encoded=dumps(compact(result))
        if self.policy.enabled and batch_allowed is None:
            wire = json.loads(encoded)
            wire["next_action"] = self.policy.hint()
            wire["evidence_coverage"] = self.coverage.summary()
            updated = dumps(wire)
            self.policy.response_chars += len(updated) - len(encoded)
            encoded = updated
    if len(encoded) > getattr(self, '_tool_response_limit', 50000):
        # The primary packet was checked above; state is already durably recorded.
        # Omit only extra task/hint metadata, never source/measurement content.
        encoded = core_encoded
        entry['supplemental_context_omitted'] = True
    if self.policy.enabled:
        entry["efficiency"] = {
            "duplicate": duplicate, "new_evidence_count": len(new_evidence),
            "before_compaction_chars": before_chars, "response_chars": len(encoded),
            "stalled_calls": self.policy.stalled, "stop_reason": self.policy.stop_reason,
        }
    entry["elapsed_seconds"] = round(time.monotonic() - tick, 3)
    self.journal.tool_result(args, full_result or result, encoded, entry, archive_ref)
    if self.request.adaptation_policy_id and not duplicate and entry['status']=='completed':
        # Only the finalized normal read is visible to the Agent. Generated
        # proposals, diagnostics and candidates remain in separate audit records.
        from cad_agent.extensions.adaptation.dispatch import enqueue_read
        enqueue_read(self.w,self.request.adaptation_policy_id,self.request.snapshot_id,name,args)
    self.progress(stage=f"已执行 {name}", trace=self.trace, usage=self.usage)
    return encoded

