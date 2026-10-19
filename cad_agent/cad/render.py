"""Display-only DXF rendering. The semantic entity index stays unchanged.

All geometry is in DXF WCS; both browser and region exports flip Y once.
Text uses ezdxf's layout engine with browser glyphs constrained to the measured
run width. This lightweight preview targets the pinned ezdxf 1.4.3 drawing API.
"""
from collections import Counter
from hashlib import sha256
from html import escape
import math
import unicodedata
from pathlib import Path

import ezdxf
from ezdxf import reorder
from ezdxf.entities import Insert
from ezdxf.xclip import XClip
from ezdxf.path import from_vertices
from ezdxf.tools import clipping_portal
from ezdxf.addons.drawing import RenderContext
from ezdxf.addons.drawing.backend import Backend
from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy, LinePolicy
from ezdxf.addons.drawing.frontend import UniversalFrontend
from ezdxf.addons.drawing.pipeline import RenderPipeline2d, prepare_string_for_rendering
from ezdxf.addons.drawing.unified_text_renderer import UnifiedTextRenderer
from ezdxf.fonts import fonts
from ezdxf.math import Matrix44, Vec3
from ezdxf.path import Command
from cad_agent.contracts import stable_id

RENDER_VERSION = 'ezdxf-wcs-light-v7'
TEXT_FONT = 'Arial, "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", sans-serif'
# Large multi-floor sheets exceed the former 250k-item budget once XCLIP
# scopes are restored. Preview packets spatially bound browser loading; keep
# finite server-side item and path budgets as well.
MAX_ITEMS = 2_000_000
MAX_ENTITIES = 80000
# Entity expansion is budgeted per source root, not per drawing. Storage
# budgets below remain drawing-wide, so a malicious block cannot grow forever.
MAX_PATH_CHARS = 240_000_000


def font_directory():
    from cad_agent.config import ROOT, local_env
    return Path(local_env().get('CAD_RENDER_FONT_DIR', str(ROOT / '.data/render-fonts')))


def font_signature():
    folder = font_directory()
    entries = [(p.name, p.stat().st_size, p.stat().st_mtime_ns)
               for p in sorted(folder.glob('*')) if p.is_file()]
    return sha256(repr((str(folder), entries)).encode()).hexdigest()


def number(v):
    if not math.isfinite(v):
        raise ValueError('Non-finite render coordinate')
    return f'{v:.10g}'


def xy(p):
    return f'{number(p.x)} {number(p.y)}'


def simplify_polyline(points, tolerance):
    """Iterative Douglas-Peucker, bounded by a fraction of this path's extent.

    Only dense straight-segment paths opt in; curves and filled faces stay exact.
    Keeping first/last vertices also preserves closed paths.
    """
    keep = {0, len(points)-1}
    stack = [(0, len(points)-1)]
    threshold = tolerance*tolerance
    while stack:
        first, last = stack.pop()
        ax, ay = points[first]; bx, by = points[last]
        dx, dy = bx-ax, by-ay
        length = dx*dx+dy*dy
        farthest, index = threshold, None
        for j in range(first+1, last):
            px, py = points[j]
            t = max(0., min(1., ((px-ax)*dx+(py-ay)*dy)/length)) if length else 0.
            distance = (px-ax-t*dx)**2+(py-ay-t*dy)**2
            if distance > farthest:
                farthest, index = distance, j
        if index is not None:
            keep.add(index)
            stack.extend(((first,index),(index,last)))
    return [points[j] for j in sorted(keep)]


def path_data(path, close=False, simplify=False):
    if not len(path):
        return ''
    if simplify and len(path) > 128:
        commands = list(path.commands())
        if all(c.type == Command.LINE_TO for c in commands):
            points = [path.start] + [c.end for c in commands]
            box = bounds(points)
            tolerance = max(box[2]-box[0], box[3]-box[1])*.001
            if tolerance > 0:
                points = simplify_polyline(points, tolerance)
                return 'M' + ' L'.join(xy(p) for p in points)
    out = ['M' + xy(path.start)]
    for c in path.commands():
        if c.type == Command.MOVE_TO:
            out.append('M' + xy(c.end))
        elif c.type == Command.LINE_TO:
            out.append('L' + xy(c.end))
        elif c.type == Command.CURVE3_TO:
            out.append('Q' + xy(c.ctrl) + ' ' + xy(c.end))
        elif c.type == Command.CURVE4_TO:
            out.append('C' + xy(c.ctrl1) + ' ' + xy(c.ctrl2) + ' ' + xy(c.end))
    if close:
        out.append('Z')
    return ' '.join(out)


