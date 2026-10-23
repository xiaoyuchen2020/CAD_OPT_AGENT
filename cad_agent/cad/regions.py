"""Spatial source units. Retains ownership and never merges across layouts/frames."""

import re
from statistics import median
from cad_agent.contracts import SourceRegion, SourcePart, stable_id
from cad_agent.cad.reader import union_box


def contains(box, item):
    x, y = (item.bbox[0] + item.bbox[2]) / 2, (item.bbox[1] + item.bbox[3]) / 2
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def build_regions(texts, scene, rectangles, segments, snapshot_id):
    regions, frames = [], []
    for layout in scene["layouts"]:
        lid = layout["layout_id"]
        items = [x for x in texts if x.layout_id == lid]
        if not items:
            continue
        h = median(x.height for x in items)
        candidates = []
        for rect in rectangles:
            b = rect["bbox"]
            if rect["layout_id"] != lid or b[2] - b[0] < 25 * h or b[3] - b[1] < 15 * h:
                continue
            enclosed = [t for t in items if contains(b, t)]
            # A closed rectangle is not by itself a verified title block.
            titles = [
                t
                for t in enclosed
                if any(
                    w in t.text
                    for w in (
                        "设计总说明",
                        "设计说明",
                        "材料表",
                        "做法表",
                        "门窗表",
                        "设备表",
                        "平面图",
                        "剖面图",
                        "立面图",
                        "节点图",
                        "砌体工程",
                    )
                )
            ]
            if titles and len(enclosed) >= 4:
                candidates.append((rect, titles))
        # Use innermost candidate borders; outer page margins must not duplicate them.
        candidates = [
            (r, ts)
            for r, ts in candidates
            if not any(
                other["id"] != r["id"]
                and r["bbox"][0] <= other["bbox"][0]
                and r["bbox"][1] <= other["bbox"][1]
                and r["bbox"][2] >= other["bbox"][2]
                and r["bbox"][3] >= other["bbox"][3]
                and other["bbox"] != r["bbox"]
                for other, _ in candidates
            )
        ]
        current_frames = []
        for rect, titles in candidates:
            title = max(titles, key=lambda t: (t.height, t.bbox[3])).text
            current_frames.append(
                {
                    "frame_id": stable_id("frame", snapshot_id, rect["id"]),
                    "name": title[:100],
                    "layout_id": lid,
                    "bbox": rect["bbox"],
                    "method": "closed_border_and_title_candidate",
                }
            )
        fallback = {
            "frame_id": stable_id("frame", snapshot_id, lid, "unassigned"),
            "name": layout["name"] + " · 未确认图框",
            "layout_id": lid,
            "bbox": layout["bbox"],
            "method": "layout_fallback",
        }
        grouped = {}
        used_fallback = False
        for t in items:
            owners = [f for f in current_frames if contains(f["bbox"], t)]
            frame = owners[0] if len(owners) == 1 else fallback
            used_fallback |= frame is fallback
            grouped.setdefault(frame["frame_id"], (frame, []))[1].append(t)
        frames.extend(current_frames)
        if used_fallback:
            frames.append(fallback)
        edges = [s for s in segments if s["layout_id"] == lid]
        for fid, (frame, entries) in grouped.items():
            # Closed row bands recover split table-cell text, without guessing values.
            row_groups, consumed = {}, set()
            horizontal = [
                (min(s["a"][0], s["b"][0]), s["a"][1], max(s["a"][0], s["b"][0]))
                for s in edges
                if abs(s["a"][1] - s["b"][1]) < 1e-5
                and abs(s["a"][0] - s["b"][0]) > h * 8
            ]
            vertical = [
                (s["a"][0], min(s["a"][1], s["b"][1]), max(s["a"][1], s["b"][1]))
                for s in edges
                if abs(s["a"][0] - s["b"][0]) < 1e-5
            ]
            # Use native insertion points: substituted CAD font extents can cross
            # several adjacent cells although the actual text belongs to one cell.
            for t in entries:
                if abs(t.rotation) > 0.1:
                    continue
                x, y = t.origin[:2]
                below = [e for e in horizontal if e[0] <= x <= e[2] and e[1] < y]
                above = [e for e in horizontal if e[0] <= x <= e[2] and e[1] > y]
                if not below or not above:
                    continue
                bot, top = max(below, key=lambda e: e[1]), min(
                    above, key=lambda e: e[1]
                )
                left, right = max(bot[0], top[0]), min(bot[2], top[2])
                xs = sorted(
                    set(
                        round(e[0], 3)
                        for e in vertical
                        if e[1] <= bot[1] + 0.1
                        and e[2] >= top[1] - 0.1
                        and left - 0.1 <= e[0] <= right + 0.1
                    )
                )
                if not (1.5 * h < top[1] - bot[1] < 12 * h and len(xs) >= 3):
                    continue
                if abs(xs[0] - left) > 0.1 or abs(xs[-1] - right) > 0.1:
                    continue
                # Ruled notes have narrow indent guides and one dominant text
                # column. These are paragraphs, not a row of data fields.
                if max(b - a for a, b in zip(xs, xs[1:])) > 0.78 * (right - left):
                    continue
                cell = next(((a, b) for a, b in zip(xs, xs[1:]) if a <= x < b), None)
                if cell:
                    key = (left, bot[1], right, top[1])
                    row_groups.setdefault(key, {}).setdefault(cell, []).append(t)
                    consumed.add(t.entity_id)
            made = []
            for box, cells in row_groups.items():
                row_key = stable_id("row", snapshot_id, fid, str(box))
                headers = [
                    c
                    for hb, c in row_groups.items()
                    if abs(hb[0] - box[0]) < 0.1
                    and abs(hb[2] - box[2]) < 0.1
                    and 0 <= hb[1] - box[3] < 12 * h
                    and {"块材", "密度等级", "强度等级"}.issubset(
                        {"".join(t.text for t in ts).strip() for ts in c.values()}
                    )
                ]
                if headers and all(
                    len(reading_lines(ts)) == 1 for ts in cells.values()
                ):
                    lines = [reading_lines(ts)[0] for _, ts in sorted(cells.items())]
                    r = region(
                        [t for line in lines for t in line],
                        frame,
                        snapshot_id,
                        "table_row",
                        lines=lines,
                        line_separator=" | ",
                    )
                    r.table_row_key = row_key
                    made.append(r)
                    continue
                cell_regions = []
                for (left, right), cell_items in sorted(cells.items()):
                    # Preserve separate material alternatives on separate cell
                    # lines; labels are linked explicitly, never numeric columns.
                    for run in reading_lines(cell_items):
                        r = region(run, frame, snapshot_id, "table_row")
                        r.table_row_key = row_key
                        cell_regions.append(r)
                labels = [
                    r
                    for r in cell_regions
                    if re.fullmatch(
                        r"(?:分户墙|外墙|内墙|内隔墙|隔墙|墙体)", r.text.strip()
                    )
                ]
                for r in cell_regions:
                    r.context_region_ids = [x.region_id for x in labels if x is not r]
                made.extend(cell_regions)
            columns = {}
            for t in entries:
                if t.entity_id in consumed:
                    continue
                x, y = t.origin[:2]
                guides = sorted(
                    set(
                        round(e[0], 3)
                        for e in vertical
                        if e[1] <= y <= e[2] and e[2] - e[1] > 15 * t.height
                    )
                )
                # Ignore narrow indentation guides. Wide bordered columns are
                # independent reading streams even at nearly equal baselines.
                wide = [(a, b) for a, b in zip(guides, guides[1:]) if b - a > 12 * h]
                inside = [pair for pair in wide if pair[0] <= x < pair[1]]
                indented = [pair for pair in wide if 0 < pair[0] - x < 8 * h]
                owner = inside or sorted(indented, key=lambda pair: pair[0] - x)
                key = tuple(owner[0]) if owner else None
                columns.setdefault(key, []).append(t)
            from cad_agent.cad.assembly_headings import reading_columns
            streams = [stream for key, column in columns.items() for stream in reading_columns(column, key)]
            for key, column in streams:
                lines = reading_lines(column, bounded=key is not None)
                paragraphs = []
                for line in lines:
                    if paragraphs and can_continue(paragraphs[-1][-1], line):
                        paragraphs[-1].append(line)
                    else:
                        paragraphs.append([line])
                units = []
                for lineset in paragraphs:
                    r = region(
                        [t for line in lineset for t in line],
                        frame,
                        snapshot_id,
                        "paragraph",
                        lines=lineset,
                    )
                    r.column_key = str(key) if key is not None else None
                    units.append(r)
                # A numbered heading applies only until the next sibling, in
                # the same physical reading column. Unbounded text uses only
                # nearby explicit headings, never whole-page inheritance.
                active = []
                for r in units:
                    level = clause_level(r.text)
                    if level is not None:
                        active = [(l, x) for l, x in active if l < level]
                    r.context_region_ids = [
                        x.region_id
                        for _, x in active
                        if key is not None or 0 < x.bbox[1] - r.bbox[1] < 8 * h
                    ]
                    if is_heading(r.text):
                        active.append((level if level is not None else 1, r))
                made.extend(units)
            regions.extend(made)
    return regions, frames


