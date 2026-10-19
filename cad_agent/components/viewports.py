"""Native paper viewport references; geometry remains in model WCS.

No screenshot coordinates or paper scale are used as physical measurement units.
Unsupported camera/clipping states remain explicit instead of guessed mapping.
"""
import math
from copy import deepcopy
from shapely.geometry import box
from cad_agent.contracts import stable_id
from cad_agent.geometry.source import source_viewports


def attach_model_scopes(workspace,sid,layout_id,subframes):
    resolved=source_viewports(workspace,sid,layout_id,subframes)
    for frame,result in zip(subframes,resolved):
        frame.clear();frame.update(result)


def attach_model_scopes_from_source(source,sid,layout_id,subframes):
    """Worker-local viewport geometry; output contains no native DXF objects."""
    subframes=deepcopy(subframes)
    doc=source['doc']
    layout=next((l for l in doc.layouts if stable_id('layout',sid,l.name)==layout_id),None)
    if layout is None or layout.name=='Model':return subframes
    model_id=stable_id('layout',sid,'Model');views=[]
    for v in layout.query('VIEWPORT'):
        if v.dxf.id<=1 or v.dxf.status<=0:continue
        x,y=list(v.dxf.center)[:2];w,h=v.dxf.width,v.dxf.height
        if w<=0 or h<=0:continue
        bounds=[x-w/2,y-h/2,x+w/2,y+h/2]
        direction=v.dxf.view_direction_vector
        supported=abs(direction.x)<1e-9 and abs(direction.y)<1e-9 and direction.z>0 and v.dxf.get('clipping_boundary_handle','0') in ('','0') and not (v.dxf.flags&1)
        row=dict(viewport_handle=v.dxf.handle,paper_layout_id=layout_id,paper_bbox=bounds,
                 model_layout_id=model_id,status='supported_top_view' if supported else 'unsupported_camera_or_clip',
                 view_twist_degrees=v.dxf.view_twist_angle,model_bbox=None)
        if supported:
            try:
                b=list(v.get_modelspace_limits())
                if not all(math.isfinite(n) for n in b) or b[0]>=b[2] or b[1]>=b[3]:continue
                row['model_bbox']=b
                # Preserve the affine transform for independent coordinate audit.
                m=v.get_transformation_matrix()
                row['model_to_paper_matrix']=list(m)
            except (ValueError,ZeroDivisionError):row['status']='invalid_viewport_parameters'
        views.append(row)
    for frame in subframes:
        title=box(*frame['title_bbox'])
        overlaps=[v for v in views if box(*v['paper_bbox']).intersects(title)]
        frame['model_viewport_candidates']=overlaps[:4]
        if len(overlaps)==1 and overlaps[0]['model_bbox']:
            v=overlaps[0]
            frame['model_geometry_scope']=dict(layout_id=model_id,bbox=v['model_bbox'],viewport_handle=v['viewport_handle'],
                basis='native_paper_viewport',scope_status='viewport_extent_not_confirmed_subframe',
                note='标题在纸空间视口范围内；实际建筑线形在此模型空间范围。render_region/discover_rooms使用本layout_id及bbox，勿沿用纸空间标题坐标。视口范围与标题语义归属仍需看图核查。')

    return subframes
