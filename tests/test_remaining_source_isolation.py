"""Regression boundaries for conditional units and native paper viewports."""
import os
from cad_agent.analysis.unit_policy import resolve_units
from cad_agent.components.dimensions import read_dimensions_from_source
from cad_agent.components.viewports import attach_model_scopes
from cad_agent.components.contracts import ContextQuery
from tests.test_components import ingest


def forbid_parent_source(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('parent native source read')
    monkeypatch.setattr('ezdxf.readfile',forbidden)
    monkeypatch.setattr('cad_agent.geometry.source.native_source',forbidden)
    monkeypatch.setattr('cad_agent.geometry.source._native_source',forbidden)


def batch(lid):
    return dict(batch_id='rooms_test',scope=dict(layout_id=lid,frame_id='',bbox=[0,0,10,10]),items=[],unit='unknown')


def test_source_dimensions_preserves_global_diagnostics_for_unit_audit():
    issues=[dict(code='global'),dict(code='local',layout_id='m'),dict(code='other',layout_id='p')]
    result=read_dimensions_from_source(dict(entities={},diagnostics=issues),'s','m',None,'unknown')
    assert result['diagnostics']==[issues[1]]
    assert result['source_diagnostics']==issues[:2]


def test_conditional_units_use_one_worker_read_and_no_parent_source(workspace,monkeypatch):
    _,drawing,lid=ingest(workspace,lambda doc:doc.modelspace().add_line((0,0),(1,0)),units=0)
    forbid_parent_source(monkeypatch)
    compute=workspace.geometry_executor.compute;calls=[]
    def observed(operation,payload,**kwargs):
        result=compute(operation,payload,**kwargs);calls.append(operation);return result
    monkeypatch.setattr(workspace.geometry_executor,'compute',observed)
    result=resolve_units(workspace,drawing['id'],batch(lid),dict(condition='no_dimensions',unit='mm'))
    assert result['unit_resolution']['status']=='resolved'
    assert result['unit_resolution']['dimension_check']['complete'] is True
    assert calls==['source_dimensions']
    assert workspace.geometry_executor.last_worker_pid!=os.getpid()


def test_viewport_binding_is_in_worker_and_keeps_original_frame_identity(workspace,monkeypatch):
    def draw(doc):
        doc.layouts.get('Layout1').add_line((0,0),(1,0))
        viewport=doc.layouts.get('Layout1').add_viewport(center=(100,200),size=(200,100),view_center_point=(1000,2000),view_height=100)
        viewport.dxf.view_target_point=(300,400,0);viewport.dxf.status=2
    _,drawing,_=ingest(workspace,draw)
    lid=next(l['layout_id'] for l in drawing['layouts'] if l['name']=='Layout1')
    forbid_parent_source(monkeypatch)
    title=dict(title_bbox=[90,155,110,160],bbox=None)
    frames=[title]
    attach_model_scopes(workspace,drawing['id'],lid,frames)
    assert frames[0] is title
    assert title['model_geometry_scope']['bbox']==[1200,2350,1400,2450]
    assert workspace.geometry_executor.last_worker_pid!=os.getpid()


def test_model_context_and_declared_mm_do_not_enter_new_worker_path(workspace,monkeypatch):
    service,drawing,lid=ingest(workspace,lambda doc:doc.modelspace().add_text('房间',dxfattribs={'height':.1}),units=4)
    forbid_parent_source(monkeypatch)
    def forbidden(*args,**kwargs):raise AssertionError('unexpected worker on Model/mm path')
    monkeypatch.setattr(workspace.geometry_executor,'compute',forbidden)
    service.build('context',ContextQuery(layout_id=lid))
    result=resolve_units(workspace,drawing['id'],batch(lid),None)
    assert result['unit_resolution']['basis']=='dxf_header'
    assert result['unit']=='mm'