def clause_level(text):
    t = re.sub(r"\s+", "", text)
    if re.match(r"[一二三四五六七八九十]+[、．.]", t):
        return 0
    if re.match(r"\d+\.\d+[、．.]?", t):
        return 1
    if re.match(r"\d+[、．.]", t):
        return 1
    if re.match(r"(?:[（(]?\d+[）)]|[a-zA-Z][、．.])", t):
        return 2
    return None


def is_heading(text):
    from cad_agent.cad.assembly_headings import HEADING
    if HEADING.match(text):
        return True
    t = re.sub(r"\s+", "", text)
    if (
        len(t) <= 65
        and clause_level(t) is not None
        and not any(x in t for x in "。；;，,/")
        and not re.search(r"采用|详见|见顶板|基层|清理|施工厚度", t)
    ):
        if re.search(
            r"(?:工程|设计说明|设计要求|技术要求|构造要求|材料要求|做法|门窗表|设备表|材料表)[:：]?$",
            t,
        ):
            return True
    return len(t) < 110 and bool(
        re.search(
            r"(?:墙体|砌体|墙体材料|墙体工程).*[:：]$|(?:墙体|砌体)(?:工程|材料|做法)$",
            t,
        )
    )


def reading_lines(items, bounded=True):
    lines = []
    for t in sorted(items, key=lambda t: (-t.origin[1], t.origin[0])):
        candidate = None
        if t.kind == "TEXT" and abs(t.rotation) < 0.1:
            for line in reversed(lines[-8:]):
                first = line[0]
                # Independently numbered source entities are never one sentence.
                if re.match(r'^\s*\d+[、．.]', first.text) and re.match(r'^\s*\d+[、．.]', t.text):
                    continue
                if first.kind != "TEXT" or abs(first.rotation) > 0.1:
                    continue
                if (
                    abs(first.origin[1] - t.origin[1])
                    < min(first.height, t.height) * 0.35
                ):
                    ordered = sorted(line + [t], key=lambda x: x.origin[0])

                    # In unbordered areas require proximity. Native widths of
                    # CJK characters are close to their height, unlike fallback
                    # font bounding boxes. Only use this to detect reading gaps.
                    def advance(x):
                        return sum(
                            x.height * (1 if ord(c) > 255 else 0.65) for c in x.text
                        )

                    if all(
                        (
                            b.origin[0] - a.origin[0] - advance(a)
                            < 2 * max(a.height, b.height)
                            or bounded
                            and b.text.startswith(("）", ")"))
                        )
                        and max(a.height, b.height) < 1.7 * min(a.height, b.height)
                        for a, b in zip(ordered, ordered[1:])
                    ):
                        candidate = line
                        break
        if candidate is None:
            lines.append([t])
        else:
            candidate.append(t)
            candidate.sort(key=lambda x: x.origin[0])
    return sorted(lines, key=lambda line: (-line[0].origin[1], line[0].origin[0]))


