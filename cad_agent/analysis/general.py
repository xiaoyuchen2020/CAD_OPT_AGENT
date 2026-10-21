"""Domain-neutral, bounded views over immutable DXF sources. No generated code."""

import json
import math
import re
from collections import defaultdict
from html import escape
from statistics import median

from cad_agent.cache import workspace_cache, file_stamp
from cad_agent.contracts import stable_id
from cad_agent.evidence import bind_evidence
from cad_agent.cad.regions import is_heading

INDEX_VERSION = "general-v1"


def intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


class DrawingIndex:
    def __init__(self, workspace, snapshot):
        self.w = workspace
        self.snapshot = snapshot
        self.drawing = workspace.store.get("drawing", snapshot)
        paths = [workspace.store.artifact(snapshot, name) for name in ('regions.json', 'scene.json')]
        self.signature = (INDEX_VERSION, snapshot, *(file_stamp(p) for p in paths))
        cache = workspace_cache(workspace, '_reading_cache', max_entries=8)
        def build():
            regions = workspace.regions(snapshot)
            entity_sources = defaultdict(list)
            for r in regions:
                for part in r.parts:
                    entity_sources[part.entity_id].append((r, part))
            return dict(regions=regions, by_id={r.region_id:r for r in regions},
                        entity_sources=entity_sources, scene=None)
        data = cache.get_or_build(self.signature, build, lambda _: paths[0].stat().st_size * 6)
        self._data = data
        self.regions, self.by_id, self.entity_sources = data['regions'], data['by_id'], data['entity_sources']
        self._tables = None

    @property
    def scene(self):
        path = self.w.store.artifact(self.snapshot, 'scene.json')
        cache = workspace_cache(self.w, '_scene_cache', max_entries=4)
        return cache.get_or_build((INDEX_VERSION, file_stamp(path)),
                                  lambda: json.loads(path.read_text()),
                                  lambda _: path.stat().st_size * 6)

    def scope(self, layout_id="", frame_id="", section_id=""):
        if layout_id and layout_id not in {
            x["layout_id"] for x in self.drawing["layouts"]
        }:
            raise ValueError("布局不属于当前图纸")
        if frame_id and frame_id not in {x["frame_id"] for x in self.drawing["frames"]}:
            raise ValueError("图框不属于当前图纸")
        if section_id and section_id not in self.by_id:
            raise ValueError("章节不属于当前图纸")
        rows = [
            r
            for r in self.regions
            if (not layout_id or r.layout_id == layout_id)
            and (not frame_id or r.frame_id == frame_id)
        ]
        if section_id:
            # Transitive parser parents, never geometric nearest heading.
            ids = {section_id}
            for _ in range(24):
                nxt = ids | {
                    r.region_id for r in rows if ids.intersection(r.context_region_ids)
                }
                if nxt == ids:
                    break
                ids = nxt
            rows = [r for r in rows if r.region_id in ids]
        return rows

    def evidence(self, region, start=0, end=None):
        return bind_evidence(
            self.snapshot,
            region,
            start,
            min(len(region.text), start + 6000) if end is None else end,
            self.drawing["unit"],
        ).model_dump()

    def entity_evidence(self, entity_id):
        return [
            self.evidence(r, p.text_span[0], p.text_span[1])
            for r, p in self.entity_sources.get(entity_id, [])
            if 0 < p.text_span[1] - p.text_span[0] <= 6000
        ]

    def search(
        self,
        query="",
        keywords=None,
        match="any",
        layout_id="",
        frame_id="",
        section_id="",
        offset=0,
        limit=8,
        mode="keyword",
        alternatives=None,
        prioritize_headings=False,
    ):
        from cad_agent.analysis.recall import terms as recall_terms, matched, page
        groups = recall_terms(query, keywords, alternatives)
        # Explicit alternative words request a deterministic, exhaustive literal
        # enumeration. They are candidate vocabulary, not asserted synonyms.
        requested_mode = mode
        if alternatives:
            mode = 'keyword'
        if mode != "keyword":
            from cad_agent.analysis.retrieval import search

            return search(
                self,
                query,
                keywords,
                match,
                layout_id,
                frame_id,
                section_id,
                offset,
                limit,
                mode,
            )
        rows = self.scope(layout_id, frame_id, section_id)
        hits = []
        seen = set()
        for r in rows:
            accepted, found = matched(r.text, groups, match)
            if not accepted or r.region_id in seen:
                continue
            seen.add(r.region_id)
            pos = max(
                0,
                min((r.text.casefold().find(t.casefold()) for t in found), default=0)
                - 150,
            )
            hits.append(
                {
                    "region_id": r.region_id,
                    "frame_id": r.frame_id,
                    "layout_id": r.layout_id,
                    "kind": r.kind,
                    "snippet": r.text[pos : pos + 900],
                    "snippet_start": pos,
                    "text_length": len(r.text),
                    "matched_terms": found,
                    "matched_query_variants": found,
                    "match_basis": "literal_case_insensitive",
                    "parser_context_region_ids": r.context_region_ids,
                }
            )
        if prioritize_headings:
            from cad_agent.analysis.assembly import HEADING
            for item in hits:
                region = self.by_id[item['region_id']]
                for heading in HEADING.finditer(region.text):
                    end = region.text.find('\n', heading.start())
                    title = region.text[heading.start():end if end >= 0 else len(region.text)]
                    if any(term.casefold() in title.casefold() for term in item['matched_terms']):
                        item['assembly_heading'] = title
                        break
        hits.sort(key=lambda item: (not bool(item.get('assembly_heading')), item['region_id']))
        result = {
            "items": hits[offset : offset + limit],
            "total": len(hits),
            "offset": offset,
            "has_more": offset + limit < len(hits),
            "note": "仅检索已解析文字；章节筛选基于解析器关系，零命中不证明原图未注明。",
        }
        if prioritize_headings:
            result['ranking_context'] = 'explicit_assembly_heading_first_v1'
        if alternatives:
            result['retrieval'] = dict(requested_mode=requested_mode,effective_mode='keyword',
                fallback_reason='explicit_alternatives_literal_enumeration', score_is_confidence=False)
        return page(self,result,'search_sources',dict(query=query,keywords=keywords or [],match=match,
            alternatives=alternatives or [],layout_id=layout_id,frame_id=frame_id,section_id=section_id,
            offset=offset,limit=limit))

    def outline(self, kind="sections", layout_id="", frame_id="", offset=0, limit=8):
        rows = self.scope(layout_id, frame_id)
        if kind == "sections":
            items = [
                {
                    "region_id": r.region_id,
                    "title_candidate": r.text[:180],
                    "layout_id": r.layout_id,
                    "frame_id": r.frame_id,
                    "parent_region_ids": r.context_region_ids,
                    "state": "candidate",
                    "evidence": self.evidence(r),
                }
                for r in rows
                if is_heading(r.text)
            ]
        elif kind == "tables":
            items = [
                {
                    k: t[k]
                    for k in (
                        "table_id",
                        "layout_id",
                        "bbox",
                        "row_count",
                        "column_count",
                        "state",
                    )
                }
                | {
                    "preview": " | ".join(
                        c["text"][:60] for c in t["rows"][0]["cells"]
                    )[:300]
                }
                for t in self.tables
                if (not layout_id or t["layout_id"] == layout_id)
                and (
                    not frame_id
                    or any(
                        c["region_ids"]
                        and any(
                            self.by_id[rid].frame_id == frame_id
                            for rid in c["region_ids"]
                        )
                        for row in t["rows"]
                        for c in row["cells"]
                    )
                )
            ]
        elif kind == "frames":
            items = [
                f | {"state": "candidate"}
                for f in self.drawing["frames"]
                if (not layout_id or f["layout_id"] == layout_id)
                and (not frame_id or f["frame_id"] == frame_id)
            ]
        else:
            items = self.drawing["layouts"]
            if layout_id:
                items = [x for x in items if x["layout_id"] == layout_id]
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "has_more": offset + limit < len(items),
            "offset": offset,
            "view": kind,
            "diagnostics": self.drawing["diagnostics"][:8],
            "note": "图框、章节和表格为候选结构；布局数量不是独立图纸张数。",
        }

    @property
    def tables(self):
        if self._tables is None:
            self._tables = self._build_tables()
        return self._tables

    def _build_tables(self):
        """Recover bounded ruled grids; preserve cell geometry, never guess headers."""
        tables = []
        for layout in self.drawing["layouts"]:
            lid = layout["layout_id"]
            texts = [t for t in self.scene["texts"] if t["layout_id"] == lid]
            if not texts:
                continue
            h = median(t["height"] for t in texts)
            horizontals, verticals = set(), set()
            for line in self.scene["lines"]:
                if line["layout_id"] != lid:
                    continue
                for a, b in zip(line["points"], line["points"][1:]):
                    if abs(a[1] - b[1]) < 0.01 and abs(a[0] - b[0]) > 8 * h:
                        horizontals.add(
                            (
                                round(min(a[0], b[0]), 2),
                                round(max(a[0], b[0]), 2),
                                round(a[1], 2),
                            )
                        )
                    elif abs(a[0] - b[0]) < 0.01:
                        verticals.add(
                            (
                                round(a[0], 2),
                                round(min(a[1], b[1]), 2),
                                round(max(a[1], b[1]), 2),
                            )
                        )
            spans = defaultdict(list)
            for left, right, y in horizontals:
                spans[(left, right)].append(y)
            for (left, right), ys in sorted(spans.items()):
                bands = []
                for bottom, top in zip(sorted(set(ys)), sorted(set(ys))[1:]):
                    if not h < top - bottom < 40 * h:
                        continue
                    xs = sorted(
                        {
                            x
                            for x, y0, y1 in verticals
                            if y0 <= bottom + 0.1
                            and y1 >= top - 0.1
                            and left - 0.1 <= x <= right + 0.1
                        }
                    )
                    if (
                        len(xs) < 3
                        or abs(xs[0] - left) > 0.1
                        or abs(xs[-1] - right) > 0.1
                    ):
                        continue
                    if max(b - a for a, b in zip(xs, xs[1:])) > 0.85 * (right - left):
                        continue
                    cells = []
                    for x0, x1 in zip(xs, xs[1:]):
                        selected = sorted(
                            [
                                t
                                for t in texts
                                if abs(t.get("rotation", 0)) < 0.1
                                and x0 <= t["origin"][0] < x1
                                and bottom <= t["origin"][1] < top
                            ],
                            key=lambda t: (
                                -round(t["origin"][1] / max(h * 0.3, 0.01)),
                                t["origin"][0],
                            ),
                        )
                        ids = [t["entity_id"] for t in selected]
                        cells.append(
                            {
                                "bbox": [x0, bottom, x1, top],
                                "text": "\n".join(t["text"] for t in selected),
                                "entity_ids": ids,
                                "region_ids": list(
                                    dict.fromkeys(
                                        r.region_id
                                        for eid in ids
                                        for r, _ in self.entity_sources[eid]
                                    )
                                ),
                            }
                        )
                    if any(c["text"] for c in cells):
                        bands.append(
                            {"bbox": [left, bottom, right, top], "cells": cells}
                        )
                # Split disjoint bands instead of merging unrelated same-width tables.
                groups = []
                for band in sorted(bands, key=lambda r: -r["bbox"][3]):
                    if (
                        not groups
                        or abs(groups[-1][-1]["bbox"][1] - band["bbox"][3]) > 0.1
                    ):
                        groups.append([])
                    groups[-1].append(band)
                for group in groups:
                    if len(group) < 2:
                        continue
                    box = [left, group[-1]["bbox"][1], right, group[0]["bbox"][3]]
                    tid = stable_id(
                        "table", self.snapshot, lid, str(box), INDEX_VERSION
                    )
                    boundaries = sorted(
                        {
                            x
                            for row in group
                            for c in row["cells"]
                            for x in (c["bbox"][0], c["bbox"][2])
                        }
                    )
                    for i, row in enumerate(group):
                        row["row_index"] = i
                        for cell in row["cells"]:
                            cell["column_index"] = boundaries.index(cell["bbox"][0])
                            cell["column_span"] = (
                                boundaries.index(cell["bbox"][2]) - cell["column_index"]
                            )
                            cell["cell_id"] = stable_id(
                                "cell", tid, str(i), str(cell["column_index"])
                            )
                    tables.append(
                        {
                            "table_id": tid,
                            "layout_id": lid,
                            "bbox": box,
                            "rows": group,
                            "row_count": len(group),
                            "column_count": len(boundaries) - 1,
                            "state": "candidate",
                        }
                    )
        return sorted(
            tables, key=lambda t: (t["layout_id"], -t["bbox"][3], t["bbox"][0])
        )

    def read_table(self, table_id, row_offset=0, row_limit=3):
        table = next((t for t in self.tables if t["table_id"] == table_id), None)
        if table is None:
            raise ValueError("表格不属于当前图纸或结构版本")
        rows = []
        for row in table["rows"][row_offset : row_offset + row_limit]:
            cells = []
            for c in row["cells"]:
                evidence = [
                    e for eid in c["entity_ids"] for e in self.entity_evidence(eid)
                ]
                cells.append(
                    c
                    | {
                        "evidence": evidence,
                        "value_state": "observed" if c["text"] else "unknown",
                        "header_relation": "unconfirmed",
                        "unit": None,
                    }
                )
            rows.append(row | {"cells": cells})
        return {k: v for k, v in table.items() if k != "rows"} | {
            "items": rows,
            "row_offset": row_offset,
            "has_more": row_offset + row_limit < table["row_count"],
            "header_candidate_row": 0,
            "header_state": "unconfirmed",
            "note": "首行仅为表头候选；不自动继承单位。横向合并保留 column_span，跨行合并/无边线表/多级表头未确认。空值不能自动向下填充。",
        }

    def entities(
        self,
        spatial_relation='bbox_intersects',
        layout_id="",
        frame_id="",
        bbox=None,
        layer="",
        kind="",
        query="",
        offset=0,
        limit=12,
        alternatives=None,
    ):
        from cad_agent.analysis.recall import terms, matched, page
        groups = terms(query, alternatives=alternatives)
        self.scope(layout_id, frame_id)
        kind = kind.upper()
        frame_box = None
        if frame_id:
            f = next(f for f in self.drawing["frames"] if f["frame_id"] == frame_id)
            if layout_id and layout_id != f["layout_id"]:
                raise ValueError("布局与图框不匹配")
            layout_id = f["layout_id"]
            frame_box = f["bbox"]
            bbox = f["bbox"] if bbox is None else bbox
        if bbox is not None and (
            len(bbox) != 4
            or not all(math.isfinite(x) for x in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            raise ValueError("范围必须为有效原图坐标")
        items = []
        seen = set()
        for e in self.scene["texts"] + self.scene["lines"]:
            box = e.get("bbox")
            if box is None:
                pts = e["points"]
                box = [
                    min(x[0] for x in pts),
                    min(x[1] for x in pts),
                    max(x[0] for x in pts),
                    max(x[1] for x in pts),
                ]
            if layout_id and e["layout_id"] != layout_id:
                continue
            if bbox is not None and not intersects(box, bbox):
                continue
            if bbox is not None and spatial_relation == 'insertion_point_inside':
                origin = e.get('origin')
                if origin is None or not (bbox[0] <= origin[0] <= bbox[2] and bbox[1] <= origin[1] <= bbox[3]):
                    continue
            if frame_box is not None and not intersects(box, frame_box):
                continue
            if layer and e.get("layer") != layer:
                continue
            if kind and e.get("kind") != kind:
                continue
            accepted, found = matched(e.get('text',''),groups)
            if not accepted or e['entity_id'] in seen:
                continue
            seen.add(e['entity_id'])
            items.append(
                {
                    k: e.get(k)
                    for k in (
                        "entity_id",
                        "layout_id",
                        "kind",
                        "layer",
                        "entity_path",
                        "block_name",
                        "origin",
                        "text",
                    )
                }
                | {
                    "bbox": box,
                    "matched_terms": found,
                    "matched_query_variants": found,
                    "source_spans": [dict(region_id=r.region_id,text_span=p.text_span)
                                     for r,p in self.entity_sources[e['entity_id']]],
                    "source_locations": [dict(region_id=r.region_id,frame_id=r.frame_id,layout_id=r.layout_id)
                                         for r,_ in self.entity_sources[e['entity_id']]],
                    "source_region_ids": list(
                        dict.fromkeys(
                            r.region_id for r, _ in self.entity_sources[e["entity_id"]]
                        )
                    ),
                }
            )
        items.sort(key=lambda item: item['entity_id'])
        result = {
            "items": items[offset : offset + limit],
            "total": len(items),
            "has_more": offset + limit < len(items),
            "offset": offset,
            "unit": self.drawing["unit"],
            "note": "几何相交只表示空间邻近，不证明构件或引线关联。旧快照的几何元数据可能为空；重新导入可获取新版本。未做工程量测算。",
        }
        return page(self,result,'query_entities',dict(query=query,alternatives=alternatives or [],
            layout_id=layout_id,frame_id=frame_id,bbox=bbox,layer=layer,kind=kind,offset=offset,limit=limit,spatial_relation=spatial_relation))

    def inspect(self, region_id, padding=1.0):
        r = self.by_id.get(region_id)
        if r is None:
            raise ValueError("区域不属于当前图纸")
        box = list(r.bbox)
        dx = max(box[2] - box[0], 1) * padding
        dy = max(box[3] - box[1], 1) * padding
        box = [box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy]
        result = self.entities(layout_id=r.layout_id, bbox=box, limit=20)
        return {
            "region_id": region_id,
            "layout_id": r.layout_id,
            "bbox": box,
            "evidence": self.evidence(r),
            "nearby": result,
            "preview_url": f"/api/drawings/{self.snapshot}/sources/{region_id}/preview.svg?padding={padding}",
            "visual_input_to_model": False,
            "note": "局部 SVG 供用户查看；当前文本运行时没有读取图像。不能据此声称看见未解析图形。",
        }

    def svg(self, region_id, padding=1.0):
        from cad_agent.cad.render import render_svg
        view = self.inspect(region_id, padding)
        return render_svg(self.w.rendering(self.snapshot), view["layout_id"],
                          view["bbox"], highlight=self.by_id[region_id].bbox)
