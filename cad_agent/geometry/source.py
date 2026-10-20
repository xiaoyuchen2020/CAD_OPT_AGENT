"""Immutable calculation geometry, separate from all display simplification.

Use the existing instance walker/IDs, retaining native parameters after INSERT
transforms. Tessellation is explicitly bounded and never labelled exact geometry.
"""
from cad_agent.cache import BoundedCache, workspace_cache, file_stamp
from pathlib import Path
from itertools import islice
import json
import math
import os

import ezdxf
from ezdxf.path import make_path, from_hatch
from ezdxf import bbox as cad_bbox
from shapely.geometry import LineString, box
from shapely.strtree import STRtree

from cad_agent.cad.reader import entity_batches
from cad_agent.contracts import stable_id

GEOMETRY_VERSION = "geometry-v3-root-batches"
UNIT_TO_M = {"mm": .001, "cm": .01, "m": 1., "in": .0254, "ft": .3048}
MAX_POINTS = 20000
MAX_TOTAL_POINTS = 400000


def finite_point(p):
    if len(p) != 2 or not all(math.isfinite(v) for v in p):
        raise ValueError("坐标必须为有限的二维 WCS 坐标。")
    return tuple(p)


def finite_box(b):
    if len(b) != 4 or not all(math.isfinite(v) for v in b) or b[0] >= b[2] or b[1] >= b[3]:
        raise ValueError("范围必须为有效原图坐标。")
    return box(*b)


def native_parameters(e):
    attrs = ("start", "end", "center", "radius", "start_angle", "end_angle",
             "major_axis", "ratio", "start_param", "end_param", "extrusion",
             "elevation", "dimtype", "defpoint", "defpoint2", "defpoint3",
             "defpoint4", "defpoint5", "angle", "text", "dimstyle", "geometry")
    result = {}
    for name in attrs:
        if e.dxf.hasattr(name):
            value = e.dxf.get(name)
            result[name] = list(value) if hasattr(value, "x") else value
    if e.dxftype() == "LWPOLYLINE":
        result.update(vertices=[list(v) for v in e.get_points("xyseb")], closed=e.closed)
    elif e.dxftype() == "POLYLINE":
        result.update(vertices=[list(v.dxf.location) + [v.dxf.get("bulge", 0)] for v in e.vertices], closed=e.is_closed)
    elif e.dxftype() == "SPLINE":
        result.update(degree=e.dxf.degree, control_points=[list(p) for p in e.control_points],
                      fit_points=[list(p) for p in e.fit_points], knots=list(e.knots), weights=list(e.weights))
    elif e.dxftype() == "DIMENSION":
        try:
            value = e.get_measurement()
            result["actual_measurement"] = list(value) if hasattr(value, "x") else value
        except (ValueError, TypeError, AttributeError):
            result["actual_measurement"] = None
        result["note"] = "原生标注数值与文字覆盖分别保留；尚未确认标注对象、样式换算或工程口径。"
    return result


def candidate_bounds(e):
    """Cheap conservative WCS extents for the spatial broad phase only.

    Arc candidates use their full circle; measurement still tessellates the
    original arc at the requested physical tolerance after spatial selection.
    """
    kind = e.dxftype()
    if kind == 'LINE':
        a, b = e.dxf.start, e.dxf.end
        return [min(a.x,b.x),min(a.y,b.y),max(a.x,b.x),max(a.y,b.y)]
    if kind in {'ARC', 'CIRCLE'}:
        center=e.ocs().to_wcs(e.dxf.center);radius=abs(e.dxf.radius)
        return [center.x-radius,center.y-radius,center.x+radius,center.y+radius]
    if kind == 'ELLIPSE':
        center=e.dxf.center;major=e.dxf.major_axis
        minor=e.dxf.extrusion.normalize().cross(major)*e.dxf.ratio
        rx=math.hypot(major.x,minor.x);ry=math.hypot(major.y,minor.y)
        return [center.x-rx,center.y-ry,center.x+rx,center.y+ry]
    ext=cad_bbox.extents([e],fast=True)
    if ext.has_data:
        return [ext.extmin.x,ext.extmin.y,ext.extmax.x,ext.extmax.y]
    return None