def can_continue(previous, current):
    a, b = previous[0], current[0]
    if any(t.kind != "TEXT" or abs(t.rotation) > 0.1 for t in previous + current):
        return False
    h = max(a.height, b.height)
    gap = a.origin[1] - b.origin[1]
    text = "".join(t.text for t in previous).rstrip()
    following = "".join(t.text for t in current)
    if not (0.6 * h < gap < 2.8 * h and abs(a.origin[0] - b.origin[0]) < 5 * h):
        return False
    if clause_level(following) is not None or is_heading(text):
        return False
    # A terminal full stop closes the paragraph. Commas and unfinished words
    # are soft wraps. Heading colons never consume the first child clause.
    return bool(text) and text[-1] not in "。；;：:!?！？"


def region(
    items,
    frame,
    snapshot_id,
    kind,
    text=None,
    box=None,
    lines=None,
    line_separator="\n",
):
    ids = [x.entity_id for x in items]
    parts, chunks, offset = [], [], 0
    for index, line in enumerate(lines or [items]):
        if index:
            chunks.append(line_separator)
            offset += len(line_separator)
        for t in line:
            chunks.append(t.text)
            parts.append(
                SourcePart(
                    entity_id=t.entity_id,
                    text_span=[offset, offset + len(t.text)],
                    entity_text_span=[0, len(t.text)],
                    bbox=t.bbox,
                )
            )
            offset += len(t.text)
    return SourceRegion(
        region_id=stable_id("region", snapshot_id, *ids),
        layout_id=frame["layout_id"],
        frame_id=frame["frame_id"],
        frame_name=frame["name"],
        kind=kind,
        text="".join(chunks),
        entity_ids=ids,
        parts=parts,
        bbox=box or union_box(t.bbox for t in items),
    )
