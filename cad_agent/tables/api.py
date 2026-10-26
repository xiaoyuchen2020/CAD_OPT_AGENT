"""Read-only table inspection and source/grid overlays for the desktop viewer."""
from html import escape
from fastapi import APIRouter
from fastapi.responses import Response
from .service import TableService
from .contracts import SearchTables, InspectTable, ReadTableContent
from .reading import read


def overlay(service,table_id):
    from cad_agent.cad.render import render_svg
    d=service.archived_view(table_id)
    # Source rendering and overlay share WCS. The source renderer uses a
    # transformed viewport; an outer viewport overlay avoids font assumptions.
    a,b,c,z=d['bbox'];pad=max(c-a,z-b)*.025
    bounds=[a-pad,b-pad,c+pad,z+pad]
    svg=render_svg(service.w.rendering(service.snapshot),d['layout_id'],bounds,width=1400,height=1000)
    # The renderer already exposes the exact source viewport through its SVG
    # coordinate transform. Inject paths in that same CAD-coordinate group.
    paths=[]
    for cell in d['cells']:
        points=' '.join(f'{x},{y}' for x,y in cell['boundary'])
        paths.append(f'<polygon points="{points}" fill="none" stroke="#e06a18" stroke-width="{max(c-a,z-b)*.001}" opacity="0.8"><title>{escape(cell["text"])}</title></polygon>')
    return svg.replace('</svg>','<g transform="scale(1 -1)">'+''.join(paths)+'</g></svg>')


def router(workspace):
    routes=APIRouter(prefix='/api/drawings/{snapshot_id}/table-documents',tags=['table-documents'])
    @routes.post('/search')
    def search(snapshot_id:str,payload:SearchTables):
        if payload.snapshot_id and payload.snapshot_id!=snapshot_id:raise ValueError('TABLE_SNAPSHOT_MISMATCH')
        return TableService(workspace,snapshot_id).search(payload)
    @routes.get('/{table_id}')
    def inspect(snapshot_id:str,table_id:str):
        return TableService(workspace,snapshot_id).inspect(InspectTable(table_id=table_id,limit=100))
    @routes.post('/read')
    def content(snapshot_id:str,payload:ReadTableContent):
        if payload.snapshot_id and payload.snapshot_id!=snapshot_id:raise ValueError('TABLE_SNAPSHOT_MISMATCH')
        return read(TableService(workspace,snapshot_id),payload,'table-browser:'+snapshot_id)
    @routes.get('/{table_id}/cells/{cell_id}')
    def cell(snapshot_id:str,table_id:str,cell_id:str):
        service=TableService(workspace,snapshot_id);d=service.archived_view(table_id)
        c=next((c for c in d['cells'] if c['cell_id']==cell_id),None)
        if c is None:raise KeyError(cell_id)
        ev=[e for f in c['fragments'] for e in f['evidence']]
        return dict(snapshot_id=snapshot_id,table_id=table_id,cell_id=cell_id,layout_id=d['layout_id'],
                    region_id=ev[0]['region_id'] if ev else cell_id,quote=c['text'],bbox=c['bbox'],
                    entity_ids=[f['entity_id'] for f in c['fragments']],source_fragments=c['fragments'],
                    highlight_regions=[dict(boundary=c['boundary'])],precision='table_cell',
                    preview_url=f'/api/drawings/{snapshot_id}/table-documents/{table_id}/overlay.svg')
    @routes.get('/{table_id}/overlay.svg')
    def image(snapshot_id:str,table_id:str):
        return Response(overlay(TableService(workspace,snapshot_id),table_id),media_type='image/svg+xml')
    return routes