def _native_source(path, sid, cache):
    """Index transformed source entities before tessellation, independently of preview limits.

    Multiple drawings are retained within a bounded derived-data cache. Missing extents stay explicit: they
    cannot safely be dismissed as distant objects.
    """
    key = (GEOMETRY_VERSION, sid, file_stamp(path))
    source_path = path
    def build():
        doc = ezdxf.readfile(source_path)
        entities, shapes, ids, diagnostics = {}, [], [], []
        coverage = {}
        for layout in doc.layouts:
            lid = stable_id("layout", sid, layout.name)
            issues = []
            for e, path in (item for batch in entity_batches(layout, issues, layout.name, stats=coverage) for item in batch):
                if e.dxftype() in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "POINT", "VIEWPORT", "SEQEND"}:
                    continue
                eid = stable_id("ent", sid, lid, "/".join(path))
                bounds = None
                try:
                    bounds = candidate_bounds(e)
                    if bounds and not all(math.isfinite(v) for v in bounds):
                        bounds = None
                except (ValueError, TypeError, AttributeError, ZeroDivisionError):
                    pass
                entities[eid] = (e, path, lid, bounds)
                if bounds:
                    # Rectangle expansion avoids constructing 64-vertex rounded
                    # buffers for every one of tens of thousands of entities.
                    a,b,c,d=bounds
                    shapes.append(box(a-1e-7,b-1e-7,c+1e-7,d+1e-7)); ids.append(eid)
                else:
                    diagnostics.append(dict(code="UNLOCATED_GEOMETRY", kind=e.dxftype(), entity_id=eid, layout_id=lid))
            diagnostics.extend(dict(d.model_dump(), layout_id=lid) for d in issues)
        result = dict(doc=doc, entities=entities, ids=ids, tree=STRtree(shapes), diagnostics=diagnostics, coverage=coverage)
        return result
    return cache.get_or_build(key, build, lambda _: path.stat().st_size * 12)


