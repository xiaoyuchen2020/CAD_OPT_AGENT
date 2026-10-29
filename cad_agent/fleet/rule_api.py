"""Authenticated rule-library and E0 exploration entry points.

Exploration only collects facts, methods and gaps. It never marks a rule's
optimization decision complete. The existing Fleet owns worker execution.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .snapshots import model_snapshot


TOOL_TREES = ('analysis', 'extensions', 'runtimes', 'harness', 'geometry', 'adapters')
TOOL_FILES = ('cad_agent/adapters/model.py', 'cad_agent/contracts.py',
              'cad_agent/config.py', 'cad_agent/service.py',
              'cad_agent/exploration/question.py')


def tool_snapshot():
    root = Path(__file__).resolve().parents[2]
    files = set(TOOL_FILES)
    for directory in TOOL_TREES:
        files.update(str(path.relative_to(root)) for path in (root / 'cad_agent' / directory).rglob('*.py'))
    return {'version': 'e0@1', 'host_files': {name: sha256((root / name).read_bytes()).hexdigest() for name in sorted(files)}}


def check_tool_snapshot(snapshot):
    if snapshot != tool_snapshot():
        from .snapshots import SnapshotMismatch
        raise SnapshotMismatch('EXPLORATION_TOOL_VERSION_MISMATCH')


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EffectiveSetRequest(Input):
    project_id: str = Field(min_length=1, max_length=200)
    selections: list[dict] = Field(min_length=1, max_length=100)
    overrides: list[dict] = Field(default_factory=list, max_length=1000)


class ExplorationRequest(Input):
    project_id: str = Field(min_length=1, max_length=200)
    effective_set_id: str
    upload_ids: list[str] = Field(default_factory=list, max_length=9)
    original_user_input: str = Field(default='', max_length=1500)
    dynamic_methods: Literal['off', 'task_verified'] = 'off'
    model_profile: Literal['api_sol', 'api_astra'] = 'api_sol'
    budget: dict = Field(default_factory=dict)
    label: str = Field(default='', max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)


class StartCaseRequest(Input):
    preferred_node: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,64}$')


def build_router(app):
    from cad_agent.rules import RuleLibraryStore, RuleLibraryError
    from cad_agent.exploration import ExplorationStore, ExplorationError, ExplorationConflict, build_job_payload

    fleet = app.state.fleet_store
    libraries = RuleLibraryStore(fleet.root / 'rules')
    exploration = ExplorationStore(fleet.root / 'exploration')
    app.state.rule_libraries = libraries
    app.state.exploration = exploration
    router = APIRouter(prefix='/api/fleet')

    @app.exception_handler(RuleLibraryError)
    async def rule_error(request, exc):
        code = getattr(exc, 'code', 'rule_library_error')
        status = 404 if code in {'library_not_found', 'rule_not_found', 'image_not_found', 'effective_set_not_found', 'project_mismatch'} else 400
        return JSONResponse({'detail': str(exc), 'code': code, 'diagnostics': getattr(exc, 'diagnostics', [])}, status_code=status)

    @app.exception_handler(ExplorationError)
    async def exploration_error(request, exc):
        return JSONResponse({'detail': str(exc), 'code': getattr(exc, 'code', 'exploration_error')},
                            status_code=409 if isinstance(exc, ExplorationConflict) else 400)

    def sync_run(run_id):
        run = exploration.get_run(run_id)
        for case in run['cases']:
            job_id = case.get('fleet_job_id') or case.get('job_id')
            if not job_id and case.get('dispatch_key'):
                pending = fleet.job_for_key(case['dispatch_key'])
                if pending:
                    job_id = pending['id']
                    try:
                        exploration.reconcile_job(run_id, case['case_id'], pending)
                    except ExplorationConflict:
                        fleet.cancel(job_id)
                        continue
            if not job_id:
                continue
            job = fleet.job(job_id)
            if run.get('status') == 'cancelled' or case.get('status') == 'cancelled':
                if job['status'] in ('queued', 'running'):
                    job = fleet.cancel(job_id)
            # A retry may have completed while no browser was polling. Import
            # earlier attempt evidence/usage without replaying old job states.
            for attempt in fleet.attempts(job_id):
                if attempt['id'] == job.get('attempt_id'):
                    continue
                prior_archive = (json.loads(fleet.archive(job_id, attempt['id']).read_text())
                                 if attempt.get('archive_url') else None)
                exploration.record_attempt_archive(job_id, attempt, prior_archive)
            archive = None
            archive_ref = None
            if job.get('archive_url'):
                # Pin the exact attempt captured in job(), not whichever one
                # became current between the state read and archive read.
                archive = json.loads(fleet.archive(job_id, job['attempt_id']).read_text())
                archive_ref = f"/api/fleet/jobs/{job_id}/attempts/{job['attempt_id']}/archive"
            exploration.record_job_result(job, archive=archive, archive_ref=archive_ref)
        if hasattr(app.state,'operations'):
            app.state.operations.exploration_detail(run_id)
        return exploration.get_run(run_id)

    app.state.sync_exploration_run = sync_run

    @router.get('/exploration-capabilities')
    def capabilities():
        return {
            'stage': 'E0', 'rule_evaluation': False, 'automatic_tool_promotion': False,
            'rule_import_formats': ['xlsx'], 'dynamic_methods_default': 'off',
            'code_execution': 'explicit_authorization_and_configured_node_sandbox',
            'max_drawings_per_case': 9, 'max_question_characters': 3000,
            'budget_semantics': 'dispatch budget overlay gates later cases; runtime budget overlay applies at next safe boundary, not an in-flight request cap',
            'live_controls': {'enabled':True,'actions':['pause','resume','guidance','configure'],
                'configuration':['dispatch_budget','budget','sandbox','validator'],
                'boundary':'between_case_dispatch_and_at_agent_model_or_tool_boundaries',
                'scope':'audited_overlays_do_not_rewrite_frozen_sources_or_code_authorization',
                'pause':'cooperative; current model/tool call finishes; occupied worker/model slots retained'},
            'external_supervision':{'enabled':True,'default':'off','modes':['advisory','guidance'],
                'independent_budget':True,'permissions':'guidance_only; no authority to change permissions or source scope'},
            'tool_snapshot': tool_snapshot(),
        }

    @router.get('/rule-libraries')
    def list_libraries(project_id: str | None = None):
        return {'items': libraries.list_libraries(project_id)}

    @router.post('/rule-libraries/import', status_code=201)
    async def import_library(project_id: str = Form(...), file: UploadFile = File(...)):
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > libraries.max_source_bytes:
                raise HTTPException(413, '规则库超过导入大小上限')
        result = await run_in_threadpool(libraries.import_xlsx, file.filename or 'rules.xlsx', bytes(content), project_id)
        return libraries._summary(result)

    @router.get('/rule-libraries/{library_id}')
    def library(library_id: str, project_id: str | None = None):
        document = libraries.get_library(library_id, project_id)
        return {k: v for k, v in document.items() if k != 'workbook'}

    @router.get('/rule-libraries/{library_id}/source')
    def source(library_id: str, project_id: str | None = None):
        return Response(libraries.get_source(library_id, project_id),
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': 'attachment; filename="rules.xlsx"'})

    @router.get('/rule-libraries/{library_id}/rules/{rule_id}')
    def rule(library_id: str, rule_id: str, project_id: str | None = None):
        return libraries.get_rule(library_id, rule_id, project_id)

    @router.get('/rule-libraries/{library_id}/images/{image_id}')
    def image(library_id: str, image_id: str, project_id: str | None = None):
        data, mime = libraries.get_image(library_id, image_id, project_id)
        return Response(data, media_type=mime)

    @router.post('/effective-rule-sets', status_code=201)
    def effective(payload: EffectiveSetRequest):
        return libraries.create_effective_set(payload.project_id, payload.selections, payload.overrides)

    @router.get('/effective-rule-sets/{set_id}')
    def get_effective(set_id: str, project_id: str):
        return libraries.get_effective_set(set_id, project_id)

    @router.post('/exploration-runs', status_code=201)
    def create_run(payload: ExplorationRequest):
        if len(payload.upload_ids) != len(set(payload.upload_ids)):
            raise ValueError('图纸不能重复选择')
        effective = libraries.get_effective_set(payload.effective_set_id, payload.project_id)
        drawings = [fleet.upload(ident) for ident in payload.upload_ids]
        return exploration.create_run(effective_set=effective, drawings=drawings,
            original_user_input=payload.original_user_input, dynamic_methods=payload.dynamic_methods,
            tool_snapshot=tool_snapshot(), model_snapshot=model_snapshot(payload.model_profile),
            budget=payload.budget, label=payload.label, idempotency_key=payload.idempotency_key)

    @router.get('/exploration-runs')
    def runs(project_id: str | None = None):
        for item in exploration.list_runs(project_id):
            sync_run(item['run_id'])
        return {'items': exploration.list_runs(project_id)}

    @router.get('/exploration-runs/{run_id}')
    def run(run_id: str):
        return sync_run(run_id)

    @router.get('/exploration-runs/{run_id}/export')
    def export(run_id: str):
        value = sync_run(run_id)
        from .operations_api import control_audit
        value['operations_audit'] = control_audit(app, 'exploration', run_id)
        return Response(json.dumps(value, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="{run_id}.json"'})

    @router.post('/exploration-runs/{run_id}/cases/{case_id}/start')
    def start(run_id: str, case_id: str, payload: StartCaseRequest):
        if payload.preferred_node and payload.preferred_node not in os.environ.get('CAD_FLEET_NODE_IDS', 'a,b').split(','):
            raise ValueError('未知执行节点')
        run = sync_run(run_id)
        case = exploration.get_case(run_id, case_id)
        if case.get('fleet_job_id') or case.get('job_id'):
            return case
        # Validate the whole prompt before reserving capacity; never truncate rules.
        try:
            job_payload = build_job_payload(run, case, preferred_node=payload.preferred_node)
        except ExplorationError as exc:
            if exc.code in {'EXPLORATION_QUESTION_TOO_LONG', 'RULE_TEXT_REQUIRED'}:
                exploration.fail_dispatch(run_id, case_id, {'code': exc.code, 'message': str(exc)})
            raise
        case = exploration.prepare_case(run_id, case_id, dispatch_payload=job_payload)
        job = None
        try:
            job = fleet.create_job(job_payload, idempotency_key=case['dispatch_key'])
            return exploration.bind_job(run_id, case_id, job['id'])
        except Exception as exc:
            # Enqueue may have committed before its response was lost, or a
            # duplicate request may already have bound this same durable job.
            existing = fleet.job_for_key(case['dispatch_key'])
            if existing:
                if all(existing.get(key) == value for key, value in job_payload.items()):
                    recovered = exploration.reconcile_job(run_id, case_id, existing)
                    if recovered['status'] in ('cancelled', 'failed') and existing['status'] in ('queued', 'running'):
                        fleet.cancel(existing['id'])
                    return recovered
                # A conflicting caller must not fail/cancel another dispatch.
                raise
            exploration.fail_dispatch(run_id, case_id,
                {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)},
                retryable=True, preserve_preparation=True)
            raise

    @router.post('/exploration-runs/{run_id}/cancel')
    def cancel(run_id: str):
        result = exploration.cancel_run(run_id)
        for job_id in result.get('job_ids', result.get('cancel_job_ids', [])):
            fleet.cancel(job_id)
        return sync_run(run_id)

    return router