def bounds(points):
    pts = list(points)
    if not pts:
        return [0., 0., 0., 0.]
    return [min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts)]


def union(boxes):
    return bounds((p for b in boxes for p in ((b[0], b[1]), (b[2], b[3]))))


class RenderLimit(RuntimeError):
    pass


class ExpansionLimit(RenderLimit):
    pass


class OutlineBackend(Backend):
    def __init__(self):
        super().__init__()
        self.items = []
        self.text_cache = {}
        self.path_chars = 0
        self.diagnostics = []
        self.entered = Counter()
        self.entity_count = 0

    def enter_entity(self, entity, properties):
        self.entity_count += 1
        if self.entity_count > MAX_ENTITIES or len(self.entity_stack) >= 24:
            raise ExpansionLimit('单个源实体展开数量或块嵌套深度达到绘制上限')
        self.entered[entity.dxftype()] += 1
        super().enter_entity(entity, properties)

    def set_background(self, color):
        pass

    def reserve(self, data):
        self.path_chars += len(data)
        if self.path_chars > MAX_PATH_CHARS:
            raise RenderLimit('绘制路径数据达到上限')

    def add(self, item, properties):
        if len(self.items) >= MAX_ITEMS:
            raise RenderLimit('绘制图元数量达到上限')
        for v in item['bbox']:
            number(v)
        item.update(color=properties.color, stroke_width=max(.65, min(4., properties.lineweight * 96 / 25.4)),
                    layer=properties.layer, handle=properties.handle,
                    kinds=list(dict.fromkeys(e.dxftype() for e, _ in self.entity_stack)))
        self.items.append(item)

    def draw_point(self, pos, properties):
        self.add(dict(type='point', x=pos.x, y=pos.y, bbox=[pos.x,pos.y,pos.x,pos.y]), properties)

    def draw_line(self, start, end, properties):
        d = 'M' + xy(start) + ' L' + xy(end)
        self.reserve(d)
        self.add(dict(type='path', d=d, filled=False, bbox=bounds([start,end])), properties)

    def draw_solid_lines(self, lines, properties):
        lines = list(lines)
        if not lines:
            return
        d = ' '.join('M' + xy(a) + ' L' + xy(b) for a,b in lines)
        self.reserve(d)
        self.add(dict(type='path', d=d, filled=False,
                      bbox=bounds(p for pair in lines for p in pair)), properties)

    def draw_path(self, path, properties):
        d = path_data(path, simplify=True)
        if d:
            self.reserve(d)
            self.add(dict(type='path', d=d, filled=False, bbox=bounds(path.extents())), properties)

    def draw_filled_paths(self, paths, properties):
        paths = [p for p in paths if len(p)]
        if paths:
            d = ' '.join(path_data(p, close=True) for p in paths)
            self.reserve(d)
            self.add(dict(type='path', d=d, filled=True,
                          bbox=bounds(v for p in paths for v in p.extents())), properties)

    def draw_filled_polygon(self, points, properties):
        pts = list(points.vertices())
        if pts:
            d = 'M' + ' L'.join(xy(p) for p in pts) + ' Z'
            self.reserve(d)
            self.add(dict(type='path', d=d, filled=True, bbox=bounds(pts)), properties)

    def draw_image(self, image_data, properties):
        self.diagnostics.append('IMAGE 未绘制：外部栅格图像不在本地 DXF 预览范围内')

    def clear(self):
        self.items.clear()


