"""Authenticated control plane. Browser callers cannot choose actor identity."""
import json
from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .operations import OperationsStore


class ControlRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    command_id:str=Field(min_length=1,max_length=160,pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_revision:int=Field(ge=0,strict=True)
    action:Literal['pause','resume','guidance','configure']
    payload:dict=Field(default_factory=dict)


class WorkerOperations(BaseModel):
    model_config=ConfigDict(extra='forbid')
    node_id:str
    job_id:str
    attempt_id:str
    telemetry:dict|None=None
    receipts:list[dict]=Field(default_factory=list,max_length=100)


def control_audit(app, kind, ident):
    """Export operator and supervisor history outside immutable input scope."""
    operations = app.state.operations
    def job_control(job_id):
        value = operations.control(job_id)
        with operations.fleet.connection() as db:
            value['commands'] = [json.loads(row[0]) for row in db.execute(
                'SELECT data FROM operation_commands WHERE job_id=? ORDER BY revision', (job_id,))]
        return value
    snapshot = operations.snapshot(kind, ident)
    result = {'schema_version': 'operations-audit@1', 'kind': kind, 'id': ident,
              'control': job_control(ident) if kind == 'job' else snapshot['control'],
              'supervision': app.state.supervisor.store.public(kind, ident)}
    if kind == 'exploration':
        run = app.state.exploration.get_run(ident)
        result['control']['commands'] = run.get('control', {}).get('commands', [])
        children = [c['job_id'] for c in run['cases'] if c.get('job_id')]
        result['job_controls'] = {job_id: job_control(job_id) for job_id in children}
        result['job_supervision'] = {job_id: app.state.supervisor.store.public('job', job_id) for job_id in children}
    return result


def register_operations_api(app):
    operations=OperationsStore(app.state.fleet_store,app.state.exploration)
    app.state.operations=operations
    app.state.fleet_store.exploration=app.state.exploration
    router=APIRouter(prefix='/api/fleet')

    @router.get('/operations')
    def overview():
        operations.reconcile_all()
        return operations.overview()

    @router.get('/jobs/{job_id}/operations')
    def job_detail(job_id:str):return operations.job_detail(job_id)

    @router.get('/jobs/{job_id}/operations/export')
    def job_export(job_id: str):
        value = {'operations': operations.job_detail(job_id), 'audit': control_audit(app, 'job', job_id)}
        return Response(json.dumps(value, ensure_ascii=False, indent=2), media_type='application/json',
            headers={'Content-Disposition': f'attachment; filename="{job_id}.operations.json"'})

    @router.post('/jobs/{job_id}/control')
    def job_control(job_id:str,payload:ControlRequest):
        return operations.submit('job',job_id,**payload.model_dump(),source='human')

    @router.get('/exploration-runs/{run_id}/operations')
    def exploration_detail(run_id:str):
        app.state.sync_exploration_run(run_id)
        return operations.exploration_detail(run_id)

    @router.post('/exploration-runs/{run_id}/control')
    def exploration_control(run_id:str,payload:ControlRequest):
        return operations.submit('exploration',run_id,**payload.model_dump(),source='human')

    @router.post('/worker/operations/poll')
    def poll(payload:WorkerOperations):
        operations.reconcile_all()
        return operations.pending(payload.job_id,payload.node_id,payload.attempt_id)

    @router.post('/worker/operations/report')
    def report(payload:WorkerOperations):
        return operations.report(**payload.model_dump())

    app.include_router(router)
    return operations
