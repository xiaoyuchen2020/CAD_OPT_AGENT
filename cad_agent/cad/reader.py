"""Read-only DXF ingestion with version-scoped identities and visible limitations.

No imports from building semantics, policies or the production CAD repository.
"""

from pathlib import Path
from itertools import islice
import math
import re
import ezdxf
from ezdxf import bbox as cad_bbox
from ezdxf.path import make_path
from cad_agent.contracts import Entity, Diagnostic, stable_id

PARSER_VERSION = "native-dxf-v5.2-native-tables"
MAX_ENTITIES = 80000
ENTITY_BATCH_SIZE = 512


def union_box(boxes):
    boxes = list(boxes)
    if not boxes:
        return [0.0, 0.0, 100.0, 100.0]
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def decode_text(entity):
    text = (
        entity.plain_text()
        if entity.dxftype() == "MTEXT"
        # Standalone attribute definitions display their tag, not the default
        # value. INSERT instance values are ATTRIB and still use dxf.text.
        else entity.dxf.get("tag", "") if entity.dxftype() == "ATTDEF"
        else entity.dxf.get("text", "")
    )
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4,8})", lambda m: chr(int(m[1], 16)), text)
    for a, b in [
        ("%%p", "±"),
        ("%%P", "±"),
        ("%%c", "Φ"),
        ("%%C", "Φ"),
        ("%%d", "°"),
        ("%%D", "°"),
    ]:
        text = text.replace(a, b)
    return text.replace("\x00", "").strip()


def walk_entities(entities, diagnostics, layout, path=(), active=(), counter=None, entity_limit=MAX_ENTITIES, start_index=0):
    counter = counter if counter is not None else [0]
    for index, entity in enumerate(entities, start=start_index):
        if counter[0] >= entity_limit:
            if not any((d.get("code") if isinstance(d, dict) else d.code) == "ENTITY_LIMIT" for d in diagnostics):
                diagnostics.append(
                    Diagnostic(
                        code="ENTITY_LIMIT",
                        message="图元数量超过本地解析上限，当前图纸仅部分解析。",
                    )
                )
            return
        counter[0] += 1
        token = str(entity.dxf.get("handle") or f"{index}:{entity.dxftype()}")
        entity_path = (*path, token)
        if entity.dxftype() == 'ACAD_TABLE':
            yield entity,list(entity_path)
            name=entity.dxf.get('geometry','')
            if name in active or len(active)>=24:
                diagnostics.append(Diagnostic(code='NATIVE_TABLE_CYCLE',message='原生表格图形块循环，未继续展开。'))
                continue
            extrusion=entity.xtags.get_subclass('AcDbBlockReference').get_first_value(210,(0,0,1))
            if tuple(extrusion)!=(0,0,1):
                diagnostics.append(Diagnostic(code='NATIVE_TABLE_OCS_UNSUPPORTED',message='原生表格非平面坐标变换未确认，保留元数据。'))
                continue
            try:
                yield from walk_entities(entity.__virtual_entities__(),diagnostics,layout,entity_path,(*active,name),counter,entity_limit)
            except (ValueError,AttributeError,TypeError):
                diagnostics.append(Diagnostic(code='NATIVE_TABLE_EXPANSION_FAILED',message='原生表格图形块无法展开，保留元数据。'))
        elif entity.dxftype() == "INSERT":
            if entity.mcount > 1:
                yield from walk_entities(
                    entity.multi_insert(),
                    diagnostics,
                    layout,
                    entity_path,
                    active,
                    counter,
                    entity_limit,
                )
                continue
            name = entity.dxf.name
            if name in active or len(active) >= 24:
                diagnostics.append(
                    Diagnostic(
                        code="BLOCK_CYCLE",
                        message=f"布局 {layout} 的循环或过深块未展开。",
                    )
                )
                continue
            block = entity.block()
            if block is not None and (block.block.dxf.flags & 12):
                diagnostics.append(
                    Diagnostic(
                        code="XREF_UNRESOLVED", message=f"外部参照 {name} 未解析。"
                    )
                )
                continue
            try:
                yield from walk_entities(
                    entity.attribs, diagnostics, layout, entity_path, active, counter, entity_limit
                )

                def skipped(e, reason):
                    diagnostics.append(
                        Diagnostic(
                            code="BLOCK_ENTITY_SKIPPED",
                            message=f"块中 {e.dxftype()} 无法展开。",
                        )
                    )

                virtual = entity.virtual_entities(skipped_entity_callback=skipped)
                yield from walk_entities(
                    virtual, diagnostics, layout, entity_path, (*active, name), counter, entity_limit
                )
            except Exception:
                diagnostics.append(
                    Diagnostic(
                        code="BLOCK_EXPANSION_FAILED", message=f"块 {name} 展开失败。"
                    )
                )
        else:
            yield entity, list(entity_path)


