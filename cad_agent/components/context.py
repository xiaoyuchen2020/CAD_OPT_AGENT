"""Titles are anchors, not proof of a drawing boundary or scope inheritance."""
import math
import re

from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id

TITLE = re.compile(r"^.{0,45}?(?:平面图|立面图|剖面图|节点(?:详)?图|详图|设计总说明|设计说明|门窗表|做法表)(?:[（(][^）)]{0,16}[）)])?$")
FLOOR = re.compile(r"地下[一二三四五六七八九十\d]+层|负[一二三四五六七八九十\d]+层|(?:地上)?[一二三四五六七八九十百\d]+(?:[～~至\-][一二三四五六七八九十百\d]+)?层|首层|标准层|屋顶|屋面|B\d+F|\d+F", re.I)
SCALE = re.compile(r"(?:比例\s*[:：]?\s*)?1\s*[:：]\s*(\d+(?:\.\d+)?)")
FIELDS = {"drawing_number": "图号", "drawing_name": "图名", "project_name": "工程名称", "discipline": "专业", "building": "楼栋", "subproject_name": "子项名称", "scale": "比例", "date": "日期", "revision": "版本号"}


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
    field_aliases = {key: {label} for key, label in FIELDS.items()}
    field_aliases['drawing_number'].update(['图纸编号', '图纸号'])
    field_aliases['drawing_name'].add('图纸名称')
    field_aliases['revision'].update(['版次', '版本'])
    field_aliases['project_name'].add('项目名称')
    table_fields = []
    for table in reading.tables:
        if table['layout_id'] != layout_id:
            continue
        metadata = {}
        for row in table['rows']:
            cells = row['cells']
            for i, cell in enumerate(cells[:-1]):
                label = re.sub(r'\s+|[:：]', '', cell['text'])
                key = next((k for k, labels in field_aliases.items() if label in labels), None)
                if not key:
                    continue
                value = cells[i+1]
                if re.sub(r'\s+|[:：]', '', value['text']) in set().union(*field_aliases.values()):
                    continue
                metadata.setdefault(key, {'values': [], 'state': 'observed'})['values'].append(dict(
                    value=value['text'].strip(), value_state='observed' if value['text'].strip() else 'blank_source_cell',
                    basis='explicit_adjacent_source_table_cells', label_bbox=cell['bbox'], bbox=value['bbox'],
                    source_evidence=[e for c in (cell,value) for eid in c.get('entity_ids',[]) for e in reading.entity_evidence(eid)]))
        if 'drawing_number' not in metadata or len(metadata) < 2:
            continue
        for field in metadata.values():
            if len({v['value'] for v in field['values']}) > 1:
                field['state'] = 'conflict'
        table_fields.append(dict(object_id=stable_id('titleblock', reading.snapshot, table['table_id']),
            kind='titleblock_fields', layout_id=layout_id, bbox=table['bbox'], table_id=table['table_id'],
            metadata=metadata, status='source_fields', boundary_basis='source_table_grid', diagnostics=[]))
    # Some merged title blocks have no closed table grid. Keep field/value
    # candidates grouped by an explicit sheet-number anchor and scale-relative
    # neighborhood. These do not create drawing boundaries or inherit scopes.
    labels = [(t, next((k for k, names in field_aliases.items()
                        if re.sub(r'\s+|[:：]', '', t['text']) in names), None)) for t in texts]
    pairs = []
    for label, key in labels:
        if key is None or abs(label.get('rotation', 0)) > .1:
            continue
        h=label['height']; x,y=label['origin'][:2]
        candidates = [t for t in texts if t['entity_id']!=label['entity_id']
            and abs(t.get('rotation',0))<.1 and .65*h<=t['height']<=1.6*h
            and .5*h < t['origin'][0]-label['bbox'][2] < (16 if key=='drawing_name' else 10)*h
            and abs(t['origin'][1]-y)<.6*h
            and re.sub(r'\s+|[:：]', '', t['text']) not in set().union(*field_aliases.values())]
        candidates.sort(key=lambda t:t['origin'][0])
        if not candidates:
            continue
        # A source label between the proposed pair prevents crossing cells.
        first=candidates[0]
        if any(l['entity_id']!=label['entity_id'] and abs(l['origin'][1]-y)<.6*h
               and label['bbox'][2]<l['origin'][0]<first['origin'][0] for l,k in labels if k):
            continue
        selected=[first]
        if key=='drawing_name':
            selected=[t for t in candidates if t['origin'][0]-first['origin'][0]<10*h]
        pairs.append(dict(key=key,label=label,values=selected))
    for anchor in [p for p in pairs if p['key']=='drawing_number']:
        label=anchor['label']; h=label['height']; x,y=label['origin'][:2]
        near=[p for p in pairs if abs(p['label']['origin'][1]-y)<6*h
              and abs(p['label']['origin'][0]-x)<60*h]
        if len([p for p in near if p['key']=='drawing_number'])!=1:
            continue
        metadata={}
        used=[]
        for pair in near:
            key=pair['key']; entities=[pair['label']]+pair['values'];used+=entities
            metadata.setdefault(key,dict(values=[],state='candidate'))['values'].append(dict(
                value='、'.join(t['text'].strip() for t in pair['values']),
                basis='bounded_aligned_explicit_label_candidate',
                source_evidence=[e for t in entities for e in reading.entity_evidence(t['entity_id'])]))
        bbox=[min(t['bbox'][0] for t in used),min(t['bbox'][1] for t in used),
              max(t['bbox'][2] for t in used),max(t['bbox'][3] for t in used)]
        table_fields.append(dict(object_id=stable_id('titlefields', reading.snapshot,label['entity_id']),
            kind='titleblock_fields',layout_id=layout_id,bbox=bbox,metadata=metadata,status='candidate',
            boundary_basis='explicit_label_neighborhood',diagnostics=['FIELD_ALIGNMENT_REQUIRES_REVIEW']))
    table_fields.sort(key=lambda f: (-len(f['metadata']), f['object_id']))
    frames_out = table_fields.copy()
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
