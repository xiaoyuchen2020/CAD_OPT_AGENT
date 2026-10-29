import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from cad_agent.rules import RuleLibraryError, RuleLibraryStore
from cad_agent.rules import parser
from cad_agent.rules.store import BUILTIN_PATH


def workbook(rows, *, extra_parts=None, sheet_name="Rules", merges=()):
    data = []
    for index, row in enumerate(rows, 1):
        cells = []
        for col, value in enumerate(row, 1):
            if value is None:
                continue
            address = chr(64 + col) + str(index)
            cells.append(f'<c r="{address}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        data.append(f'<row r="{index}">{"".join(cells)}</row>')
    merge_xml = '<mergeCells>' + ''.join(f'<mergeCell ref="{m}"/>' for m in merges) + '</mergeCells>' if merges else ''
    parts = {
        "xl/workbook.xml": f'<workbook xmlns="{parser.MAIN}" xmlns:r="{parser.REL}"><sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{parser.MAIN}"><sheetData>{"".join(data)}</sheetData>{merge_xml}</worksheet>',
    }
    parts.update(extra_parts or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return stream.getvalue()


def simple(rule_id="R001", text="Apply when the source establishes the condition"):
    return workbook([["规则编号", "规则名称", "规则内容"], [rule_id, "A test rule", text]])


@pytest.fixture
def store(tmp_path):
    return RuleLibraryStore(tmp_path, seed_builtin=False)


def test_builtin_complete_source_and_uninterpreted_images(tmp_path):
    store = RuleLibraryStore(tmp_path)
    library = store.seed_builtin()
    assert library["rule_count"] == 38
    assert library["active_rule_count"] == 35
    assert library["image_count"] == 46
    assert library["source_sha256"] == "655696b873ff2003c0a7eb5dae27611273508257d47e0623b04fe3ee368d6bdd"
    assert store.get_source(library["library_id"]) == BUILTIN_PATH.read_bytes()
    assert library["status"] == "source_only"
    assert all(r["readiness"] == "needs_interpretation" for r in library["rules"])
    assert len(library["workbook"]["sheets"][0]["merged_ranges"]) == 226
    image = library["workbook"]["sheets"][0]["images"][0]
    content, mime = store.get_image(library["library_id"], image["image_id"])
    assert content.startswith(b"\x89PNG") and mime == "image/png"
    assert image["read_status"] == "not_interpreted"
    assert len(store.list_libraries()) == 1


def test_builtin_preserves_changes_supplements_cancelled_steps_and_cells(tmp_path):
    store = RuleLibraryStore(tmp_path)
    library_id = store.list_libraries()[0]["library_id"]
    rule = store.get_rule(library_id, "AR034")
    cell = next(c for c in rule["source_cells"] if c["cell"] == "M149")
    assert "总厚度" in cell["value"]
    assert cell["merge"] == "M149:O153"
    assert cell["sheet"] == "建筑设计优化要点表"
    assert any(a["cell"] == "M149" for a in rule["source_annotations"])
    assert "[建筑设计优化要点表!M149]" in rule["rule_text"]
    ar23 = store.get_rule(library_id, "AR023")
    assert any(s["row"] == 108 and any(c["cell"] == "F108" for c in s["source_cells"]) for s in ar23["supplements"])
    assert store.get_rule(library_id, "AR003")["status"] == "disabled"
    ar10 = store.get_rule(library_id, "AR010")
    assert next(s for s in ar10["steps"] if s["step_id"] == "AR010-4")["status"] == "disabled"
    ar8 = store.get_rule(library_id, "AR008")
    assert any(c["cell"] == "D34" and "区分人防" in c["value"] for c in ar8["source_cells"])
    assert any(c["cell"] == "F34" and "不分人防" in c["value"] for c in ar8["source_cells"])


def test_project_import_persists_exact_source_and_stable_identity(store, tmp_path):
    source = simple()
    library = store.import_xlsx("custom.xlsx", source, "project-a")
    repeated = store.import_xlsx("custom.xlsx", source, "project-a")
    assert repeated == library
    reopened = RuleLibraryStore(tmp_path, seed_builtin=False)
    assert reopened.get_source(library["library_id"], "project-a") == source
    rule = reopened.get_rule(library["library_id"], "R001", "project-a")
    assert rule["qualified_ref"] == library["library_id"] + ":R001"
    assert rule["source_cells"][2]["cell"] == "C2"
    assert not reopened.list_libraries()
    assert len(reopened.list_libraries("project-a")) == 1


def test_versions_and_projects_are_isolated(store):
    old = store.import_xlsx("custom.xlsx", simple(), "a")
    new = store.import_xlsx("custom.xlsx", simple(text="New condition"), "a")
    other = store.import_xlsx("custom.xlsx", simple(), "b")
    assert len({x["library_id"] for x in (old, new, other)}) == 3
    assert old["library_key"] == new["library_key"]
    assert old["source_sha256"] != new["source_sha256"]
    for project in (None, "a"):
        with pytest.raises(RuleLibraryError, match="does not belong"):
            store.get_library(other["library_id"], project)
    assert len(store.list_libraries("a")) == 2
    assert len(store.list_libraries("b")) == 1
    store.import_xlsx("private.xlsx", simple(), "__no_project__")
    assert store.list_libraries() == []


def test_temporary_cannot_replace_builtin_by_filename(tmp_path):
    store = RuleLibraryStore(tmp_path)
    builtin = store.seed_builtin()
    imported = store.import_xlsx(builtin["filename"], simple("AR004"), "a")
    assert imported["scope"] == "project"
    assert imported["library_id"] != builtin["library_id"]
    assert store.get_rule(builtin["library_id"], "AR004")["source_sha256"] == builtin["source_sha256"]
    assert store.get_library(builtin["library_id"])["rule_count"] == 38


def test_effective_set_conflict_requires_explicit_replacement_and_is_immutable(store, tmp_path):
    old = store.import_xlsx("old.xlsx", simple(), "a")
    new = store.import_xlsx("new.xlsx", simple(text="New rule"), "a")
    selections = [{"library_id": old["library_id"]}, {"library_id": new["library_id"]}]
    with pytest.raises(RuleLibraryError) as error:
        store.create_effective_set("a", selections)
    assert error.value.code == "rule_conflict"
    assert store.list_effective_sets("a") == []
    override = {"op": "replace", "target_ref": old["rules"][0]["qualified_ref"], "replacement_ref": new["rules"][0]["qualified_ref"], "reason": "Project amendment"}
    snapshot = store.create_effective_set("a", selections, [override])
    assert snapshot["rules"][0]["qualified_ref"] == new["rules"][0]["qualified_ref"]
    assert snapshot["disabled"][0]["rule_ref"] == old["rules"][0]["qualified_ref"]
    assert len(snapshot["libraries"]) == 2
    assert store.create_effective_set("a", selections, [override]) == snapshot
    store.import_xlsx("new.xlsx", simple(text="Another revision"), "a")
    reopened = RuleLibraryStore(tmp_path, seed_builtin=False)
    assert reopened.get_effective_set(snapshot["effective_set_id"], "a") == snapshot
    with pytest.raises(RuleLibraryError) as error:
        reopened.get_effective_set(snapshot["effective_set_id"], "b")
    assert error.value.code == "project_mismatch"


def test_effective_set_add_disable_and_source_cancellation(store):
    source = workbook([["规则编号", "规则名称"], ["R001", "One"], ["R002", "Two"], ["R003（取消）", "Three"]])
    library = store.import_xlsx("test.xlsx", source, "a")
    extra = store.import_xlsx("extra.xlsx", simple("EXTRA"), "a")
    snapshot = store.create_effective_set("a", [{"library_id": library["library_id"]}], [
        {"op": "disable", "target_ref": library["rules"][0]["qualified_ref"]},
        {"op": "add", "rule_ref": extra["rules"][0]["qualified_ref"]},
    ])
    assert {r["rule_id"] for r in snapshot["rules"]} == {"R002", "EXTRA"}
    assert {r["reason"] for r in snapshot["disabled"]} == {"disabled_in_source", "disable"}
    with pytest.raises(RuleLibraryError) as error:
        store.create_effective_set("a", [{"library_id": library["library_id"]}], [{"op": "add", "rule_ref": library["rules"][2]["qualified_ref"]}])
    assert error.value.code == "disabled_rule"
    with pytest.raises(RuleLibraryError) as error:
        store.create_effective_set("a", [{"library_id": library["library_id"], "rule_ids": ["R003"]}])
    assert error.value.code == "empty_effective_set"


def test_effective_set_cannot_select_or_add_other_project_sources(store):
    own = store.import_xlsx("own.xlsx", simple("OWN"), "a")
    other = store.import_xlsx("other.xlsx", simple("OTHER"), "b")
    with pytest.raises(RuleLibraryError) as error:
        store.create_effective_set("a", [{"library_id": other["library_id"]}])
    assert error.value.code == "project_mismatch"
    with pytest.raises(RuleLibraryError) as error:
        store.create_effective_set("a", [{"library_id": own["library_id"]}], [{"op": "add", "rule_ref": other["rules"][0]["qualified_ref"]}])
    assert error.value.code == "project_mismatch"


@pytest.mark.parametrize("source,code", [
    (b"not zip", "invalid_xlsx"),
    (workbook([["Name", "Text"], ["Something", "A description"]]), "unrecognized_rulebook"),
    (workbook([["规则编号", "内容"], ["BAD ID", "Text"]]), "unrecognized_rulebook"),
    (workbook([["规则编号"], ["R001"], ["R001"]]), "duplicate_rule_ids"),
    (workbook([["规则编号"], ["R001"]], extra_parts={"../escape.xml": "bad"}), "invalid_xlsx"),
    (workbook([["规则编号"], ["R001"]], extra_parts={"xl/workbook.xml": '<!DOCTYPE x [<!ENTITY a "a">]><x/>'}), "unsafe_xml"),
])
def test_invalid_sources_do_not_create_successful_library(store, source, code):
    with pytest.raises(RuleLibraryError) as error:
        store.import_xlsx("bad.xlsx", source, "a")
    assert error.value.code == code
    assert store.list_libraries("a") == []


def test_size_and_expanded_limits_do_not_persist(tmp_path, monkeypatch):
    source = simple()
    store = RuleLibraryStore(tmp_path, seed_builtin=False, max_source_bytes=len(source) - 1)
    with pytest.raises(RuleLibraryError) as error:
        store.import_xlsx("large.xlsx", source, "a")
    assert error.value.code == "source_limit"
    store.max_source_bytes = len(source) + 1
    monkeypatch.setattr(parser, "MAX_EXPANDED_BYTES", 10)
    with pytest.raises(RuleLibraryError) as error:
        store.import_xlsx("large.xlsx", source, "a")
    assert error.value.code == "expanded_limit"
    assert store.list_libraries("a") == []


def test_preserves_formula_and_unrecognized_sheet_diagnostic(store):
    source = workbook([["规则编号", "内容"], ["R001", "Formula below"], [None, "Placeholder"]])
    input_zip = zipfile.ZipFile(io.BytesIO(source))
    parts = {name: input_zip.read(name) for name in input_zip.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b'<c r="B3" t="inlineStr"><is><t>Placeholder</t></is></c>', b'<c r="B3"><f>2+3</f><v>5</v></c>')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    library = store.import_xlsx("formula.xlsx", stream.getvalue(), "a")
    cell = next(c for c in library["rules"][0]["source_cells"] if c["cell"] == "B3")
    assert cell["formula"] == "2+3" and cell["value"] == "5"
    assert library["rules"][0]["readiness"] == "needs_interpretation"
    assert any(d["code"] == "formula_not_evaluated" for d in library["rules"][0]["diagnostics"])


def test_unrecognized_sheet_is_retained_and_diagnosed(store):
    source = workbook([["规则编号", "内容"], ["R001", "Text"]])
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(b"</sheets>", b'<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets>')
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(b"</Relationships>", b'<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
    parts["xl/worksheets/sheet2.xml"] = f'<worksheet xmlns="{parser.MAIN}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Additional author notes</t></is></c></row></sheetData></worksheet>'.encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    library = store.import_xlsx("notes.xlsx", stream.getvalue(), "a")
    assert library["rule_count"] == 1
    assert library["workbook"]["sheets"][1]["cells"]["A1"]["value"] == "Additional author notes"
    assert any(d["code"] == "unrecognized_sheet" and d["sheet"] == "Notes" for d in library["diagnostics"])


def test_zip_duplicate_parts_and_cell_limits_are_diagnostic(store, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("xl/workbook.xml", "<x/>")
        with pytest.warns(UserWarning):
            archive.writestr("xl/workbook.xml", "<x/>")
    with pytest.raises(RuleLibraryError) as error:
        store.import_xlsx("duplicate.xlsx", stream.getvalue(), "a")
    assert error.value.code == "invalid_xlsx"
    monkeypatch.setattr(parser, "MAX_CELLS", 1)
    with pytest.raises(RuleLibraryError) as error:
        store.import_xlsx("large.xlsx", simple(), "a")
    assert error.value.code == "cell_limit"
    assert not store.list_libraries("a")


@pytest.mark.parametrize("operation", [{"op": "disable", "target_ref": []}, {"op": "overwrite"}, {"op": "add", "rule_ref": None}])
def test_malformed_override_has_actionable_error(store, operation):
    library = store.import_xlsx("test.xlsx", simple(), "a")
    with pytest.raises(RuleLibraryError):
        store.create_effective_set("a", [{"library_id": library["library_id"]}], [operation])
    assert store.list_effective_sets("a") == []