def native_cache_options():
    """Explicit retention budget, shared by worker and local cache creation.

    Defaults to 512 MiB estimated retained native-index weight per cache. The
    existing file-size x 12 estimate is unchanged. Each spawned geometry worker
    owns one cache; legacy in-process workspace callers own a separate cache.
    N worker caches plus M local workspace caches can therefore retain up to
    (N + M) times this budget, in addition to source documents being built,
    derived geometry and other process memory. This is not an RSS limit.
    Oversized indices can still be computed, but are never retained.

    Set CAD_NATIVE_GEOMETRY_CACHE_BYTES and CAD_NATIVE_GEOMETRY_CACHE_ENTRIES
    before starting a worker or creating a local cache. Zero disables retention;
    existing caches retain their original limits until their owner is recreated.
    """
    result = {}
    for key, env, default in (
            ('max_bytes', 'CAD_NATIVE_GEOMETRY_CACHE_BYTES', 512 * 1024 * 1024),
            ('max_entries', 'CAD_NATIVE_GEOMETRY_CACHE_ENTRIES', 4)):
        raw = os.environ.get(env, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{env} must be a nonnegative integer.') from None
        if value < 0:
            raise ValueError(f'{env} must be a nonnegative integer.')
        result[key] = value
    return result


def native_source(workspace, sid):
    return _native_source(workspace.store.artifact(sid, "source.dxf"), sid,
                          workspace_cache(workspace, '_native_geometry', **native_cache_options()))


_WORKER_NATIVE = BoundedCache(**native_cache_options())
_WORKER_TEXT = BoundedCache(max_entries=2)


def source_text_metadata(workspace, sid, handles):
    """Bounded root TEXT attributes; parsing remains inside the worker."""
    from .compute import compute
    path=workspace.store.artifact(sid,'source.dxf')
    return compute(workspace,'source_text_metadata',dict(source_path=str(path.resolve()),
        source_stamp=list(file_stamp(path)),sid=sid,handles=list(handles)))


def compute_text_metadata(payload):
    path=Path(payload['source_path'])
    handles=payload['handles']
    if len(handles)>24 or any(not isinstance(h,str) or not h or len(h)>32 for h in handles):
        raise ValueError('原生文字属性每次最多读取24个有效handle。')
    if list(file_stamp(path))!=payload['source_stamp']:
        raise ValueError('文字来源文件已变化，请重新读取。')
    # Index only DXF byte offsets; do not materialize every object in a large
    # pump-room drawing merely to inspect two TEXT records.
    from ezdxf.lldxf import fileindex
    from ezdxf.lldxf.extendedtags import ExtendedTags
    from ezdxf.entities import factory
    def build_index():
        structure=fileindex.load(str(path));spans={};layouts=[];current=None;handle=None
        for entry in structure.index:
            if entry.code==0:
                if current is not None and handle:
                    span=(current.value,current.location,entry.location)
                    if current.value=='TEXT':spans[handle]=span
                    elif current.value=='LAYOUT':layouts.append(span)
                current=entry;handle=None
            elif entry.code==5:handle=entry.value
        return dict(encoding=structure.encoding,texts=spans,layouts=layouts)
    indexed=_WORKER_TEXT.get_or_build((str(path.resolve()),file_stamp(path)),build_index,lambda _:path.stat().st_size)
    result={}
    with path.open('rb') as stream:
        def entity(span):
            _,start,end=span;stream.seek(start)
            raw=stream.read(end-start).decode(indexed['encoding'],errors='surrogateescape').replace('\r\n','\n')
            return factory.load(ExtendedTags.from_text(raw))
        owners={}
        for span in indexed['layouts']:
            layout=entity(span)
            owners[layout.dxf.block_record_handle]=stable_id('layout',payload['sid'],layout.dxf.name)
        for handle in handles:
            span=indexed['texts'].get(handle)
            if not span:continue
            e=entity(span)
            if e.dxf.owner not in owners:continue
            result[handle]=dict(owner=e.dxf.owner,style=e.dxf.get('style','Standard'),text=e.plain_text(),
                origin=list(e.dxf.insert),height=e.dxf.height,rotation=e.dxf.get('rotation',0),
                layout_id=owners[e.dxf.owner],layer=e.dxf.get('layer','0'))
    if list(file_stamp(path))!=payload['source_stamp']:
        raise ValueError('文字来源文件已变化，结果未发布。')
    return result


class ScopedSourceBundle:
    """Request-local, detached scope reuse. Never retains a native source object."""
    def __init__(self, workspace, sid):
        self.workspace, self.sid = workspace, sid
        self.path = workspace.store.artifact(sid, 'source.dxf')
        self.stamp = list(file_stamp(self.path))
        self.key = None
        self.value = None

    def get(self, layout_id, bounds, unit):
        if list(file_stamp(self.path)) != self.stamp:
            raise ValueError('几何来源文件已变化，请重新读取。')
        key = (layout_id, tuple(bounds), unit)
        if self.key is not None and key != self.key:
            raise ValueError('请求内几何范围不匹配。')
        if self.value is None:
            from .compute import compute
            drawing = self.workspace.store.get('drawing', self.sid)
            self.value = compute(self.workspace, 'source_bundle', dict(
                source_path=str(self.path.resolve()), source_stamp=self.stamp,
                sid=self.sid, drawing={k:drawing[k] for k in ('sha256','unit')},
                selection=dict(bounds=list(bounds),layout_id=layout_id,effective_unit=unit)))
            self.key = key
        return self.value


def compute_source_bundle(payload):
    path = Path(payload['source_path'])
    if list(file_stamp(path)) != payload['source_stamp']:
        raise ValueError('几何来源文件已变化，请重新读取。')
    source = _native_source(path, payload['sid'], _WORKER_NATIVE)
    data = _build_geometry(path, payload['sid'], payload['drawing'], _WORKER_NATIVE,
                           prepared_source=source, **payload['selection'])
    selection = payload['selection']
    records = GeometryIndex(data).query(selection['layout_id'], selection['bounds'])
    from cad_agent.components.openings import effective_layer, arc_sources
    result = dict(records=records, complete=data['complete'], diagnostics=data['diagnostics'],
                  effective_layers={r['entity_id']:effective_layer(r,source) for r in records},
                  arc_sources=arc_sources(records,source))
    if list(file_stamp(path)) != payload['source_stamp']:
        raise ValueError('几何计算期间来源文件已变化，结果未发布。')
    return result


def source_descriptors(workspace, sid, records):
    from .compute import compute
    path=workspace.store.artifact(sid,'source.dxf')
    return compute(workspace,'source_descriptors',dict(source_path=str(path.resolve()),source_stamp=list(file_stamp(path)),
        sid=sid,records=[{k:r[k] for k in ('entity_id','kind','layer','entity_path') if k in r} for r in records]))


def source_dimensions(workspace, sid, layout_id, bounds, unit):
    from .compute import compute
    path=workspace.store.artifact(sid,'source.dxf')
    return compute(workspace,'source_dimensions',dict(source_path=str(path.resolve()),source_stamp=list(file_stamp(path)),
        sid=sid,layout_id=layout_id,bounds=bounds,unit=unit))


def source_viewports(workspace, sid, layout_id, subframes):
    from .compute import compute
    path=workspace.store.artifact(sid,'source.dxf')
    return compute(workspace,'source_viewports',dict(source_path=str(path.resolve()),source_stamp=list(file_stamp(path)),
        sid=sid,layout_id=layout_id,subframes=subframes))


def compute_source_metadata(operation, payload):
    path=Path(payload['source_path'])
    if list(file_stamp(path)) != payload['source_stamp']:
        raise ValueError('几何来源文件已变化，请重新读取。')
    source=_native_source(path,payload['sid'],_WORKER_NATIVE)
    if operation=='source_dimensions':
        from cad_agent.components.dimensions import read_dimensions_from_source
        result=read_dimensions_from_source(source,payload['sid'],payload['layout_id'],payload['bounds'],payload['unit'])
    elif operation=='source_viewports':
        from cad_agent.components.viewports import attach_model_scopes_from_source
        result=attach_model_scopes_from_source(source,payload['sid'],payload['layout_id'],payload['subframes'])
    elif operation=='source_descriptors':
        from cad_agent.components.openings import effective_layer, arc_sources
        records=payload['records']
        if any(r['entity_id'] not in source['entities'] for r in records):
            raise ValueError('几何描述图元不属于当前来源。')
        result=dict(effective_layers={r['entity_id']:effective_layer(r,source) for r in records},
                    arc_sources=arc_sources(records,source))
    else:
        raise ValueError('Unsupported source metadata operation')
    if list(file_stamp(path)) != payload['source_stamp']:
        raise ValueError('几何计算期间来源文件已变化，结果未发布。')
    return result


def compute_source_geometry(payload):
    # Path is produced from Store.artifact by the trusted parent. It is not an
    # HTTP/tool parameter. This worker has no database or publication access.
    path=Path(payload['source_path'])
    if list(file_stamp(path)) != payload['source_stamp']:
        raise ValueError('几何来源文件已变化，请重新读取。')
    result=_build_geometry(path,payload['sid'],payload['drawing'],_WORKER_NATIVE,**payload['selection'])
    if list(file_stamp(path)) != payload['source_stamp']:
        raise ValueError('几何计算期间来源文件已变化，结果未发布。')
    return result


def build_geometry(workspace, sid, *, bounds=None, layout_id=None, effective_unit=None, entity_ids=None):
    from .compute import compute
    drawing=workspace.store.get('drawing',sid)
    path=workspace.store.artifact(sid,'source.dxf')
    return compute(workspace,'source_geometry',dict(source_path=str(path.resolve()),source_stamp=list(file_stamp(path)),
        sid=sid,drawing={k:drawing[k] for k in ('sha256','unit')},
        selection=dict(bounds=bounds,layout_id=layout_id,effective_unit=effective_unit,entity_ids=entity_ids)))


def _build_geometry(source_path, sid, drawing, cache, *, bounds=None, layout_id=None, effective_unit=None, entity_ids=None, prepared_source=None):
    factor = UNIT_TO_M.get(effective_unit or drawing["unit"])
    # Unknown-unit geometry retains native curves, but must not exhaust a whole
    # drawing on a single circle. Physical accuracy is unknown until a unit is supplied.
    flatten = .001 / factor if factor else 1.
    records, diagnostics = [], []
    point_count = 0
    kinds = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "HATCH", "DIMENSION", "SOLID", "TRACE", "3DFACE"}
    if bounds is not None or entity_ids is not None:
        source = prepared_source if prepared_source is not None else _native_source(source_path, sid, cache)
        chosen = entity_ids if entity_ids is not None else [source["ids"][int(i)] for i in source["tree"].query(finite_box(bounds), predicate="intersects")]
        if any(eid not in source["entities"] for eid in chosen):
            raise ValueError("几何图元不存在或不属于当前快照。")
        entries = [(source["entities"][eid][0], source["entities"][eid][1], source["entities"][eid][2])
                   for eid in sorted(chosen) if layout_id is None or source["entities"][eid][2] == layout_id]
        diagnostics.extend(d for d in source["diagnostics"] if d.get("layout_id") == layout_id)
    else:
        doc = ezdxf.readfile(source_path)
        entries = ((e, path, stable_id("layout", sid, layout.name)) for layout in doc.layouts
                   for batch in entity_batches(layout, diagnostics, layout.name) for e, path in batch)
    ignored = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "POINT", "VIEWPORT", "SEQEND"}
    for e, path, lid in entries:
        kind = e.dxftype()
        if kind not in kinds:
            if kind not in ignored:
                diagnostics.append({"code": "UNSUPPORTED_GEOMETRY", "kind": kind, "layout_id": lid})
            continue
        eid = stable_id("ent", sid, lid, "/".join(path))
        rec = dict(entity_id=eid, entity_path=path, layout_id=lid, kind=kind,
                   layer=e.dxf.get("layer", "0"), native=native_parameters(e), paths=[], bbox=None,
                   coordinate_space="dxf_wcs", status="ready")
        try:
            paths = [] if kind == "DIMENSION" else list(from_hatch(e)) if kind == "HATCH" else [make_path(e)]
            linear = all(not p.has_curves for p in paths)
            for p in paths:
                xyz = [tuple(v) for v in islice(p.flattening(distance=flatten, segments=8), MAX_POINTS + 1)]
                point_count += len(xyz)
                if len(xyz) > MAX_POINTS or point_count > MAX_TOTAL_POINTS:
                    raise ValueError("GEOMETRY_POINT_LIMIT")
                if not all(math.isfinite(v) for q in xyz for v in q):
                    raise ValueError("NONFINITE_GEOMETRY")
                if xyz and max(q[2] for q in xyz) - min(q[2] for q in xyz) > flatten:
                    raise ValueError("NONPLANAR_GEOMETRY")
                if len(xyz) >= 2:
                    rec["paths"].append([[q[0], q[1]] for q in xyz])
            pts = [q for p in rec["paths"] for q in p]
            if pts:
                rec["bbox"] = [min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts)]
            rec["linear"] = linear
            rec["approximation_m"] = 0 if linear else (.001 if factor else None)
        except (ValueError, TypeError, AttributeError, ZeroDivisionError) as exc:
            rec.update(status="unavailable", paths=[], bbox=None, linear=False)
            diagnostics.append({"code": str(exc)[:100], "entity_id": eid, "layout_id": lid})
        records.append(rec)
        if point_count > MAX_TOTAL_POINTS:
            break
    diagnostics = [d.model_dump() if hasattr(d, "model_dump") else d for d in diagnostics]
    return dict(version=GEOMETRY_VERSION, snapshot_id=sid, source_sha256=drawing["sha256"],
                unit=drawing["unit"], applied_unit=effective_unit or drawing["unit"], unit_to_m=factor,
                entities=records, diagnostics=diagnostics, scope_bbox=bounds,
                complete=not diagnostics, coordinate_space="dxf_wcs")


