from fastapi import APIRouter
from fastapi.responses import Response, HTMLResponse
from .contracts import Start, Review, Promote, SPECS
from .service import AdaptationService
from .review import page, script, source_svg
from .shadow import ShadowService, Policy
from cad_agent.contracts import Record
from pydantic import model_validator
from .capabilities import capabilities


class Trial(Record):
    table_id: str | None = None
    target_id: str | None = None

    @model_validator(mode='after')
    def selector(self):
        if bool(self.table_id)==bool(self.target_id):raise ValueError('ONE_TARGET_SELECTOR_REQUIRED')
        return self


def router(workspace):
    routes = APIRouter(prefix='/api/drawings/{snapshot_id}/adaptations', tags=['controlled-adaptation'])

    @routes.get('/capabilities')
    def supported(snapshot_id: str):
        workspace.store.get('drawing',snapshot_id)
        return capabilities()

    @routes.post('/policies')
    def create_policy(snapshot_id: str, payload: Policy):
        workspace.store.get('drawing',snapshot_id)
        return ShadowService(workspace).create(payload)

    @routes.get('/policies/{policy_id}')
    def get_policy(snapshot_id: str, policy_id: str):
        return ShadowService(workspace).get(policy_id)

    @routes.post('/policies/{policy_id}/disable')
    def disable_policy(snapshot_id: str, policy_id: str):
        return ShadowService(workspace).disable(policy_id)

    @routes.post('/policies/{policy_id}/trials', status_code=202)
    def trial(snapshot_id: str, policy_id: str, payload: Trial):
        return ShadowService(workspace).trial(policy_id,snapshot_id,payload.table_id,target_id=payload.target_id)

    @routes.get('/methods')
    def methods(snapshot_id: str):
        workspace.store.get('drawing',snapshot_id)
        return dict(specs=SPECS, versions=workspace.store.list('adaptation_version'))

    @routes.post('/runs', status_code=202)
    def start(snapshot_id: str, payload: Start):
        return AdaptationService(workspace,snapshot_id).start(payload)

    @routes.get('/runs/{run_id}')
    def status(snapshot_id: str, run_id: str):
        return AdaptationService(workspace,snapshot_id).get(run_id)

    @routes.post('/runs/{run_id}/cancel')
    def cancel(snapshot_id: str, run_id: str):
        return AdaptationService(workspace,snapshot_id).cancel(run_id)

    @routes.get('/runs/{run_id}/result')
    def result(snapshot_id: str, run_id: str):
        result = AdaptationService(workspace,snapshot_id).result(run_id)
        return dict(run=result['run'], validation=result['validation'])

    @routes.get('/runs/{run_id}/review',response_class=HTMLResponse)
    def review_page(snapshot_id: str,run_id: str):
        return page(AdaptationService(workspace,snapshot_id),run_id)

    @routes.get('/runs/{run_id}/review.js')
    def review_script(snapshot_id: str,run_id: str):
        return Response(script(AdaptationService(workspace,snapshot_id),run_id),media_type='text/javascript')

    @routes.get('/runs/{run_id}/source.svg')
    def source(snapshot_id: str,run_id: str):
        return Response(source_svg(AdaptationService(workspace,snapshot_id),run_id,local_origin=True),media_type='image/svg+xml')

    @routes.get('/runs/{run_id}/overlay.svg')
    def overlay(snapshot_id: str,run_id: str):
        return Response(source_svg(AdaptationService(workspace,snapshot_id),run_id,True,local_origin=True),media_type='image/svg+xml')

    @routes.post('/runs/{run_id}/reviews')
    def review(snapshot_id: str,run_id: str,payload: Review):
        return AdaptationService(workspace,snapshot_id).review(run_id,payload)

    @routes.post('/versions')
    def promote(snapshot_id: str,payload: Promote):
        return AdaptationService(workspace,snapshot_id).promote(payload)

    @routes.post('/versions/{version_id}/disable')
    def disable(snapshot_id: str,version_id: str):
        return AdaptationService(workspace,snapshot_id).disable(version_id)

    return routes