def entity_batches(entities, diagnostics, layout, *, batch_size=ENTITY_BATCH_SIZE,
                   entity_limit=None, stats=None):
    """Bound temporary expansion per source root, while visiting every root.

    Keep virtual-child ordinals and original handles intact: batching is not
    part of entity identity. An expensive/cyclic root cannot starve later roots
    or layouts. The low-level walker still supports an explicit total budget.
    """
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    limit = MAX_ENTITIES if entity_limit is None else entity_limit
    stats = stats if stats is not None else {}
    batch = []
    for ordinal, root in enumerate(entities):
        issues, counter = [], [0]
        for item in walk_entities((root,), issues, layout, counter=counter,
                                  entity_limit=limit, start_index=ordinal):
            batch.append(item)
            if len(batch) == batch_size:
                stats['batches'] = stats.get('batches', 0) + 1
                yield batch
                batch = []
        stats['roots'] = stats.get('roots', 0) + 1
        stats['visited'] = stats.get('visited', 0) + counter[0]
        stats['max_root_visited'] = max(stats.get('max_root_visited', 0), counter[0])
        for issue in issues:
            if issue.code == 'ENTITY_LIMIT':
                stats['limited_roots'] = stats.get('limited_roots', 0) + 1
                issue = Diagnostic(code='ENTITY_LIMIT', message=
                    f'布局 {layout} 的源实体 {root.dxf.get("handle") or ordinal} 展开预算耗尽；'
                    '该实体仅部分解析，后续源实体继续处理。')
            diagnostics.append(issue)
    if batch:
        stats['batches'] = stats.get('batches', 0) + 1
        yield batch


def text_box(entity, text, height):
    # CAD fonts may be missing. Text extents are estimates, never clause precision.
    try:
        b = cad_bbox.extents([entity], fast=True)
        if b.has_data:
            box = [
                float(b.extmin.x),
                float(b.extmin.y),
                float(b.extmax.x),
                float(b.extmax.y),
            ]
            if (
                all(math.isfinite(x) for x in box)
                and box[2] > box[0]
                and box[3] > box[1]
            ):
                return box
    except Exception:
        pass
    p = entity.dxf.insert
    lines = text.splitlines() or [""]
    width = (
        max(sum(1 if ord(c) > 255 else 0.6 for c in line) for line in lines) * height
    )
    return [
        p.x,
        p.y - height * len(lines),
        p.x + max(height, width),
        p.y + height * 0.2,
    ]


