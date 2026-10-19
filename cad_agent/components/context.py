"""Titles are anchors, not proof of a drawing boundary or scope inheritance."""
import math
import re

from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id

TITLE = re.compile(r"^.{0,45}?(?:平面图|立面图|剖面图|节点(?:详)?图|详图|设计总说明|设计说明|门窗表|做法表)(?:[（(][^）)]{0,16}[）)])?$")
FLOOR = re.compile(r"地下[一二三四五六七八九十\d]+层|负[一二三四五六七八九十\d]+层|(?:地上)?[一二三四五六七八九十百\d]+(?:[～~至\-][一二三四五六七八九十百\d]+)?层|首层|标准层|屋顶|屋面|B\d+F|\d+F", re.I)
SCALE = re.compile(r"(?:比例\s*[:：]?\s*)?1\s*[:：]\s*(\d+(?:\.\d+)?)")
FIELDS = {"drawing_number": "图号", "drawing_name": "图名", "project_name": "工程名称", "discipline": "专业", "building": "楼栋"}


def build_context(reading, layout_id, frames):
    texts = [t for t in reading.scene["texts"] if t["layout_id"] == layout_id]
    points = [Point(t["origin"]) for t in texts]
    tree = STRtree(points)
    regions = {eid: r.region_id for r in reading.regions for eid in r.entity_ids}
    def source(t):
        return dict(entity_id=t["entity_id"], region_id=regions.get(t["entity_id"]),
                    text=t["text"], bbox=t["bbox"], rotation=t["rotation"])
    titles = []
    for t in texts:
        label = re.sub(r"\s+", "", t["text"])
        if TITLE.fullmatch(label) and not re.search(r"详见|参见|见图|目录", label):
            titles.append((t, label))
    title_ids = {t["entity_id"] for t, _ in titles}
    borders = []
    for e in reading.scene["lines"]:
        pts = e["points"]
        if e["layout_id"] != layout_id or len(pts) != 5 or pts[0] != pts[-1]:
            continue
        poly = Polygon(pts)
        if not poly.is_valid or poly.area <= 0:
            continue
        vectors = [(b[0]-a[0], b[1]-a[1]) for a,b in zip(pts, pts[1:])]
        if any(abs(a[0]*b[0]+a[1]*b[1]) > 1e-5*math.hypot(*a)*math.hypot(*b)
               for a,b in zip(vectors, vectors[1:]+vectors[:1])):
            continue
        enclosed = [texts[int(i)] for i in tree.query(poly, predicate="covers")]
        anchors = [t for t in enclosed if t["entity_id"] in title_ids]
        if not anchors or len(enclosed) < 4:
            continue
        h = max(t["height"] for t in anchors)
        if min(math.hypot(*v) for v in vectors) < 15*h:
            continue
        borders.append(dict(id=stable_id("border", reading.snapshot, e["entity_id"]),
                            entity_id=e["entity_id"], polygon=pts, shape=poly, texts=enclosed,
                            title_ids=[t["entity_id"] for t in anchors], bbox=list(poly.bounds)))
    frames_out = []
    for f in frames:
        frames_out.append(dict(object_id=f["frame_id"], kind="frame", layout_id=layout_id,
            frame_id=f["frame_id"], name=f["name"], bbox=f["bbox"], status="candidate",
            boundary_basis=f["method"], metadata={}, diagnostics=["frame_identity_unconfirmed"]))
    # Retain nested/rotated borders without silently replacing existing frame IDs.
    for b in borders:
        metadata = {}
        for key, label in FIELDS.items():
            values = []
            for t in b["texts"]:
                match = re.fullmatch(r"\s*"+label+r"\s*[:：]\s*(.{1,80})", t["text"])
                if match:
                    values.append(dict(value=match[1].strip(), source=source(t)))
            if values:
                metadata[key] = dict(values=values, state="conflict" if len({v["value"] for v in values})>1 else "observed")
        frames_out.append(dict(object_id=b["id"], kind="border_candidate", layout_id=layout_id,
            bbox=b["bbox"], boundary=b["polygon"], entity_ids=[b["entity_id"]], metadata=metadata,
            status="candidate", boundary_basis="closed_rectangle_with_title_anchors",
            diagnostics=["border_may_be_frame_or_subframe"]))
    subframes = []
    for t, label in titles:
        enclosing = sorted((b for b in borders if t["entity_id"] in b["title_ids"]), key=lambda b:b["shape"].area)
        border = enclosing[0] if enclosing else None
        owners = [f for f in frames if f["bbox"][0]<=t["origin"][0]<=f["bbox"][2] and f["bbox"][1]<=t["origin"][1]<=f["bbox"][3]]
        dedicated = border is not None and len(border["title_ids"]) == 1
        kind = next((v for k,v in [("平面", "PLAN"),("立面", "FACADE"),("剖面", "SECTION"),("详图", "DETAIL"),("节点", "DETAIL"),("说明", "NOTES"),("表", "TABLE")] if k in label), "UNKNOWN")
        h = t["height"]
        scales = []
        for i in tree.query(Point(t["origin"]).buffer(20*h)):
            other = texts[int(i)]
            if abs((other["rotation"]-t["rotation"]+180)%360-180) > 1:
                continue
            if border and not border["shape"].covers(Point(other["origin"])):
                continue
            for m in SCALE.finditer(other["text"]):
                scales.append(dict(value="1:"+m[1], source=source(other)))
        subframes.append(dict(object_id=stable_id("subframe", reading.snapshot, t["entity_id"]),
            kind="subframe", layout_id=layout_id, title=label, title_source=source(t),
            frame_ids=[f["frame_id"] for f in owners], parent_border_id=border["id"] if border else None,
            view_type=kind, floor_candidates=FLOOR.findall(label), rotation=t["rotation"],
            scale_candidates=scales, scale_state="conflict" if len({s["value"] for s in scales})>1 else "candidate" if scales else "unknown",
            bbox=border["bbox"] if dedicated else None, title_bbox=t["bbox"],
            boundary=border["polygon"] if dedicated else None, status="candidate",
            boundary_basis="single_title_closed_border" if dedicated else "title_anchor_only",
            diagnostics=[] if dedicated else ["subframe_extent_unresolved"],
            note="标题、楼层与比例关联是候选；比例不转换模型坐标单位。无独立边界时不能以标题框代替子图范围。"))
    return dict(frames=frames_out, subframes=subframes,
                diagnostics=["context_identity_unconfirmed", "metadata_requires_explicit_field_labels"],
                coverage="parsed_native_text_and_closed_rectangles_only")