class CadTextEngine(UnifiedTextRenderer):
    """Preserve native TTF outlines and SHX advances when resources exist.

    ezdxf cannot load Bigfont. For those styles, ideographs use the CAD
    full-width cell (one cap-height); Latin advances come from the SHX font.
    Substitute outlines and layout use exactly the same advance metrics.
    """
    def __init__(self, face, cad_fonts, bigfonts):
        super().__init__()
        self.face = face
        self.cad_fonts = cad_fonts
        self.bigfonts = bigfonts
        self.fallback = super().get_font(face)
        ttf = fonts.font_manager.get_ttf_font(face.filename)
        self.em_scale = self.fallback.glyph_cache.font_measurements.cap_height / ttf['head'].unitsPerEm

    def get_font(self, font_face):
        if font_face.filename in self.cad_fonts:
            return self.fallback
        return super().get_font(font_face if fonts.font_manager.has_font(font_face.filename) else self.face)

    def is_stroke_font(self, font_face):
        return False  # SHX metrics are rendered with filled fallback outlines.

    def advance(self, char, base, bigfont=None):
        if bigfont:
            width=bigfont.advance(char)
            if width is not None:
                return width
        if unicodedata.east_asian_width(char) in ('W','F'):
            return 1.0
        if char.isspace():
            return base.space_width() if base else self.fallback.space_width()
        return base.text_width_ex(char,1.) if base else self.fallback.text_width_ex(char,1.)

    def get_font_measurements(self, font_face, cap_height=1.):
        if font_face.filename in self.cad_fonts:
            base=self.cad_fonts[font_face.filename]
            return (base or self.fallback).measurements.scale(cap_height)
        return super().get_font_measurements(font_face,cap_height)

    def get_text_line_width(self,text,font_face,cap_height=1.):
        if font_face.filename in self.cad_fonts:
            return sum(self.advance(c,self.cad_fonts[font_face.filename],self.bigfonts.get(font_face.filename)) for c in text)*cap_height
        return super().get_text_line_width(text,font_face,cap_height)

    def get_text_glyph_paths(self,text,font_face,cap_height=1.):
        if font_face.filename not in self.cad_fonts:
            return super().get_text_glyph_paths(text,font_face,cap_height)
        paths=[]; offset=0.
        for char in text:
            advance=self.advance(char,self.cad_fonts[font_face.filename],self.bigfonts.get(font_face.filename))
            glyphs=self.fallback.text_glyph_paths(char,1.)
            wide=unicodedata.east_asian_width(char) in ('W','F')
            natural=self.fallback.text_width_ex(char,1.)
            sx=(advance/natural if natural else 1.)*cap_height
            sy=(self.em_scale if wide else 1.)*cap_height
            matrix=Matrix44.scale(sx,sy,1) @ Matrix44.translate(offset*cap_height,0,0)
            for glyph in glyphs:
                glyph.transform_inplace(matrix)
                paths.append(glyph)
            offset+=advance
        return paths


class PreviewPipeline(RenderPipeline2d):
    def __init__(self, backend, face, cad_fonts, bigfonts):
        super().__init__(backend)
        self.text_engine = CadTextEngine(face,cad_fonts,bigfonts)
        self.default_font_face = face

    def draw_text(self, text, transform, properties, cap_height, dxftype='TEXT'):
        if self.clipping_portal.is_active:
            # Let ezdxf clip glyph paths inside VIEWPORT/XCLIP.
            return super().draw_text(text, transform, properties, cap_height, dxftype)
        text = prepare_string_for_rendering(text, dxftype)
        if not text.strip():
            return
        backend = self.backend
        face = properties.font or self.default_font_face
        key = face.filename + '\x1f' + text
        if key not in backend.text_cache:
            backend.text_cache[key] = self.text_engine.get_text_line_width(text, face, 1.)
        width = backend.text_cache[key]
        if width <= 0:
            return
        # Approximate cap box is sufficient for preview fitting/cropping. Keep
        # authored advances so adjacent DXF fragments do not overrun each other.
        box = [0., 0., width, 1.]
        mat = Matrix44.scale(cap_height) @ transform
        origin = mat.transform(Vec3(0,0,0))
        ux = mat.transform_direction(Vec3(1,0,0)); uy = mat.transform_direction(Vec3(0,1,0))
        matrix = [ux.x,ux.y,uy.x,uy.y,origin.x,origin.y]
        b = bounds(mat.transform(Vec3(x,y,0)) for x,y in
                   [(box[0],box[1]),(box[2],box[1]),(box[2],box[3]),(box[0],box[3])])
        font_size = 1. if any(unicodedata.east_asian_width(c) in ('W', 'F') for c in text) else 1.38
        backend.add(dict(type='text', matrix=matrix, text=text, width=width,
                         font_size=font_size, bbox=b),
                    self.get_backend_properties(properties))


