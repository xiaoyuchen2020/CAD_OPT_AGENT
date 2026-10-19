import pytest
from cad_agent.building.masonry import extract
from cad_agent.evidence import bind_evidence, validate_proposal
from conftest import region


def run(text):
    return extract("snap_test", [region(text)], "mm")


def test_protected_values_and_no_policy():
    facts, evidence, coverage, _, _ = run(
        "地上内墙采用B06级A3.5蒸压加气混凝土砌块，100或200厚，Ma5.0砂浆。"
    )
    a = facts[0].attributes
    assert a["density_grade"].value == "B06"
    assert a["strength_grade"].value == "A3.5"
    assert a["thickness_mm"].value == [100, 200]
    assert a["mortar_grade"].value == "Ma5.0"
    assert a["thermal_requirement"].state == "unknown"
    assert facts[0].review_status == "source_bound"
    assert evidence[0].bbox == [0, 0, 100, 30]
    assert evidence[0].precision == "entity"
    assert "optimization" not in facts[0].model_dump()


@pytest.mark.parametrize(
    "text,material",
    [
        ("地上内墙采用B05级A3.5蒸压加气混凝土砌块。", "蒸压加气混凝土砌块"),
        ("地下内墙采用MU20水泥实心砖。", "水泥实心砖"),
        ("地上卫生间内墙采用MU10烧结多孔砖。", "烧结多孔砖"),
        ("墙脚采用MU20混凝土实心砖。", "混凝土实心砖"),
    ],
)
def test_special_and_already_low_density_are_retained(text, material):
    facts, *_ = run(text)
    assert len(facts) == 1
    assert facts[0].attributes["material"].value == material


def test_missing_is_unknown_not_negative():
    f = run("内隔墙采用蒸压加气混凝土砌块。")[0][0]
    assert set(f.missing_fields) == {
        "elevation_scope",
        "strength_grade",
        "density_grade",
    }
    assert f.attributes["thermal_requirement"].value is None
    assert f.review_status == "needs_review"


def test_mixed_paragraph_and_exact_spans():
    text = "地上内墙采用B06级A3.5蒸压加气混凝土砌块；外墙采用MU10烧结多孔砖。"
    facts, evs, *_ = run(text)
    assert len(facts) == 2
    assert [f.attributes["wall_role"].value for f in facts] == ["inner", "outer"]
    for e in evs:
        assert text[e.text_span[0] : e.text_span[1]] == e.quote
    assert facts[1].attributes["elevation_scope"].state == "unknown"


def test_conflicting_grades_kept_for_review():
    f = run("地上内墙采用B06或B07级A3.5蒸压加气混凝土砌块。")[0][0]
    assert f.attributes["density_grade"].value == ["B06", "B07"]
    assert f.attributes["density_grade"].state == "conflict"
    assert "density_grade" in f.conflicts


def test_finishing_is_not_masonry():
    assert run("蒸压加气混凝土砌块专用界面剂一道。")[0] == []


def test_heading_inheritance_is_evidenced_and_ambiguous_headings_conflict():
    h = region("地上墙体材料", "heading")
    body = region(
        "内墙采用B06级A3.5蒸压加气混凝土砌块。", context_region_ids=["heading"]
    )
    f, ev, *_ = extract("snap_test", [h, body], "mm")
    assert f[0].attributes["elevation_scope"].state == "derived"
    assert len(ev) == 2
    h2 = region("地下墙体材料", "heading2")
    body.context_region_ids.append("heading2")
    f, *_ = extract("snap_test", [h, h2, body], "mm")
    assert f[0].attributes["elevation_scope"].state == "conflict"


def test_no_context_inheritance_across_frame():
    h = region("地上墙体材料", "heading", frame_id="other")
    body = region("内墙采用蒸压加气混凝土砌块。", context_region_ids=["heading"])
    f, *_ = extract("snap_test", [h, body], "mm")
    assert f[0].attributes["elevation_scope"].state == "unknown"


