"""Bounded local crops and candidate bindings; no model calls or arbitrary paths."""
import hashlib
import json
import math
import re
from xml.sax.saxutils import escape

from pydantic import Field, model_validator
from cad_agent.contracts import Record, stable_id
from cad_agent.cad.render import render_svg
from cad_agent.geometry.source import scoped_geometry_index

VISION_VERSION = 'local-image-v3-spatial-candidates'
MAX_IMAGE_BYTES = 1_500_000


class RenderRegion(Record):
    layout_id: str
    bbox: list[float] = Field(min_length=4, max_length=4)
    highlight_entity_ids: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def check(self):
        a,b,c,d = self.bbox
        if not all(math.isfinite(v) for v in self.bbox) or c <= a or d <= b:
            raise ValueError('需要有限且有面积的 WCS bbox。')
        if len(set(self.highlight_entity_ids)) != len(self.highlight_entity_ids):
            raise ValueError('图元不得重复。')
        return self


class BindVisualObjects(Record):
    image_id: str
    entity_ids: list[str] = Field(min_length=1, max_length=12)
    interpretation: str = Field(min_length=1, max_length=160)


class VisionService:
    def __init__(self, workspace, snapshot_id):
        self.w, self.sid = workspace, snapshot_id
        self.drawing = workspace.store.get('drawing', snapshot_id)

    def path(self, image_id):
        if not re.fullmatch(r'image_[a-zA-Z0-9]+', image_id):
            raise KeyError('Invalid image ID')
        return self.w.store.artifact_dir(self.sid) / 'vision' / (image_id + '.png')

    def get(self, image_id):
        item = self.w.store.get('visual_image', image_id)
        if item['snapshot_id'] != self.sid or item['source_sha256'] != self.drawing['sha256']:
            raise KeyError('Image belongs to another snapshot')
        return item

    def png(self, image_id):
        item = self.get(image_id)
        raw = self.path(image_id).read_bytes()
        if len(raw) > MAX_IMAGE_BYTES or hashlib.sha256(raw).hexdigest() != item['image_sha256']:
            raise ValueError('Image integrity mismatch')
        return raw

    def render(self, args):
        if args.layout_id not in {l['layout_id'] for l in self.drawing['layouts']}:
            raise ValueError('布局不属于当前图纸。')
        render = self.w.rendering(self.sid)
        box = args.bbox
        x0,y0,x1,y1 = box
        # Match the exact raster aspect ratio; equal WCS scale on both axes.
        scale = 1024 / max(x1-x0, y1-y0)
        width, height = max(1, round((x1-x0)*scale)), max(1, round((y1-y0)*scale))
        if min(width, height) < 64:
            raise ValueError('局部范围过于狭长，请扩大短边。')
        x1, y1 = x0 + width/scale, y0 + height/scale
        box = [x0,y0,x1,y1]
        items = [i for i in render['items'] if i['layout_id'] == args.layout_id and
                 i['bbox'][0] <= x1 and i['bbox'][2] >= x0 and i['bbox'][1] <= y1 and i['bbox'][3] >= y0]
        if not items or len(items) > 3000:
            raise ValueError('局部图像为空或图元过多，请缩小到一个房间/节点。')
        index = scoped_geometry_index(self.w, self.sid, args.layout_id, box, self.drawing['unit'])
        nearby = index.query(args.layout_id, box)
        by_id = {e['entity_id']:e for e in nearby}
        if any(e not in by_id for e in args.highlight_entity_ids):
            raise ValueError('高亮实体必须来自当前图像范围。')
        from .compute import compute
        ids=compute(self.w,'visual_candidates',dict(nearby=nearby,bounds=box,highlight_entity_ids=args.highlight_entity_ids))
        candidates=[by_id[eid] for eid in ids]
        image_id = stable_id('image', self.sid, self.drawing['sha256'], VISION_VERSION,
                             render['version'], render.get('font_signature'), json.dumps(args.model_dump(), sort_keys=True))
        try:
            cached = self.get(image_id)
            self.png(image_id)
            return cached
        except KeyError:
            pass
        svg = render_svg(render | {'items': items}, args.layout_id, box,
                         width=width, height=height, raster=True)
        overlays, mapping = [], []
        for n, entity in enumerate(candidates, 1):
            label = f'E{n}'
            a,b,c,d = entity['bbox']
            px,py = max(x0,min(x1,(a+c)/2)),max(y0,min(y1,(b+d)/2))
            mapping.append(dict(label=label, entity_id=entity['entity_id'], entity_path=entity['entity_path'],
                                kind=entity['kind'], layer=entity['layer'], bbox=entity['bbox'],
                                pixel_anchor=[round((px-x0)*scale,2),round((y1-py)*scale,2)]))
            if entity['entity_id'] in args.highlight_entity_ids:
                for points in entity['paths'][:8]:
                    path = 'M ' + ' L '.join(f'{x},{-y}' for x,y in points[:256])
                    overlays.append(f'<path d="{path}" fill="none" stroke="#dc5a24" stroke-width="{2/scale}"/>')
            overlays.append(f'<text x="{px}" y="{-py}" font-size="{13/scale}" fill="#005bce" stroke="white" stroke-width="{2/scale}" paint-order="stroke">{escape(label)}</text>')
        svg = svg.replace('</svg>', ''.join(overlays) + '</svg>')
        if len(svg) > 2_000_000:
            raise ValueError('局部路径过于复杂，请缩小范围。')
        import resvg_py
        raw = resvg_py.svg_to_bytes(svg_string=svg)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError('图像超过传输上限，请缩小范围。')
        path = self.path(image_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        from uuid import uuid4
        tmp = path.with_suffix('.'+uuid4().hex+'.tmp')
        tmp.write_bytes(raw); tmp.replace(path)
        url = f'/api/drawings/{self.sid}/images/{image_id}'
        result = dict(image_id=image_id, snapshot_id=self.sid, source_sha256=self.drawing['sha256'],
            image_sha256=hashlib.sha256(raw).hexdigest(), renderer_version=VISION_VERSION,
            drawing_renderer_version=render['version'], layout_id=args.layout_id, bbox=box,
            width=width, height=height, byte_count=len(raw), mime_type='image/png',
            coordinate_space='dxf_wcs', unit=self.drawing['unit'],
            wcs_to_pixel=[scale,0,0,-scale,-x0*scale,y1*scale],
            pixel_to_wcs=[1/scale,0,0,-1/scale,x0,y1],
            candidates=mapping, candidates_truncated=len(nearby)>len(candidates),
            render_item_count=len(items), render_truncated=False,
            diagnostics=render['diagnostics'][:12], diagnostics_truncated=len(render['diagnostics'])>12,
            geometry_complete=index.data['complete'], image_url=url+'/image.png', evidence_url=url,
            note='局部渲染，E编号/橙色高亮是辅助叠加；文字字体和线型可能替代，非原始像素。对象身份待模型或人工核对，图片不能证明尺寸/材料。')
        self.w.store.put('visual_image', image_id, result)
        return result

    def bind(self, args):
        image = self.get(args.image_id)
        ids = {e['entity_id'] for e in image['candidates']}
        if len(set(args.entity_ids)) != len(args.entity_ids) or not set(args.entity_ids) <= ids:
            raise ValueError('只能绑定当前图像返回的候选实体。')
        bid = stable_id('binding', args.image_id, json.dumps(args.model_dump(), sort_keys=True))
        result = dict(binding_id=bid, snapshot_id=self.sid, source_sha256=image['source_sha256'],
                      image_id=args.image_id, image_sha256=image['image_sha256'], layout_id=image['layout_id'],
                      bbox=image['bbox'], entity_ids=args.entity_ids, interpretation=args.interpretation,
                      identity_status='visual_candidate_unverified', evidence_url=image['evidence_url'],
                      note='实体归属已验证；interpretation 是模型视觉假设，不是已确认设计身份。')
        self.w.store.put('visual_binding', bid, result)
        return result

    def binding(self, binding_id):
        result = self.w.store.get('visual_binding', binding_id)
        self.get(result['image_id'])
        if result['snapshot_id'] != self.sid:
            raise KeyError(binding_id)
        return result