class DrawingFrontend(UniversalFrontend):
    def draw_composite_entity(self, entity, properties):
        if not isinstance(entity, Insert):
            return super().draw_composite_entity(entity, properties)
        # ezdxf 1.4.3 only pops XCLIP when its frame is visible. Keep the
        # clip scoped to each INSERT (including MINSERT cells), regardless of
        # frame policy. ATTRIBs follow outside that clip, as in ezdxf.
        self.ctx.push_state(properties)
        try:
            inserts = entity.multi_insert() if entity.mcount > 1 else (entity,)
            for insert in inserts:
                clip = XClip(insert)
                active = clip.has_clipping_path and clip.is_clipping_enabled
                if active:
                    boundary = clip.get_wcs_clipping_path()
                    shape = (clipping_portal.make_inverted_clipping_shape(
                        boundary.inner_polygon(), outer_bounds=boundary.outer_bounds())
                        if boundary.is_inverted_clip else
                        clipping_portal.find_best_clipping_shape(boundary.vertices))
                    self.pipeline.push_clipping_shape(shape, None)
                try:
                    self.draw_entities(insert.virtual_entities(skipped_entity_callback=self.skip_entity))
                    if active and clip.get_xclip_frame_policy():
                        self.pipeline.draw_path(from_vertices(boundary.inner_polygon(), close=True), properties)
                finally:
                    if active:
                        self.pipeline.pop_clipping_shape()
                self.draw_entities(insert.attribs)
        finally:
            self.ctx.pop_state()

    def draw_hatch_pattern(self, polygon, paths, properties):
        # Material pattern lines can dwarf the actual drawing. Outline patterned
        # fills; native solid HATCH/SOLID still use the normal filled pipeline.
        for path in paths:
            # Pattern boundary paths are OCS, unlike ordinary drawing paths.
            # Apply elevation before the OCS basis (including mirrored normals).
            matrix = Matrix44.translate(0, 0, polygon.dxf.elevation.z)
            if polygon.ocs().transform:
                matrix = matrix @ polygon.ocs().matrix
            self.pipeline.draw_path(path.transform(matrix), properties)

    def skip_entity(self, entity, msg):
        if msg != 'invisible':
            self.pipeline.backend.diagnostics.append(f'{entity.dxftype()}: {msg}')

    def log_message(self, message):
        # Keep diagnostics visible rather than relying on a console log.
        if 'relative point size' not in message:
            self.pipeline.backend.diagnostics.append(str(message))


def unicode_font():
    candidates = ['Arial Unicode.ttf', 'NotoSansCJK-Regular.ttc', 'NotoSansSC-Regular.ttf',
                  'NotoSansCJKsc-Regular.otf', 'msyh.ttc', 'simhei.ttf']
    for name in candidates:
        if fonts.font_manager.has_font(name):
            return fonts.font_manager.get_font_face(name)
    raise ValueError('未找到支持中文的替代字体；请安装 Noto Sans CJK 后重新生成预览。')


def render_drawing(path: Path, snapshot_id: str):
    doc = ezdxf.readfile(path)
    font_dir = font_directory()
    if font_dir.is_dir():
        fonts.font_manager.scan_folder(font_dir)
    face = unicode_font()
    backend = OutlineBackend()
    config = Configuration(background_policy=BackgroundPolicy.WHITE, min_lineweight=.17,
                           line_policy=LinePolicy.SOLID,
                           hatching_timeout=3., max_flattening_distance=.1)
    ctx = RenderContext(doc)
    cad_fonts = {}
    bigfonts = {}
    bigfont_resources = {}
    from cad_agent.cad.bigfont import BigfontMetrics
    font_files = {p.name.lower():p for p in font_dir.glob("*") if p.is_file()}
    font_sources = {}
    for style in doc.styles:
        name = Path(style.dxf.font.replace('\\','/')).name
        if style.dxf.get('bigfont') or Path(name).suffix.lower()=='.shx' or (name and not Path(name).suffix):
            shx = name if name.lower().endswith('.shx') else name+'.shx'
            base = None
            if fonts.font_manager.has_font(shx):
                try:
                    base = fonts.make_font(shx,1.)
                except Exception:
                    pass
            key = 'cad:' + style.dxf.name
            cad_fonts[key] = base
            big_name=style.dxf.get('bigfont','').lower()
            if big_name and not big_name.endswith('.shx'):
                big_name += '.shx'
            if big_name in font_files:
                try:
                    if big_name not in bigfont_resources:
                        bigfont_resources[big_name]=BigfontMetrics(font_files[big_name])
                    bigfonts[key]=bigfont_resources[big_name]
                except (ValueError,IndexError):
                    pass
            ctx.fonts[style.dxf.name.lower()] = fonts.FontFace(filename=key)
            font_sources[style.dxf.name] = dict(font=name,bigfont=style.dxf.get('bigfont',''),
                                              metric_font=base.name if base else 'Unicode fallback',
                                              bigfont_metrics=bigfonts[key].name if key in bigfonts else 'fullwidth fallback')
        elif name and fonts.font_manager.has_font(name):
            ctx.fonts[style.dxf.name.lower()] = fonts.font_manager.get_font_face(name)
            font_sources[style.dxf.name] = dict(font=name,metric_font=name)
        else:
            ctx.fonts[style.dxf.name.lower()] = face
    pipeline = PreviewPipeline(backend, face, cad_fonts, bigfonts)
    frontend = DrawingFrontend(ctx, pipeline, config)
    layouts = []
    for layout in doc.layouts:
        start = len(backend.items)
        counts_before = backend.entered.copy()
        backend.entity_stack.clear()
        limited = False
        ctx.set_current_layout(layout)
        frontend.set_background(ctx.current_layout_properties.background_color)
        mapping = list(layout.get_redraw_order())
        roots = reorder.ascending(layout, mapping) if mapping else layout
        for entity in roots:
            backend.entity_count = 0
            try:
                frontend.draw_entities([entity])
            except ExpansionLimit as e:
                backend.diagnostics.append(f'{layout.name}/{entity.dxf.handle}: {e}')
            except RenderLimit as e:
                backend.diagnostics.append(str(e))
                limited = True
                break
            except Exception as e:
                backend.diagnostics.append(f'{layout.name}/{entity.dxf.handle} 绘制失败: {type(e).__name__}')
            else:
                continue
            # A rejected root may leave INSERT inheritance or XCLIP stacks open.
            # Rebuild transient state; keep completed items, fonts and ordering.
            saved_fonts = ctx.fonts.copy()
            ctx = RenderContext(doc)
            ctx.fonts.update(saved_fonts)
            ctx.set_current_layout(layout)
            backend.entity_stack.clear()
            pipeline = PreviewPipeline(backend, face, cad_fonts, bigfonts)
            frontend = DrawingFrontend(ctx, pipeline, config)
        items = backend.items[start:]
        lid = stable_id('layout',snapshot_id,layout.name)
        for item in items:
            item['layout_id'] = lid
        if items:
            layouts.append(dict(layout_id=lid,name=layout.name,bbox=union(i['bbox'] for i in items),
                                entered_types=dict(backend.entered - counts_before)))
        if limited:
            break
    return dict(version=RENDER_VERSION, font=face.filename, font_sources=font_sources,
                font_signature=font_signature(), items=backend.items,
                mode='lightweight', layouts=layouts,
                diagnostics=list(dict.fromkeys(backend.diagnostics)),
                entered_types=dict(backend.entered), complete=not backend.diagnostics)