def test_remainder_subject_and_unresolved_scope():
    text = "内墙有水房间采用MU10烧结多孔砖；其余采用B06级A3.5蒸压加气混凝土砌块。"
    facts, evs, *_ = run(text)
    assert len(facts) == 2
    assert facts[1].attributes["wall_role"].value == "inner"
    assert facts[1].attributes["wall_role"].state == "derived"
    assert len(facts[1].evidence_ids) == 2
    unknown = run("其余采用B06级A3.5蒸压加气混凝土砌块。")[0][0]
    assert "unresolved_remainder_scope" in unknown.conflicts


def test_identity_includes_source_occurrence_and_snapshot():
    text = "地上内墙采用B06级A3.5蒸压加气混凝土砌块。"
    regions = [region(text, "a"), region(text, "b", frame_id="other")]
    f, ev, *_ = extract("snap1", regions, "mm")
    assert len({x.practice_id for x in f}) == 2
    assert len({e.evidence_id for e in ev}) == 2
    f2, *_ = extract("snap2", regions, "mm")
    assert f[0].practice_id != f2[0].practice_id


@pytest.mark.parametrize(
    "proposal",
    [
        {"region_id": "r1", "quote": "原文没有的B05砌块"},
        {"region_id": "wrong", "quote": "内墙"},
        {"region_id": "r1", "quote": ""},
        None,
    ],
)
def test_model_cannot_fabricate_source(proposal):
    r = region("内墙采用混凝土实心砖。")
    with pytest.raises(ValueError):
        validate_proposal(proposal, {"r1": r})
    f, ev, c, ds, _ = extract("snap", [r], "mm", [proposal])
    assert len(f) == 1
    assert any(d["code"] == "MODEL_UNGROUNDED_ITEM" for d in ds)
    assert c.completeness == "partial"


def test_repeated_quote_is_ambiguous():
    r = region("内墙采用混凝土实心砖；外墙采用混凝土实心砖。")
    with pytest.raises(ValueError):
        validate_proposal({"region_id": "r1", "quote": "混凝土实心砖"}, {"r1": r})


def test_model_partial_paragraph_cannot_hide_second_clause_or_scope():
    r = region("地上内墙采用B06级A3.5蒸压加气混凝土砌块；外墙采用MU10烧结多孔砖。")
    f, ev, c, ds, _ = extract(
        "snap", [r], "mm", [{"region_id": "r1", "quote": "蒸压加气混凝土砌块"}]
    )
    assert len(f) == 2
    assert f[0].attributes["density_grade"].value == "B06"
    assert f[0].attributes["wall_role"].value == "inner"
    assert ds[0]["code"] == "MODEL_OMITTED_CANDIDATES"


def test_exact_empty_result_is_bounded_not_error():
    f, ev, c, ds, _ = run("门窗工程另详门窗表。")
    assert f == ev == ds == []
    assert c.candidate_units == 0 and c.completeness == "bounded"


def test_evidence_rejects_invalid_span():
    with pytest.raises(ValueError):
        bind_evidence("s", region("abc"), 0, 20, "mm")


def test_fragment_separators_do_not_lose_thickness_or_grades():
    f = run("内墙采用 | 200 | 或 | 240 | 厚蒸压砂加气混凝土砌块（ | B06A3.5 | ）。")[0][
        0
    ]
    assert f.attributes["thickness_mm"].value == [200, 240]
    assert f.attributes["density_grade"].value == "B06"
    assert f.attributes["strength_grade"].value == "A3.5"
    assert " | " in f.original_text


@pytest.mark.parametrize(
    "text,kind",
    [
        ("地上内墙不得采用B06级A3.5蒸压加气混凝土砌块。", "prohibition"),
        ("构造要求详《蒸压加气混凝土砌块应用细则》。", "reference"),
        ("隔墙的隔声性能，蒸压加气混凝土砌块。", "material_mention"),
    ],
)
def test_mention_reference_and_prohibition_not_selected_material(text, kind):
    f = run(text)[0][0]
    assert f.attributes["statement_kind"].value == kind
    if kind != "prohibition":
        assert f.review_status == "needs_review"
