from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
import json

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cad_agent.config import Settings, ROOT
from cad_agent.runtimes.budget_policy import policy_for
from cad_agent.contracts import ExtractionRequest, ChatRequest
from cad_agent.service import Workspace
from cad_agent.demo import create_demo
from cad_agent.adapters.model import ModelError
from cad_agent.adapters.oda import ConversionError
from cad_agent.analysis.agent import source_context, tool_definitions
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.room_batch import RoomBatchQuery, UnitConfirmation, ScopeConfirmation, BatchPage, RoomBatchService
from cad_agent.analysis.tasks import TaskLedger, TaskUnitConfirmation, TaskBatchNavigation
from cad_agent.semantics.service import SemanticService
from cad_agent.semantics.contracts import AnnotationQuery, AxisLocation, DetailTrace, SemanticTable
from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest, BuildingScheduleRequest
from cad_agent.analysis.business_tables import extract_planning_metrics, extract_building_schedule
from cad_agent.components.contracts import ContextQuery, DimensionQuery, RelationQuery
from cad_agent.components.service import ComponentService
from cad_agent.geometry.contracts import GeometryRead, SpatialQuery, GeometryMeasure, RoomMeasure, RoomDiscover, RoomContoursRead, RoomAreaMeasure


def create_app(settings=None):
    settings = settings or Settings.load()
    workspace = Workspace(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        workspace.close()

    app = FastAPI(
        title="CAD Atlas · 通用图纸事实 API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.workspace = workspace
    from cad_agent.extensions.table_recovery.api import router as recovery_router
    app.include_router(recovery_router(workspace))
    from cad_agent.tables.api import router as table_router
    app.include_router(table_router(workspace))
    from cad_agent.extensions.adaptation.api import router as adaptation_router
    app.include_router(adaptation_router(workspace))
    from cad_agent.analysis.review_api import router as review_router
    app.include_router(review_router(workspace))
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=3)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and origin:
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in (
                "http",
                "https",
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "ORIGIN_REJECTED",
                            "message": "仅接受当前工作台发起的操作。",
                        }
                    },
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "记录不存在，或不属于当前文件版本。",
                }
            },
        )

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "INVALID_INPUT", "message": str(exc)}},
        )

    @app.exception_handler(ModelError)
    async def model_failure(request, exc):
        return JSONResponse(
            status_code=503,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ConversionError)
    async def conversion_failure(request, exc):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"code": exc.code, "message": exc.message}})

    @app.get("/api/health")
    def health():
        from cad_agent.analysis.session import AGENT_VERSION
        return {
            "status": "ok",
            "version": "0.1.0",
            "agent_version": AGENT_VERSION,
            "agent_limits": {"timeout_seconds":settings.agent_timeout_seconds,"max_tool_calls":settings.agent_max_tool_calls,"base_max_rounds":settings.agent_max_rounds},
            "model": workspace.gateway.status(),
            "supported_formats": ["dxf", "dwg"],
            "dwg_conversion": workspace.converter.status(),
            "policies_enabled": False,
            "codex_subscription": {"supported": True, "local_only": True},
            "siwc_subscription": {"supported": True, "local_only": True},
            "retrieval": {
                "modes": ["keyword", "lexical", "hybrid"],
                "default": "keyword",
                "model_files_present": (
                    settings.embedding_model_dir / "cad-model.json"
                ).is_file(),
            },
        }

    @app.get("/api/runtimes")
    def runtimes():
        from cad_agent.runtimes.external import runtime_status

        return {
            "items": runtime_status(),
            "default": "native",
            "model": settings.model,
            "code_execution_enabled": False,
            "task_code_capabilities": {
                "authorization": "explicit_question_or_request_switch",
                "runtime": "responses_subscription", "model": "gpt-6.1-sol",
                "editable_tools": ["read_assembly.source_spans"],
                "programs": "versioned_multi_module_contracts",
                "global_tool_mutation": False,
            },
            "automatic_fallback_default": False,
        }

    @app.get('/api/tool-capabilities')
    def tool_capabilities(runtime: str = 'native', model_profile: str | None = None):
        from cad_agent.analysis.tool_registry import capability_manifest
        profile=model_profile or settings.model_profile
        if profile not in workspace.model_catalog:
            raise ValueError('未知模型配置。')
        return {'items':capability_manifest(runtime=runtime,provider=workspace.model_catalog[profile].provider)}

    @app.get("/api/models")
    def models():
        labels = {
            "default": "默认",
            "api_sol": "API · GPT-6.1 Sol",
            "api_astra": "API · GPT-6 Astra",
            "qwen_large": "Qwen 大 · 长上下文",
            "qwen_medium": "Qwen 中",
            "qwen_small": "Qwen 小 · 短上下文",
            "codex_sol_56": "Codex 订阅 · GPT-5.6 Sol",
            "codex_sol": "Codex 订阅 · GPT-6.1 Sol",
            "siwc_sol": "标准 · GPT-6.1 Sol · Low",
            "siwc_sol_high": "深度 · GPT-6.1 Sol · High",
            "siwc_luna_high": "ChatGPT 直接订阅 · GPT-6 Luna · High",
            "codex_astra": "Codex 订阅 · GPT-6 Astra",
        }
        return {
            # Explicit default selection, independent of subscription login state.
            "default": "api_sol",
            "items": [
                {
                    "id": key,
                    "label": labels[key],
                    "model": value.model,
                    "reasoning_effort": value.reasoning_effort if value.provider == "responses_subscription" else None,
                    "context_length": value.context_length,
                    "configured": value.provider in ("codex_subscription", "responses_subscription") or bool(
                        value.api_base and value.api_key and value.model
                    ),
                    "billing": "subscription" if value.provider in ("codex_subscription", "responses_subscription") else "api",
                    "required_runtime": value.provider if value.provider in ("codex_subscription", "responses_subscription") else None,
                    "budget_policy": policy_for(value).public() if policy_for(value) else None,
                }
                for key, value in workspace.model_catalog.items()
                # Keep legacy profiles internally for existing runs, but retire
                # their entries from the user-facing model selector.
                if value.provider not in ("qwen", "codex_subscription")
            ],
        }

    @app.get("/api/codex-account")
    def codex_account():
        return workspace.codex_account.status()

    @app.post("/api/codex-account/login")
    def codex_login(flow: Literal["browser", "device"] = "browser"):
        return workspace.codex_account.login(flow)

    @app.post("/api/codex-account/logout")
    def codex_logout():
        return workspace.codex_account.logout()

    @app.get("/api/siwc-account")
    def siwc_account():
        return workspace.siwc_account.status()

    @app.post("/api/siwc-account/login")
    def siwc_login(account_id: str | None = None):
        return workspace.siwc_account.login(account_id)

    @app.post("/api/siwc-account/logout")
    def siwc_logout():
        return workspace.siwc_account.logout()

    @app.get("/api/siwc-account/models")
    def siwc_models():
        return workspace.siwc_account.models()

    @app.get("/api/drawings")
    def drawings():
        return {"items": workspace.store.list("drawing")}

    @app.post("/api/drawings", status_code=201)
    async def upload(file: UploadFile = File(...)):
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > settings.max_upload_bytes:
                raise HTTPException(413, "图纸超过 32 MB 上传上限。")
        return await run_in_threadpool(
            workspace.ingest, file.filename or "drawing.dxf", bytes(data)
        )

    @app.post("/api/demo")
    def demo():
        path = settings.data_dir / "demo.dxf"
        if not path.exists():
            create_demo(path)
        drawing = workspace.ingest(
            "示例 · 建筑设计总说明.dxf", path.read_bytes(), synthetic=True
        )
        job = workspace.start_extract(ExtractionRequest(snapshot_id=drawing["id"]))
        return {"drawing": drawing, "job": job}

    @app.get("/api/drawings/{snapshot_id}")
    def drawing(snapshot_id: str):
        return workspace.store.get("drawing", snapshot_id)

    @app.post("/api/drawings/{snapshot_id}/reindex", status_code=201)
    def reindex(snapshot_id: str):
        from cad_agent.cad.reindex import reindex_drawing
        return reindex_drawing(workspace, snapshot_id)

    @app.get("/api/drawings/{snapshot_id}/scene")
    def scene(snapshot_id: str):
        return workspace.preview_scene(snapshot_id)

    def display_packet(path, request, immutable=False):
        headers={"Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-store",
                 "Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"]="gzip"
            path=path.with_suffix('.json.gz')
        return FileResponse(path, media_type="application/json", headers=headers)

    @app.get("/api/drawings/{snapshot_id}/preview")
    def preview_manifest(snapshot_id: str, request: Request):
        from cad_agent.cad.preview import ensure_preview
        _,folder=ensure_preview(workspace,snapshot_id)
        return display_packet(folder/'manifest.json',request)

    @app.get("/api/drawings/{snapshot_id}/preview/{revision}/{packet}")
    def preview_chunk(snapshot_id: str, revision: str, packet: str, request: Request):
        from cad_agent.cad.preview import preview_file
        return display_packet(preview_file(workspace,snapshot_id,revision,packet),request,True)

    @app.get("/api/drawings/{snapshot_id}/source")
    def source(snapshot_id: str):
        d = workspace.store.get("drawing", snapshot_id)
        dwg = d.get("source_format") == "DWG"
        return FileResponse(
            workspace.store.artifact(snapshot_id, "source.dwg" if dwg else "source.dxf"),
            media_type="application/octet-stream" if dwg else "application/dxf",
            filename=d["name"],
        )

    @app.get("/api/drawings/{snapshot_id}/dxf")
    def download_dxf(snapshot_id: str):
        d = workspace.store.get("drawing", snapshot_id)
        return FileResponse(workspace.store.artifact(snapshot_id, "source.dxf"),
                            media_type="application/dxf", filename=Path(d["name"]).stem + ".dxf")

    @app.get("/api/drawings/{snapshot_id}/oda-json")
    def download_oda_json(snapshot_id: str):
        d = workspace.store.get("drawing", snapshot_id)
        if not d.get("conversion"):
            raise KeyError(snapshot_id)
        return FileResponse(workspace.store.artifact(snapshot_id, "oda.json"),
                            media_type="application/json", filename=Path(d["name"]).stem + ".oda.json")

    @app.get("/api/drawings/{snapshot_id}/entities/{entity_id}")
    def entity(snapshot_id: str, entity_id: str):
        workspace.store.get("drawing", snapshot_id)
        records = json.loads(
            workspace.store.artifact(snapshot_id, "entities.json").read_text()
        )
        result = next((e for e in records if e["entity_id"] == entity_id), None)
        if result is None:
            raise KeyError(entity_id)
        return result

    @app.get("/api/drawings/{snapshot_id}/sources")
    def search(
        snapshot_id: str,
        q: str = "",
        frame_id: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        regions = DrawingIndex(workspace, snapshot_id).regions
        if q:
            regions = [r for r in regions if q.casefold() in r.text.casefold()]
        if frame_id:
            regions = [r for r in regions if r.frame_id == frame_id]
        return {
            "items": [r.model_dump() for r in regions[offset : offset + limit]],
            "total": len(regions),
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/drawings/{snapshot_id}/retrieval")
    def retrieve(
        snapshot_id: str,
        q: str = Query("", max_length=150),
        mode: Literal["keyword", "lexical", "hybrid", "semantic"] = "keyword",
        layout_id: str = "",
        frame_id: str = "",
        section_id: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(8, ge=1, le=12),
    ):
        return DrawingIndex(workspace, snapshot_id).search(
            query=q,
            mode=mode,
            layout_id=layout_id,
            frame_id=frame_id,
            section_id=section_id,
            offset=offset,
            limit=limit,
        )

    @app.post("/api/extractions", status_code=202)
    def extraction(request: ExtractionRequest):
        return workspace.start_extract(request)

    @app.get("/api/drawings/{snapshot_id}/sources/{region_id}/context")
    def context(
        snapshot_id: str,
        region_id: str,
        neighbors: int = Query(1, ge=0, le=2),
        start: int = Query(0, ge=0),
    ):
        return source_context(workspace, snapshot_id, region_id, neighbors, start)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return workspace.store.get("job", job_id)

    @app.get("/api/fact-sets/{fact_set_id}")
    def facts(
        fact_set_id: str,
        q: str = "",
        wall_role: str = "",
        review_status: str = "",
        frame_id: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        return workspace.facts(
            fact_set_id, q, wall_role, review_status, frame_id, offset, limit
        )

    @app.get("/api/fact-sets/{fact_set_id}/export")
    def export(fact_set_id: str):
        return workspace.store.get("fact_set", fact_set_id)

    @app.get("/api/fact-sets/{fact_set_id}/evidence/{evidence_id}")
    def evidence(fact_set_id: str, evidence_id: str):
        facts = workspace.store.get("fact_set", fact_set_id)
        result = next(
            (e for e in facts["evidence"] if e["evidence_id"] == evidence_id), None
        )
        if result is None:
            raise KeyError(evidence_id)
        return result

    @app.post("/api/chat")
    def chat(request: ChatRequest):
        return workspace.answer(request)

    @app.post("/api/chat-runs", status_code=202)
    def start_chat(request: ChatRequest):
        return workspace.start_chat(request)

    @app.get('/api/drawings/{snapshot_id}/tasks')
    def drawing_tasks(snapshot_id: str):
        workspace.store.get('drawing',snapshot_id)
        records=[t for t in workspace.store.list('agent_task') if t['snapshot_id']==snapshot_id]
        records.sort(key=lambda t:t.get('updated_at',t['created_at']),reverse=True)
        return {'items':[dict(task_id=t['task_id'],question=t['question'],status=t['status']) for t in records[:10]]}

    @app.get('/api/tasks/{task_id}')
    def read_task(task_id: str):
        task=TaskLedger.__new__(TaskLedger);task.w=workspace;task.data=TaskLedger.read(workspace,task_id)
        return task.public()

    @app.get('/api/tasks/{task_id}/objects')
    def task_objects(task_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        from cad_agent.analysis.object_registry import registry_summary
        data=TaskLedger.read(workspace,task_id)
        objects=list(data.get('object_registry',{}).get('objects',{}).values())
        return {'items':objects[offset:offset+limit], 'offset':offset, 'limit':limit, 'total':len(objects), 'summary':registry_summary(data)}

    @app.get('/api/tasks/{task_id}/object-workflows')
    def task_object_workflows(task_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        data=TaskLedger.read(workspace,task_id)
        workflows=list(data.get('object_workflows',{}).values())
        return {'items':workflows[offset:offset+limit], 'offset':offset, 'limit':limit, 'total':len(workflows)}

    @app.post('/api/tasks/{task_id}/confirm-unit')
    def confirm_task_units(task_id: str, payload: TaskUnitConfirmation):
        return TaskLedger.confirm_unit(workspace,task_id,payload)

    @app.post('/api/tasks/{task_id}/room-batches/continue')
    def continue_task_batch(task_id: str, payload: TaskBatchNavigation):
        return TaskLedger.navigate_batch(workspace,task_id,payload)

    @app.get('/api/tasks/{task_id}/evidence/{evidence_id}')
    def task_evidence(task_id: str, evidence_id: str):
        return TaskLedger.read(workspace,task_id)['evidence'][evidence_id]

    @app.get('/api/tasks/{task_id}/evidence-records')
    def task_evidence_records(task_id: str, record_id: str = "", start: int = Query(0, ge=0),
                              length: int = Query(6000, ge=1, le=12000)):
        from cad_agent.analysis.evidence_store import EvidenceRecordRead, read_task_record
        data = TaskLedger.read(workspace, task_id)
        allowed = {data["snapshot_id"], *(d["snapshot_id"] for d in data.get("related_drawings", []))}
        return read_task_record(workspace, data, allowed, EvidenceRecordRead(
            view="read" if record_id else "catalog", record_id=record_id, start=start, length=length))

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: str):
        return workspace.execution.cancel(job_id, kind='job')

    @app.post('/api/chat-runs/{run_id}/cancel')
    def cancel_chat(run_id: str):
        return workspace.cancel_chat(run_id)

    @app.get('/api/execution')
    def execution_status():
        return workspace.execution_status()

    @app.get("/api/chat-runs/{run_id}")
    def chat_run(run_id: str):
        return workspace.store.get("chat_run", run_id)

    @app.get('/api/drawings/{snapshot_id}/chat-runs')
    def drawing_chat_runs(snapshot_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
        workspace.store.get('drawing', snapshot_id)
        runs = workspace.store.list('chat_run', filters={'snapshot_id': snapshot_id}, limit=limit, offset=offset)
        return {'items': [dict(id=r['id'], question=r['request']['question'], status=r['status'],
                              created_at=r['created_at']) for r in runs],
                'has_more': offset+len(runs) < workspace.store.count('chat_run', filters={'snapshot_id': snapshot_id})}

    @app.get('/api/chat-runs/{run_id}/events/{event_id}/sections/{section}')
    def run_event_section(run_id: str, event_id: str, section: str,
                          start: int = Query(0, ge=0), length: int = Query(12000, ge=1, le=12000)):
        from cad_agent.analysis.run_journal import read_section
        return read_section(workspace, run_id, event_id, section, start, length)

    @app.get('/api/chat-runs/{run_id}/journal-evidence/{evidence_id}')
    def run_journal_evidence(run_id: str, evidence_id: str):
        run = workspace.store.get('chat_run', run_id)
        if not any(evidence_id in e.get('evidence_ids', []) for e in run.get('run_events', [])):
            raise KeyError(evidence_id)
        task = TaskLedger.read(workspace, run['task_id'])
        source = task.get('evidence', {}).get(evidence_id)
        allowed = {run['snapshot_id'], *(d['snapshot_id'] for d in run['request'].get('related_drawings', []))}
        if not source or source.get('snapshot_id') not in allowed:
            raise KeyError(evidence_id)
        return source

    @app.get('/api/chat-runs/{run_id}/journal-export')
    def export_run_journal(run_id: str):
        from cad_agent.analysis.run_journal import export_journal
        run = workspace.store.get('chat_run', run_id)
        return StreamingResponse(export_journal(workspace, run), media_type='application/x-ndjson',
            headers={'Content-Disposition': 'attachment; filename="cad-run-journal.jsonl"'})

    @app.get("/api/chat-runs/{run_id}/evidence/{evidence_id}")
    def chat_evidence(run_id: str, evidence_id: str):
        run = workspace.store.get("chat_run", run_id)
        for e in (run.get("result") or {}).get("citations", []):
            if e["evidence_id"] == evidence_id:
                return e
        raise KeyError(evidence_id)

    @app.get("/api/drawings/{snapshot_id}/structure")
    def structure(
        snapshot_id: str,
        view: str = Query("sections", pattern="^(layouts|frames|sections|tables)$"),
        layout_id: str = "",
        frame_id: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(8, ge=1, le=12),
    ):
        return DrawingIndex(workspace, snapshot_id).outline(
            view, layout_id, frame_id, offset, limit
        )

    @app.get("/api/drawings/{snapshot_id}/tables/{table_id}")
    def table(
        snapshot_id: str,
        table_id: str,
        row_offset: int = Query(0, ge=0),
        row_limit: int = Query(3, ge=1, le=5),
    ):
        return DrawingIndex(workspace, snapshot_id).read_table(
            table_id, row_offset, row_limit
        )

    @app.get("/api/drawings/{snapshot_id}/sources/{region_id}/preview.svg")
    def preview(
        snapshot_id: str, region_id: str, padding: float = Query(1, ge=0, le=3)
    ):
        return Response(
            DrawingIndex(workspace, snapshot_id).svg(region_id, padding),
            media_type="image/svg+xml",
        )

    @app.get("/api/drawings/{snapshot_id}/sources/{region_id}/inspect")
    def inspect_region(
        snapshot_id: str, region_id: str, padding: float = Query(1, ge=0, le=3)
    ):
        return DrawingIndex(workspace, snapshot_id).inspect(region_id, padding)

    @app.post("/api/drawings/{snapshot_id}/semantics/query")
    def query_annotations(snapshot_id: str, payload: AnnotationQuery):
        return SemanticService(workspace, snapshot_id).query(payload)

    @app.post("/api/drawings/{snapshot_id}/semantics/locate")
    def locate_by_axes(snapshot_id: str, payload: AxisLocation):
        return SemanticService(workspace, snapshot_id).locate(payload)

    @app.post("/api/drawings/{snapshot_id}/semantics/trace")
    def trace_detail(snapshot_id: str, payload: DetailTrace):
        return SemanticService(workspace, snapshot_id).trace(payload)

    @app.post("/api/drawings/{snapshot_id}/semantics/table")
    def table_semantics(snapshot_id: str, payload: SemanticTable):
        return SemanticService(workspace, snapshot_id).table(payload)

    @app.post("/api/drawings/{snapshot_id}/business/planning-metrics")
    def planning_metrics(snapshot_id: str, payload: PlanningMetricsRequest):
        return extract_planning_metrics(workspace, snapshot_id, payload)

    @app.post("/api/drawings/{snapshot_id}/business/building-schedule")
    def building_schedule(snapshot_id: str, payload: BuildingScheduleRequest):
        return extract_building_schedule(workspace, snapshot_id, payload)

    @app.post("/api/drawings/{snapshot_id}/geometry/read")
    def read_geometry(snapshot_id: str, payload: GeometryRead):
        return GeometryService(workspace, snapshot_id).read(payload)

    @app.post("/api/drawings/{snapshot_id}/components/context")
    def drawing_context(snapshot_id: str, payload: ContextQuery):
        service = ComponentService(workspace, snapshot_id)
        return service.page(service.build("context", payload), payload.view, payload.offset, payload.limit)

    @app.post("/api/drawings/{snapshot_id}/components/dimensions")
    def dimensions(snapshot_id: str, payload: DimensionQuery):
        service = ComponentService(workspace, snapshot_id)
        return service.page(service.build("dimensions", payload), "dimensions", payload.offset, payload.limit)

    @app.post("/api/drawings/{snapshot_id}/components/relations")
    def building_relations(snapshot_id: str, payload: RelationQuery):
        service = ComponentService(workspace, snapshot_id)
        return service.page(service.build("relations", payload), payload.view, payload.offset, payload.limit)

    @app.get("/api/drawings/{snapshot_id}/components/{record_id}")
    def component_evidence(snapshot_id: str, record_id: str):
        return ComponentService(workspace, snapshot_id).get(record_id)

    @app.post("/api/drawings/{snapshot_id}/geometry/query")
    def query_geometry(snapshot_id: str, payload: SpatialQuery):
        return GeometryService(workspace, snapshot_id).query(payload)

    @app.post("/api/drawings/{snapshot_id}/geometry/measure")
    def measure_geometry(snapshot_id: str, payload: GeometryMeasure):
        return GeometryService(workspace, snapshot_id).measure(payload)

    @app.post("/api/drawings/{snapshot_id}/rooms/batch")
    def room_batch(snapshot_id: str, payload: RoomBatchQuery):
        return RoomBatchService(workspace, snapshot_id).run(payload)

    @app.get("/api/drawings/{snapshot_id}/room-batches/{batch_id}")
    def read_room_batch(snapshot_id: str, batch_id: str):
        return RoomBatchService(workspace, snapshot_id).get(batch_id)

    @app.post("/api/drawings/{snapshot_id}/room-batches/{batch_id}/confirm-unit")
    def confirm_room_units(snapshot_id: str, batch_id: str, payload: UnitConfirmation):
        return RoomBatchService(workspace, snapshot_id).confirm(batch_id, payload)

    @app.post("/api/drawings/{snapshot_id}/room-batches/{batch_id}/confirm-scope")
    def confirm_room_scope(snapshot_id: str, batch_id: str, payload: ScopeConfirmation):
        return RoomBatchService(workspace, snapshot_id).confirm_scope(batch_id, payload)

    @app.post("/api/drawings/{snapshot_id}/room-batches/{batch_id}/page")
    def room_batch_page(snapshot_id: str, batch_id: str, payload: BatchPage):
        return RoomBatchService(workspace, snapshot_id).page(batch_id, payload.offset)

    @app.post("/api/drawings/{snapshot_id}/rooms/measure")
    def measure_room(snapshot_id: str, payload: RoomMeasure):
        return GeometryService(workspace, snapshot_id).room(payload)

    @app.post("/api/drawings/{snapshot_id}/rooms/discover")
    def discover_rooms(snapshot_id: str, payload: RoomDiscover):
        service = GeometryService(workspace, snapshot_id)
        return service.discovery_summary(service.discover(payload), payload.offset, payload.limit)

    @app.post("/api/drawings/{snapshot_id}/rooms/contours")
    def room_contours(snapshot_id: str, payload: RoomContoursRead):
        return GeometryService(workspace, snapshot_id).room_contours(payload)

    @app.post("/api/drawings/{snapshot_id}/rooms/area")
    def room_area(snapshot_id: str, payload: RoomAreaMeasure):
        return GeometryService(workspace, snapshot_id).room_area(payload)

    @app.get("/api/drawings/{snapshot_id}/measurements")
    def measurements(snapshot_id: str):
        workspace.store.get("drawing", snapshot_id)
        return {"items": [GeometryService.summary(r) for r in workspace.store.list("measurement") if r["snapshot_id"] == snapshot_id]}

    @app.get('/api/drawings/{snapshot_id}/boundary-reviews/{review_id}')
    def room_boundary_evidence(snapshot_id: str, review_id: str):
        from cad_agent.geometry.room_boundary import RoomBoundaryService
        return RoomBoundaryService(workspace,snapshot_id).get(review_id)

    @app.get('/api/drawings/{snapshot_id}/images/{image_id}')
    def visual_image_evidence(snapshot_id: str, image_id: str):
        from cad_agent.geometry.vision import VisionService
        return VisionService(workspace, snapshot_id).get(image_id)

    @app.get('/api/drawings/{snapshot_id}/images/{image_id}/image.png')
    def visual_image_png(snapshot_id: str, image_id: str):
        from cad_agent.geometry.vision import VisionService
        return Response(VisionService(workspace, snapshot_id).png(image_id), media_type='image/png')

    @app.get("/api/drawings/{snapshot_id}/measurements/{measurement_id}")
    def measurement_evidence(snapshot_id: str, measurement_id: str):
        return GeometryService(workspace, snapshot_id).get(measurement_id)

    @app.get("/api/drawings/{snapshot_id}/measurements/{measurement_id}/preview.svg")
    def measurement_preview(snapshot_id: str, measurement_id: str):
        return Response(GeometryService(workspace, snapshot_id).svg(measurement_id), media_type="image/svg+xml")

    @app.get("/api/generic-fact-sets/{fact_set_id}")
    def generic_fact_set(fact_set_id: str):
        return workspace.store.get("generic_fact_set", fact_set_id)

    @app.get("/api/tools")
    def tools():
        return {
            "schema_version": "1.0",
            "agent_tools": tool_definitions(),
            "tools": [
                {"name": "get_drawing_context", "method": "POST", "path": "/api/drawings/{snapshot_id}/components/context"},
                {"name": "read_dimensions", "method": "POST", "path": "/api/drawings/{snapshot_id}/components/dimensions"},
                {"name": "query_building_relations", "method": "POST", "path": "/api/drawings/{snapshot_id}/components/relations"},
                {"name": "get_component_evidence", "method": "GET", "path": "/api/drawings/{snapshot_id}/components/{record_id}"},
                {"name": "prepare_drawing", "method": "POST", "path": "/api/drawings"},
                {
                    "name": "search_sources",
                    "method": "GET",
                    "path": "/api/drawings/{snapshot_id}/sources",
                },
                {"name": "extract_facts", "method": "POST", "path": "/api/extractions"},
                {
                    "name": "query_facts",
                    "method": "GET",
                    "path": "/api/fact-sets/{fact_set_id}",
                },
                {
                    "name": "get_evidence",
                    "method": "GET",
                    "path": "/api/fact-sets/{fact_set_id}/evidence/{evidence_id}",
                },
                {"name": "get_job", "method": "GET", "path": "/api/jobs/{job_id}"},
            ],
        }

    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    return app