def render_svg(render, layout_id, box, highlight=None, *, width=1000, height=700, raster=False):
    """Same lightweight text, order, fills and stroke as the browser."""
    x0,y0,x1,y1 = box
    w,h = max(x1-x0,1e-6),max(y1-y0,1e-6)
    # resvg does not implement non-scaling-stroke. For a fixed-size raster,
    # express screen widths in WCS units instead; browser SVG keeps its native
    # vector effect so interactive zoom still preserves screen widths.
    stroke_scale = 1 / min(width/w,height/h) if raster else 1
    vector_effect = '' if raster else ' vector-effect="non-scaling-stroke"'
    items = [i for i in render['items'] if i['layout_id']==layout_id and
             i['bbox'][0]<=x1 and i['bbox'][2]>=x0 and i['bbox'][1]<=y1 and i['bbox'][3]>=y0]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{number(x0)} {number(-y1)} {number(w)} {number(h)}" width="{number(width)}" height="{number(height)}">',
           '<rect x="'+number(x0)+'" y="'+number(-y1)+'" width="'+number(w)+'" height="'+number(h)+'" fill="#fafbf7"/>',
           '<g transform="scale(1 -1)">']
    for i in items:
        color=escape(i['color'],quote=True)
        if i['type']=='text':
            a,b,c,d,e,f = i['matrix']
            matrix=' '.join(number(v) for v in (a,b,-c,-d,e,f))
            out.append(f'<text transform="matrix({matrix})" fill="{color}" stroke="none" font-family="{escape(TEXT_FONT,quote=True)}" font-size="{number(i["font_size"])}" textLength="{number(i["width"])}" lengthAdjust="spacingAndGlyphs" xml:space="preserve">{escape(i["text"])}</text>')
        else:
            d=i.get('d') or f'M{number(i["x"])} {number(i["y"])} l0.000001 0'
            filled=i.get('filled',False)
            out.append(f'<path d="{d}" fill="{color if filled else "none"}" fill-rule="evenodd" stroke="{"none" if filled else color}" stroke-width="{number(i["stroke_width"]*stroke_scale)}"{vector_effect} stroke-linecap="round"/>')
    out.append('</g>')
    if highlight:
        a,b,c,d=highlight
        out.append(f'<rect x="{number(a)}" y="{number(-d)}" width="{number(c-a)}" height="{number(d-b)}" fill="#afc88c" fill-opacity=".2" stroke="#567c3e" stroke-width="{number(1.4*stroke_scale)}"{vector_effect}/>')
    if render['diagnostics']:
        out.append('<desc>部分绘制：'+escape('; '.join(render['diagnostics']))+'</desc>')
    out.append('</svg>')
    return ''.join(out)
