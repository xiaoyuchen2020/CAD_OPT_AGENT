from types import SimpleNamespace
import ezdxf
from cad_agent.contracts import stable_id
from cad_agent.components.viewports import attach_model_scopes, attach_model_scopes_from_source


def test_paper_title_exposes_native_model_scope_without_changing_coordinates(monkeypatch):
    doc=ezdxf.new();paper=doc.layouts.get('Layout1')
    v=paper.add_viewport(center=(100,200),size=(200,100),view_center_point=(1000,2000),view_height=100)
    v.dxf.view_target_point=(300,400,0);v.dxf.status=2
    monkeypatch.setattr('cad_agent.components.viewports.source_viewports',
        lambda w,sid,lid,frames:attach_model_scopes_from_source({'doc':doc},sid,lid,frames))
    title={'title_bbox':[90,155,110,160],'bbox':None}
    attach_model_scopes(SimpleNamespace(),'s',stable_id('layout','s','Layout1'),[title])
    scope=title['model_geometry_scope']
    assert scope['layout_id']==stable_id('layout','s','Model')
    assert scope['bbox']==[1200,2350,1400,2450]
    assert title['title_bbox']==[90,155,110,160] and title['bbox'] is None
    # Independent corner projection using the actual native camera transform.
    assert tuple(v.get_transformation_matrix().transform((1200,2350,0)))[:2]==(0,150)
    v.dxf.view_direction_vector=(1,0,1)
    unknown={'title_bbox':[90,155,110,160]}
    attach_model_scopes(SimpleNamespace(),'s',stable_id('layout','s','Layout1'),[unknown])
    assert 'model_geometry_scope' not in unknown
    assert unknown['model_viewport_candidates'][0]['model_bbox'] is None
