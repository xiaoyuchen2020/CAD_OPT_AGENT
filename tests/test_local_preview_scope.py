import json
from dataclasses import replace
import pytest
from cad_agent.analysis.scope_workflow import selected_for_call
from cad_agent.geometry.vision import RenderRegion,VisionService
from test_round3_spatial_view_scope import source_view
from test_remediation_scope import session,invoke


def test_ambiguous_view_allows_native_crop_but_not_measurement(workspace):
    workspace.settings=replace(workspace.settings,provider='responses_subscription',model='gpt-6.1-sol')
    drawing=source_view(workspace,title='一层平面图',extra_title='屋顶层平面图')
    s=session(workspace,drawing)
    unresolved=invoke(s,'resolve_drawing_scope',dict(floor_title='一层平面图',task_ids=['q1']))
    assert not unresolved['selected_scope']
    before=dict(s.task.data['unresolved_scope_by_task'])
    assert selected_for_call(s,'render_region') is None
    with pytest.raises(ValueError,match='SCOPE_UNRESOLVED'):
        selected_for_call(s,'measure_geometry')
    # Real render in the test workspace; no external model or image transport.
    image=invoke(s,'render_region',dict(layout_id=drawing['layouts'][0]['layout_id'],bbox=[0,0,10000,10000],task_ids=['q1']))
    assert 'error' not in image,image
    assert image['view_scope']=='local_source_preview' and image['engineering_scope_confirmed'] is False
    assert VisionService(workspace,drawing['id']).png(image['image_id'])
    assert s.task.data['unresolved_scope_by_task']==before
    assert not s.task.data.get('active_scope_by_task')
    assert all(r['operation'] not in ('area','geometry') or not r['fulfilled'] for r in image.get('operation_receipts',[]))


def test_preview_still_rejects_foreign_layout_empty_crop_and_invalid_coordinates(workspace):
    drawing=source_view(workspace);v=VisionService(workspace,drawing['id'])
    with pytest.raises(ValueError,match='布局'):
        v.render(RenderRegion(layout_id='foreign',bbox=[0,0,100,100]))
    with pytest.raises(ValueError,match='为空'):
        v.render(RenderRegion(layout_id=drawing['layouts'][0]['layout_id'],bbox=[1e8,1e8,1e8+100,1e8+100]))
    with pytest.raises(ValueError):RenderRegion(layout_id='l',bbox=[0,0,float('inf'),10])
