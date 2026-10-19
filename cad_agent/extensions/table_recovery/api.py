from fastapi import APIRouter
from fastapi.responses import Response

from .contracts import Diagnose, ReadInput, RunFixed, ResultPage
from .service import RecoveryService


def router(workspace):
    routes=APIRouter(prefix='/api/drawings/{snapshot_id}/table-recovery',tags=['fixed-table-recovery'])

    @routes.post('/diagnostics')
    def diagnose(snapshot_id: str, payload: Diagnose):
        return RecoveryService(workspace,snapshot_id).diagnose(payload.table_id)

    @routes.post('/inputs/read')
    def read_input(snapshot_id: str,payload: ReadInput):
        return RecoveryService(workspace,snapshot_id).read_input(payload)

    @routes.post('/runs')
    def run(snapshot_id: str,payload: RunFixed):
        # Phase one is synchronous and bounded; no pretend 202/job lifecycle.
        return RecoveryService(workspace,snapshot_id).run(payload)

    @routes.get('/runs/{run_id}')
    def get_run(snapshot_id: str,run_id: str):
        return RecoveryService(workspace,snapshot_id).get_run(run_id)

    @routes.post('/results/read')
    def result(snapshot_id: str,payload: ResultPage):
        return RecoveryService(workspace,snapshot_id).result(payload)

    @routes.get('/results/{result_id}/overlay.svg')
    def overlay(snapshot_id: str,result_id: str):
        return Response(RecoveryService(workspace,snapshot_id).overlay(result_id),media_type='image/svg+xml')

    return routes
