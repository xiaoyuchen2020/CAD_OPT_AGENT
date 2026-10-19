"""Source-bound contact sheets for bounded geometric decisions, not pixel measurements."""
import hashlib
import math
from xml.sax.saxutils import escape
from cad_agent.contracts import stable_id
from cad_agent.cad.render import render_svg
from cad_agent.geometry.vision import VisionService, MAX_IMAGE_BYTES


def sheets(workspace, sid, record):
    import resvg_py
    render=workspace.rendering(sid);vision=VisionService(workspace,sid);images=[]
    for start in range(0,len(record['items']),6):
        rows=record['items'][start:start+6];width=min(3,len(rows))*640;height=math.ceil(len(rows)/3)*680
        parts=[];cells=[]
        for n,row in enumerate(rows):
            x=n%3*640;y=n//3*680+40
            a,b,c,d=row['crop_bbox'];side=max(c-a,d-b);cx=(a+c)/2;cy=(b+d)/2
            bounds=[cx-side/2,cy-side/2,cx+side/2,cy+side/2];a,b,c,d=bounds;scale=640/side
            items=[i for i in render['items'] if i['layout_id']==record['layout_id'] and i['bbox'][0]<=c and i['bbox'][2]>=a and i['bbox'][1]<=d and i['bbox'][3]>=b]
            available=bool(items) and len(items)<=12000
            row['render_status']='available' if available else 'unavailable'
            svg=render_svg(render|{'items':items if available else []},record['layout_id'],bounds,width=640,height=640,raster=True)
            overlay=[]
            for label,paths in row.get('overlays',[]):
                for path in paths:
                    points='M '+' L '.join(f'{px},{-py}' for px,py in path)
                    overlay.append(f'<path d="{points}" stroke="#126ac2" stroke-width="{2/scale}" fill="none"/>')
                if paths:
                    px,py=paths[0][0]
                    overlay.append(f'<text x="{px}" y="{-py}" font-size="{17/scale}" fill="#126ac2" stroke="white" stroke-width="{3/scale}" paint-order="stroke">{escape(label)}</text>')
            svg=svg.replace('</svg>',''.join(overlay)+'</svg>').replace('<svg ',f'<svg x="{x}" y="{y}" ',1)
            parts.append(f'<text x="{x+8}" y="{y-12}" font-size="20">{escape(row["display_label"])} (blue = evidence overlay)</text>'+svg)
            if not available:parts.append(f'<text x="{x+8}" y="{y+40}">Image unavailable: uncertain only</text>')
            cells.append(dict(item_id=row['item_id'],bbox=bounds,pixel_rect=[x,y,x+640,y+640],render_status=row['render_status'],
                              wcs_to_pixel=[scale,0,0,-scale,x-a*scale,y+d*scale]))
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="100%" height="100%" fill="white"/>'+''.join(parts)+'</svg>'
        raw=resvg_py.svg_to_bytes(svg_string=svg)
        if len(raw)>MAX_IMAGE_BYTES:raise ValueError('局部审查图过大，请减少每批对象。')
        sha=hashlib.sha256(raw).hexdigest();iid=stable_id('image',record['review_id'],sha)
        path=vision.path(iid);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        url=f'/api/drawings/{sid}/images/{iid}'
        image=dict(image_id=iid,snapshot_id=sid,source_sha256=record['source_sha256'],image_sha256=sha,
            width=width,height=height,byte_count=len(raw),mime_type='image/png',layout_id=record['layout_id'],
            bbox=record['scope_bbox'],kind='geometry_review_sheet',review_id=record['review_id'],cells=cells,
            coordinate_space='per_cell_dxf_wcs',image_url=url+'/image.png',evidence_url=url,
            note='每格独立坐标；蓝线为工具候选，不能当原图。逐格核对身份，数值仅来自源几何。')
        workspace.store.put('visual_image',iid,image);images.append(image)
    return images