def scoped_geometry_index(workspace, sid, layout_id, bounds, unit):
    return GeometryIndex(build_geometry(workspace, sid, bounds=bounds, layout_id=layout_id, effective_unit=unit), workspace)


class GeometryIndex:
    def __init__(self, data, workspace=None):
        self.workspace = workspace
        self.data = data
        self.by_id = {r["entity_id"]: r for r in data["entities"]}
        self.ids, self.shapes = [], []
        self.layouts = {}
        if workspace is not None:
            return
        for r in data["entities"]:
            for path in r["paths"]:
                self.ids.append(r["entity_id"])
                self.shapes.append(LineString(path))
        self.tree = STRtree(self.shapes)

    def query(self, layout_id, bounds, relation="intersects"):
        if self.workspace is not None:
            from .compute import compute
            ids = compute(self.workspace,'spatial_query',dict(data=self.data,layout_id=layout_id,bounds=bounds,relation=relation))
            return [self.by_id[eid] for eid in ids]
        region = finite_box(bounds)
        ids = set()
        for i in self.tree.query(region, predicate="intersects"):
            eid = self.ids[int(i)]
            if self.by_id[eid]["layout_id"] == layout_id:
                ids.add(eid)
        records = [self.by_id[eid] for eid in sorted(ids)]
        if relation == "within":
            records = [r for r in records if all(region.covers(LineString(p)) for p in r["paths"])]
        return records


def geometry_index(workspace, sid):
    drawing = workspace.store.get("drawing", sid)
    source = workspace.store.artifact(sid, "source.dxf")
    stamp = file_stamp(source)
    key = (GEOMETRY_VERSION, sid, drawing.get('sha256'), drawing.get('unit'), stamp)
    cache = workspace_cache(workspace, '_geometry_cache', max_entries=4)
    def build():
        target = workspace.store.artifact(sid, "geometry.json")
        try:
            data = json.loads(target.read_text()) if target.exists() else None
        except (ValueError, OSError):
            data = None
        if (not data or data.get("version") != GEOMETRY_VERSION
                or data.get("source_stamp") != list(stamp)
                or data.get("source_sha256") != drawing.get("sha256")
                or data.get("unit") != drawing.get("unit")):
            data = build_geometry(workspace, sid)
            data['source_stamp'] = list(stamp)
            workspace.store.write_json(target, data)
        return GeometryIndex(data, workspace)
    return cache.get_or_build(key, build,
                              lambda result: len(json.dumps(result.data, ensure_ascii=False).encode()) * 6)