def read_drawing(path: Path, snapshot_id: str):
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        raise ValueError("DXF 无法读取：文件损坏或不是支持的 DXF 格式。") from e
    diagnostics = []
    units = {0: "unknown", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}.get(
        doc.units, "unknown"
    )
    if units == "unknown":
        diagnostics.append(
            Diagnostic(
                code="UNIT_UNKNOWN",
                message="图纸单位未明确；保留原坐标，不进行工程量测算。",
            )
        )
    texts, primitives, layouts, rectangles, segments = [], [], [], [], []
    native_tables=[]
    coverage = {}
    unsupported = set()
    for layout in doc.layouts:
        layout_id = stable_id("layout", snapshot_id, layout.name)
        layout_boxes, text_count = [], 0
        for entity, entity_path in (item for batch in entity_batches(
            layout, diagnostics, layout.name, stats=coverage
        ) for item in batch):
            kind = entity.dxftype()
            block_names = [
                doc.entitydb[token].dxf.name
                for token in entity_path
                if token in doc.entitydb and doc.entitydb[token].dxftype() == "INSERT"
            ]
            block_name = block_names[-1] if block_names else ""
            eid = stable_id("ent", snapshot_id, layout_id, "/".join(entity_path))
            if kind=='ACAD_TABLE':
                from cad_agent.tables.native import metadata
                native_tables.append(metadata(entity,eid,layout_id,entity_path))
                p=entity.dxf.get('insert',(0,0,0))
                layout_boxes.append([float(p[0]),float(p[1]),float(p[0])+1,float(p[1])+1])
            elif kind in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                try:
                    text = decode_text(entity)
                    if not text:
                        continue
                    height = max(
                        float(
                            entity.dxf.get("char_height", 1)
                            if kind == "MTEXT"
                            else entity.dxf.get("height", 1)
                        ),
                        1e-6,
                    )
                    box = text_box(entity, text, height)
                    origin = entity.dxf.insert
                    rotation = float(
                        entity.get_rotation()
                        if kind == "MTEXT"
                        else entity.dxf.get("rotation", 0)
                    )
                    item = Entity(
                        entity_id=eid,
                        entity_path=entity_path,
                        layout_id=layout_id,
                        kind=kind,
                        text=text,
                        bbox=box,
                        height=height,
                        rotation=rotation,
                        origin=[float(origin.x), float(origin.y)],
                        layer=entity.dxf.get("layer", "0"),
                        block_name=block_name,
                    )
                    texts.append(item)
                    layout_boxes.append(box)
                    text_count += 1
                except Exception:
                    diagnostics.append(
                        Diagnostic(
                            code="TEXT_READ_FAILED",
                            source_id=eid,
                            message="一个文字实体读取失败。",
                        )
                    )
            elif kind in (
                "LINE",
                "LWPOLYLINE",
                "POLYLINE",
                "ARC",
                "CIRCLE",
                "ELLIPSE",
                "SPLINE",
            ):
                try:
                    if kind == "LINE":
                        pts = [
                            [float(entity.dxf.start.x), float(entity.dxf.start.y)],
                            [float(entity.dxf.end.x), float(entity.dxf.end.y)],
                        ]
                    else:
                        pts = [
                            [float(p.x), float(p.y)]
                            for p in islice(
                                make_path(entity).flattening(distance=1.0, segments=8),
                                20001,
                            )
                        ]
                        if len(pts) > 20000:
                            diagnostics.append(
                                Diagnostic(
                                    code="CURVE_POINT_LIMIT",
                                    source_id=eid,
                                    message="一条复杂曲线超过预览细分上限，未完整绘制。",
                                )
                            )
                            continue
                    if len(pts) < 2 or not all(
                        math.isfinite(v) for p in pts for v in p
                    ):
                        continue
                    box = union_box([[x, y, x, y] for x, y in pts])
                    layout_boxes.append(box)
                    primitives.append(
                        {
                            "entity_id": eid,
                            "layout_id": layout_id,
                            "points": pts,
                            "kind": kind,
                            "layer": entity.dxf.get("layer", "0"),
                            "entity_path": entity_path,
                            "bbox": box,
                            "block_name": block_name,
                            "exact_linear": kind=='LINE' or (kind in ('LWPOLYLINE','POLYLINE') and not getattr(entity,'has_arc',True)),
                        }
                    )
                    for a, b in zip(pts, pts[1:]):
                        if abs(a[0] - b[0]) < 1e-5 or abs(a[1] - b[1]) < 1e-5:
                            segments.append({"layout_id": layout_id, "a": a, "b": b})
                    if (
                        kind in ("LWPOLYLINE", "POLYLINE")
                        and len(pts) == 5
                        and pts[0] == pts[-1]
                    ):
                        if all(
                            abs(a[0] - b[0]) < 1e-5 or abs(a[1] - b[1]) < 1e-5
                            for a, b in zip(pts, pts[1:])
                        ):
                            rectangles.append(
                                {"id": eid, "layout_id": layout_id, "bbox": box}
                            )
                except Exception:
                    unsupported.add(kind)
            elif kind == "DIMENSION":
                # Retain dimension-only layouts for the native annotation tool.
                # Calculation remains in components.dimensions; no fake text.
                try:
                    ext = cad_bbox.extents([entity], fast=True)
                    if ext.has_data:
                        layout_boxes.append([ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y])
                    else:
                        pts = [entity.dxf.get(k) for k in ("defpoint", "defpoint2", "defpoint3") if entity.dxf.hasattr(k)]
                        if pts:
                            layout_boxes.append([min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts)])
                except (ValueError, TypeError, AttributeError):
                    unsupported.add(kind)
            elif kind not in ("POINT", "VIEWPORT", "SEQEND", "ENDSEC"):
                unsupported.add(kind)
        if text_count or layout_boxes:
            layouts.append(
                {
                    "layout_id": layout_id,
                    "name": layout.name,
                    "bbox": union_box(layout_boxes),
                    "text_count": text_count,
                }
            )
    if unsupported:
        diagnostics.append(
            Diagnostic(
                code="PREVIEW_UNSUPPORTED",
                message="矢量预览未绘制这些类型："
                + ", ".join(sorted(unsupported))
                + "。",
            )
        )
    if not texts:
        diagnostics.append(
            Diagnostic(
                code="NO_NATIVE_TEXT",
                message="未读到原生文字，可能是扫描图或代理对象；本里程碑不做 OCR。",
            )
        )
    scene = {
        "layouts": layouts,
        "lines": primitives,
        "texts": [t.model_dump() for t in texts],
        "unit": units,
        "native_tables": native_tables,
        "parse_coverage": dict(coverage, batch_size=ENTITY_BATCH_SIZE, root_entity_budget=MAX_ENTITIES,
            traversal_complete=not any(d.code in {'ENTITY_LIMIT', 'BLOCK_CYCLE', 'XREF_UNRESOLVED',
                'BLOCK_ENTITY_SKIPPED', 'BLOCK_EXPANSION_FAILED','NATIVE_TABLE_CYCLE',
                'NATIVE_TABLE_OCS_UNSUPPORTED','NATIVE_TABLE_EXPANSION_FAILED'} for d in diagnostics)),
        "preview_note": "原 DXF 坐标矢量预览；字体替代可能影响文字宽度，定位框为实体估算范围。",
    }
    return texts, scene, rectangles, segments, diagnostics
