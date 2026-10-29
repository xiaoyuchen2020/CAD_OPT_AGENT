"""Bounded XLSX source ingestion using only ZIP/XML, independent of Excel styles."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from collections import Counter
from xml.etree import ElementTree as ET


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DRAW = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
ART = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS = {"s": MAIN, "r": REL, "xdr": DRAW, "a": ART}
MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_CELLS = 250000
MAX_CELL_CHARS = 200000
MAX_SHEETS = 100
MAX_MERGES = 20000


class RuleLibraryError(ValueError):
    """An actionable ingestion/selection failure; never a successful empty import."""

    def __init__(self, code: str, message: str, diagnostics: list | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _xml(z: zipfile.ZipFile, part: str) -> ET.Element:
    try:
        raw = z.read(part)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise RuleLibraryError("unsafe_xml", f"XML entity declarations are not supported: {part}")
        return ET.fromstring(raw)
    except (KeyError, ET.ParseError, RuntimeError, zipfile.BadZipFile) as exc:
        raise RuleLibraryError("invalid_xlsx", f"Cannot read workbook XML part: {part}") from exc


def open_archive(content: bytes, max_source_bytes: int = MAX_SOURCE_BYTES) -> zipfile.ZipFile:
    if not isinstance(content, bytes) or not content:
        raise RuleLibraryError("invalid_xlsx", "Workbook content must be non-empty bytes")
    if len(content) > max_source_bytes:
        raise RuleLibraryError("source_limit", f"Workbook exceeds {max_source_bytes} bytes")
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        infos = z.infolist()
        names = [i.filename for i in infos]
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
            raise RuleLibraryError("expanded_limit", "Workbook ZIP exceeds entry or expanded-size limit")
        if len(names) != len(set(names)):
            raise RuleLibraryError("invalid_xlsx", "Workbook ZIP contains duplicate part names")
        for info in infos:
            name = info.filename
            if name.startswith("/") or "\\" in name or ".." in name.split("/") or info.flag_bits & 1:
                raise RuleLibraryError("invalid_xlsx", "Workbook ZIP contains an unsafe or encrypted part")
        if "xl/workbook.xml" not in names:
            raise RuleLibraryError("invalid_xlsx", "ZIP does not contain xl/workbook.xml")
        return z
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        if isinstance(exc, RuleLibraryError):
            raise
        raise RuleLibraryError("invalid_xlsx", "File is not a readable XLSX archive") from exc


def _relationships(z: zipfile.ZipFile, part: str) -> dict:
    relpart = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relpart not in z.namelist():
        return {}
    result = {}
    for rel in _xml(z, relpart):
        target = rel.attrib.get("Target", "")
        external = rel.attrib.get("TargetMode") == "External"
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
        if not external and (resolved.startswith("../") or "\\" in resolved):
            raise RuleLibraryError("invalid_xlsx", "Relationship escapes the workbook package")
        result[rel.attrib.get("Id")] = {"target": target, "part": resolved, "external": external, "type": rel.attrib.get("Type")}
    return result


def _coords(cell: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell)
    if not match:
        raise RuleLibraryError("invalid_xlsx", f"Invalid cell reference: {cell}")
    col = 0
    for char in match[1]:
        col = col * 26 + ord(char) - 64
    row = int(match[2])
    if col > 16384 or row > 1048576:
        raise RuleLibraryError("invalid_xlsx", "Cell reference exceeds XLSX bounds")
    return row, col


def _bounds(merge: str) -> tuple[int, int, int, int]:
    start, _, end = merge.partition(":")
    r1, c1 = _coords(start)
    r2, c2 = _coords(end or start)
    if r2 < r1 or c2 < c1:
        raise RuleLibraryError("invalid_xlsx", f"Invalid merged range: {merge}")
    return r1, c1, r2, c2


def _images(z: zipfile.ZipFile, sheet_part: str, root: ET.Element) -> tuple[list, list]:
    images, diagnostics = [], []
    sheet_rels = _relationships(z, sheet_part)
    for drawing in root.findall("s:drawing", NS):
        relationship = sheet_rels.get(drawing.attrib.get(f"{{{REL}}}id"))
        if not relationship or relationship["external"]:
            diagnostics.append({"code": "unresolved_drawing", "message": "A sheet drawing relationship could not be read"})
            continue
        part = relationship["part"]
        rels = _relationships(z, part)
        for index, anchor in enumerate(_xml(z, part)):
            pic = anchor.find("xdr:pic", NS)
            if pic is None:
                diagnostics.append({"code": "unsupported_drawing", "part": part, "index": index, "message": "Non-image drawing preserved only in original XLSX"})
                continue
            properties = pic.find("xdr:nvPicPr/xdr:cNvPr", NS)
            blip = pic.find("xdr:blipFill/a:blip", NS)
            rid = blip.attrib.get(f"{{{REL}}}embed") if blip is not None else None
            image_rel = rels.get(rid)
            position = {}
            for endpoint in ("from", "to"):
                node = anchor.find(f"xdr:{endpoint}", NS)
                if node is not None:
                    position[endpoint] = {child.tag.split("}")[-1]: int(child.text or "0") for child in node}
            item = {"image_id": f"img_{digest((part + ':' + str(index)).encode())[:20]}", "name": properties.attrib.get("name") if properties is not None else None,
                    "drawing_part": part, "relationship_id": rid, "anchor": position, "read_status": "not_interpreted"}
            if not image_rel or image_rel["external"]:
                item.update({"part": None, "status": "unavailable"})
                diagnostics.append({"code": "unresolved_image", "image_id": item["image_id"]})
            else:
                image_part = image_rel["part"]
                if image_part not in z.namelist():
                    raise RuleLibraryError("invalid_xlsx", "Image relationship references a missing part")
                raw = z.read(image_part)
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(posixpath.splitext(image_part)[1].lower(), "application/octet-stream")
                item.update({"part": image_part, "mime_type": mime, "sha256": digest(raw), "size_bytes": len(raw), "status": "source_only"})
            images.append(item)
    return images, diagnostics


def parse_xlsx(content: bytes, max_source_bytes: int = MAX_SOURCE_BYTES) -> dict:
    try:
        return _parse_xlsx(content, max_source_bytes)
    except RuleLibraryError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise RuleLibraryError("invalid_xlsx", "Workbook contains invalid XML values or unreadable ZIP parts") from exc


def _parse_xlsx(content: bytes, max_source_bytes: int = MAX_SOURCE_BYTES) -> dict:
    with open_archive(content, max_source_bytes) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for item in _xml(z, "xl/sharedStrings.xml"):
                value = "".join(t.text or "" for t in item.findall(".//s:t", NS))
                if len(value) > MAX_CELL_CHARS:
                    raise RuleLibraryError("cell_limit", "A shared string exceeds the text limit")
                shared.append(value)
        rels = _relationships(z, "xl/workbook.xml")
        sheets = _xml(z, "xl/workbook.xml").findall("s:sheets/s:sheet", NS)
        if len(sheets) > MAX_SHEETS:
            raise RuleLibraryError("sheet_limit", "Workbook exceeds the sheet limit")
        result, cell_count = [], 0
        for sheet in sheets:
            relation = rels.get(sheet.attrib.get(f"{{{REL}}}id"))
            if not relation or relation["external"]:
                raise RuleLibraryError("invalid_xlsx", "Worksheet relationship is missing or external")
            part = relation["part"]
            root = _xml(z, part)
            cells = {}
            for cell in root.findall("s:sheetData/s:row/s:c", NS):
                cell_count += 1
                if cell_count > MAX_CELLS:
                    raise RuleLibraryError("cell_limit", "Workbook exceeds the cell count limit")
                address = cell.attrib.get("r", "")
                _coords(address)
                if address in cells:
                    raise RuleLibraryError("invalid_xlsx", f"Duplicate cell: {sheet.attrib.get('name')}!{address}")
                typ = cell.attrib.get("t", "n")
                raw = cell.findtext("s:v", default=None, namespaces=NS)
                formula = cell.findtext("s:f", default=None, namespaces=NS)
                value = raw
                if typ == "s" and raw is not None:
                    try:
                        index = int(raw)
                        if index < 0:
                            raise IndexError
                        value = shared[index]
                    except (ValueError, TypeError, IndexError) as exc:
                        raise RuleLibraryError("invalid_xlsx", f"Invalid shared string at {address}") from exc
                elif typ == "inlineStr":
                    value = "".join(t.text or "" for t in cell.findall("s:is//s:t", NS))
                if value is not None and len(value) > MAX_CELL_CHARS:
                    raise RuleLibraryError("cell_limit", f"Cell {address} exceeds the text limit")
                if value is not None or formula is not None:
                    cells[address] = {"value": value, "raw_value": raw, "data_type": typ, "formula": formula}
            merges = [m.attrib["ref"] for m in root.findall("s:mergeCells/s:mergeCell", NS)]
            if len(merges) > MAX_MERGES:
                raise RuleLibraryError("merge_limit", "Worksheet exceeds the merge count limit")
            for merge in merges:
                _bounds(merge)
            images, diagnostics = _images(z, part, root)
            diagnostics.extend({"code": "unassigned_image", "image_id": image["image_id"], "message": "Image has no row anchor; its rule association requires review"} for image in images if "row" not in image["anchor"].get("from", {}))
            result.append({"name": sheet.attrib["name"], "part": part, "state": sheet.attrib.get("state", "visible"), "cells": cells, "merged_ranges": merges, "images": images, "diagnostics": diagnostics})
        return {"sheets": result, "cell_count": cell_count, "image_count": sum(len(s["images"]) for s in result)}


def _normal_header(text: str) -> str:
    return re.sub(r"[\s_]+", "", text).lower()


def _rule_id(value: str | None) -> tuple[str | None, str]:
    raw = (value or "").strip()
    disabled = bool(re.search(r"[（(]\s*(取消|不做|停用|废止)\s*[）)]", raw))
    clean = re.sub(r"[（(]\s*(取消|不做|停用|废止)\s*[）)]", "", raw).strip()
    if not re.fullmatch(r"[\w][\w.-]{0,127}", clean, flags=re.UNICODE):
        return None, "disabled" if disabled else "active"
    return clean, "disabled" if disabled else "active"


def extract_rules(workbook: dict) -> tuple[list, list]:
    rules, diagnostics = [], []
    for sheet in workbook["sheets"]:
        rows = {}
        for address, cell in sheet["cells"].items():
            row, col = _coords(address)
            rows.setdefault(row, {})[col] = (address, cell)
        header = None
        parent_aliases = {"要点编码", "要点编号"}
        id_aliases = {"规则编号", "规则编码", "ruleid", "rulecode"}
        for row, cells in sorted(rows.items()):
            if row > 100:
                break
            fields = {col: _normal_header(str(cell["value"] or "")) for col, (_, cell) in cells.items()}
            parents = [col for col, val in fields.items() if val in parent_aliases]
            ids = [col for col, val in fields.items() if val in id_aliases]
            if parents or ids:
                header = (row, parents[0] if parents else ids[0], ids[0] if parents and ids else None, fields)
                break
        if header is None:
            diagnostics.append({"code": "unrecognized_sheet", "sheet": sheet["name"], "message": "No explicit rule ID header was found; original cells are preserved"})
            continue
        header_row, id_col, step_col, fields = header
        title_cols = [col for col, value in fields.items() if value in {"优化要点", "规则名称", "规则标题", "title", "name", "rulename", "ruletitle"}]
        merge_map = {merge.split(":")[0]: merge for merge in sheet["merged_ranges"]}
        current, current_step = None, None
        for row, cells in sorted(rows.items()):
            if row <= header_row:
                continue
            raw_id = cells.get(id_col, (None, {}))[1].get("value")
            if raw_id is not None and str(raw_id).strip():
                identifier, status = _rule_id(str(raw_id))
                if identifier is None:
                    diagnostics.append({"code": "invalid_rule_id", "sheet": sheet["name"], "cell": cells[id_col][0], "value": raw_id})
                    current, current_step = None, None
                else:
                    title = next((cells[col][1]["value"] for col in title_cols if col in cells and cells[col][1]["value"]), identifier)
                    current = {"rule_id": identifier, "raw_id": raw_id, "title": title, "status": status, "readiness": "needs_interpretation", "source_rows": [], "source_cells": [], "steps": [], "supplements": [], "source_annotations": [], "images": [], "diagnostics": []}
                    rules.append(current)
                    current_step = None
            if current is None:
                if any(str(cell.get("value") or "").strip() for _, cell in cells.values()):
                    diagnostics.append({"code": "unassigned_row", "sheet": sheet["name"], "row": row, "message": "Nonempty row is preserved but could not be assigned to a rule"})
                continue
            refs = [{"sheet": sheet["name"], "cell": address, **cell, **({"merge": merge_map[address]} if address in merge_map else {})} for _, (address, cell) in sorted(cells.items())]
            current["source_rows"].append({"sheet": sheet["name"], "row": row})
            current["source_cells"].extend(refs)
            for ref in refs:
                if re.search(r"(?:20\d{2}[/.-]|更改|变更|补充|新增|取消|不做|人工判定|人工分析)", str(ref["value"] or "")):
                    current["source_annotations"].append({"sheet": ref["sheet"], "cell": ref["cell"], "value": ref["value"], "interpretation": "source_annotation_requires_review"})
            raw_step = cells.get(step_col, (None, {}))[1].get("value") if step_col else None
            if raw_step:
                step_id, status = _rule_id(str(raw_step))
                current_step = {"step_id": step_id, "raw_id": raw_step, "status": status, "source_rows": [{"sheet": sheet["name"], "row": row}], "source_cells": refs}
                current["steps"].append(current_step)
                if step_id is None:
                    current["diagnostics"].append({"code": "invalid_step_id", "row": row, "value": raw_step})
            elif len(current["source_rows"]) > 1:
                current["supplements"].append({"sheet": sheet["name"], "row": row, "source_cells": refs, "interpretation": "unresolved_attachment_or_revision"})
            if any(ref.get("formula") is not None for ref in refs):
                current["diagnostics"].append({"code": "formula_not_evaluated", "row": row})
        for rule in [r for r in rules if r["source_rows"] and r["source_rows"][0]["sheet"] == sheet["name"]]:
            rule_rows = [x["row"] for x in rule["source_rows"]]
            for picture in sheet["images"]:
                anchor = picture["anchor"].get("from", {})
                end = picture["anchor"].get("to", anchor)
                if "row" in anchor and (anchor["row"] + 1 <= max(rule_rows) and end.get("row", anchor["row"]) + 1 >= min(rule_rows)):
                    rule["images"].append(picture["image_id"])
            if rule["images"]:
                rule["diagnostics"].append({"code": "images_not_interpreted", "message": "Image sources are preserved but have not been interpreted"})
            rule["rule_text"] = "\n".join(f"[{ref['sheet']}!{ref['cell']}] {ref['value']}" for ref in rule["source_cells"] if ref["value"] is not None)
    duplicates = [key for key, count in Counter(rule["rule_id"] for rule in rules).items() if count > 1]
    if duplicates:
        raise RuleLibraryError("duplicate_rule_ids", "Rule IDs must be unique within one workbook", [{"code": "duplicate_rule_id", "rule_id": key} for key in duplicates])
    if not rules:
        raise RuleLibraryError("unrecognized_rulebook", "No rule records with an explicit rule ID column could be recognized", diagnostics)
    return rules, diagnostics
