"""Geometry and source-bound regressions; fixtures are generic, not rule policies."""

import pytest
from cad_agent.contracts import Entity
from cad_agent.cad.regions import build_regions
from cad_agent.building.masonry import extract
from cad_agent.evidence import bind_evidence
from conftest import region


def assemble(lines, borders=True):
    items = []
    for i, (x, y, text) in enumerate(lines):
        items.append(
            Entity(
                entity_id=str(i),
                entity_path=[str(i)],
                layout_id="l",
                kind="TEXT",
                text=text,
                origin=[x, y],
                height=2,
                bbox=[x, y - 1, x + len(text) * 4, y + 2],
            )
        )
    scene = {"layouts": [{"layout_id": "l", "name": "Model", "bbox": [0, 0, 200, 100]}]}
    segments = (
        [{"layout_id": "l", "a": [x, 0], "b": [x, 100]} for x in [0, 90, 100, 190]]
        if borders
        else []
    )
    return build_regions(items, scene, [], segments, "s")[0], items


def test_fragments_soft_wrap_columns_and_exact_entity_span():
    rs, items = assemble(
        [
            (2, 90, "1、内墙采用"),
            (14, 90, "B06级"),
            (22, 90, "A3.5蒸压加气混凝土砌块，"),
            (2, 86, "采用DM-M5.0砂浆砌筑。"),
            (102, 90, "屋面防水工程"),
            (2, 82, "2、门窗工程："),
        ]
    )
    facts, evs, *_ = extract("s", rs, "mm")
    assert len(facts) == 1
    assert facts[0].attributes["mortar_grade"].value == "M5.0"
    assert "屋面" not in facts[0].original_text
    source = next(r for r in rs if "B06" in r.text)
    a = source.text.index("B06")
    e = bind_evidence("s", source, a, a + 3, "mm")
    assert e.entity_ids == ["1"] and e.bbox == items[1].bbox
    assert e.source_parts[0].entity_text_span == [0, len(items[1].text)]


def test_section_scope_stops_at_sibling_and_no_inferred_elevation():
    rs, _ = assemble(
        [
            (2, 94, "1.3、住宅建筑砌筑墙体材料："),
            (2, 90, "（1）内墙采用B06级A3.5蒸压加气混凝土砌块。"),
            (2, 84, "1.4、除住宅外的公建砌筑墙体材料："),
            (2, 80, "（1）外墙采用MU10混凝土多孔砖。"),
            (2, 74, "1.5、屋顶女儿墙采用混凝土实心砖。"),
        ]
    )
    facts, *_ = extract("s", rs, "mm")
    assert len(facts) == 3
    assert "住宅建筑" in facts[0].attributes["application_scope"].value
    assert "除住宅外" in facts[1].attributes["application_scope"].value
    assert facts[2].attributes["application_scope"].state == "unknown"
    assert all(f.attributes["elevation_scope"].state == "unknown" for f in facts)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("内墙采用MU20水泥实心砖，DM-M20砂浆砌筑，粉刷DP-M15砂浆15厚。", (None, "M20")),
        (
            "分户墙采用200厚B06级蒸压加气混凝土砌块+两侧15厚DPM15干混砂浆。",
            ([200], None),
        ),
        ("消火栓洞后砌筑100厚加气\n混凝土砌块；其它分隔墙内衬防火板。", ([100], None)),
    ],
)
def test_masonry_vs_finishing_and_split_material_word(text, expected):
    facts, *_ = extract("s", [region(text)], "mm")
    assert len(facts) == 1
    a = facts[0].attributes
    assert (a["thickness_mm"].value, a["mortar_grade"].value) == expected


def test_role_specific_parameters_and_material_alternative_are_not_flattened_silently():
    text = "地上墙体采用蒸压加气混凝土砌块或PC构件，外墙为240厚，内墙为200厚或\n100厚。"
    f = extract("s", [region(text)], "mm")[0][0]
    assert f.attributes["role_parameters"].value == {
        "outer": {"thickness_mm": [240]},
        "inner": {"thickness_mm": [100, 200]},
    }
    assert "material_alternatives_require_review" in f.conflicts
    assert "role_specific_values_require_review" in f.conflicts


def test_semicolon_new_concrete_subject_cannot_leak_into_brick_scope():
    text = "室外地坪标高以下采用MU20机制实心水泥砖，DMM7.5水泥砂浆砌筑；外墙均采用现浇钢筋混凝土；屋顶女儿墙采用现浇钢筋混凝土或蒸压砂加气混凝土砌块（B07级，强度\n等级不小于A5.0），采用DMM5.0专用粘结剂砌筑。"
    facts, evs, *_ = extract("s", [region(text)], "mm")
    assert len(facts) == 2
    assert facts[0].attributes["wall_role"].state == "unknown"
    assert facts[1].attributes["elevation_scope"].state == "unknown"
    assert facts[1].attributes["strength_grade"].value == "A5.0"
    assert facts[1].attributes["mortar_grade"].value == "M5.0"


def test_generic_repeated_block_mentions_are_not_dropped_and_page_numbers_not_strength():
    fs = extract("s", [region("填充墙砌块应留缝，每块砌块两侧分别填实。")], "mm")[0]
    assert len(fs) == 1 and fs[0].attributes["material"].value == "砌块"
    f = extract(
        "s", [region("参照《蒸压加气混凝土砌块构造》13J104第A58页-A60页。")], "mm"
    )[0][0]
    assert f.attributes["strength_grade"].state == "unknown"


def test_table_cell_alternatives_do_not_mix_and_ruled_notes_are_not_tables():
    scene = {"layouts": [{"layout_id": "l", "name": "Model", "bbox": [0, 0, 100, 100]}]}

    def source(x, y, text, i):
        return Entity(
            entity_id=i,
            entity_path=[i],
            layout_id="l",
            kind="TEXT",
            text=text,
            origin=[x, y],
            height=2,
            bbox=[x, y - 1, x + len(text) * 4, y + 2],
        )

    items = [
        source(2, 8, "分户墙", "label"),
        source(32, 8, "200厚B06级蒸压加气混凝土砌块+两侧15厚DPM15砂浆", "brick"),
        source(32, 4, "200厚钢筋混凝土+两侧15厚砂浆", "concrete"),
    ]
    segments = [{"layout_id": "l", "a": [0, y], "b": [100, y]} for y in [0, 12]] + [
        {"layout_id": "l", "a": [x, 0], "b": [x, 12]} for x in [0, 30, 100]
    ]
    rs, _ = build_regions(items, scene, [], segments, "s")
    facts, evs, *_ = extract("s", rs, "mm")
    assert len(facts) == 1
    assert facts[0].attributes["thickness_mm"].value == [200]
    assert facts[0].attributes["wall_role"].value == "inner"
    assert facts[0].attributes["special_uses"].value == ["分户墙"]
    assert facts[0].attributes["mortar_grade"].value is None
    assert not any("concrete" in e.entity_ids for e in evs)
    assert any(e.entity_ids == ["label"] for e in evs)
    guides = [{"layout_id": "l", "a": [0, y], "b": [100, y]} for y in [0, 12]] + [
        {"layout_id": "l", "a": [x, 0], "b": [x, 100]} for x in [0, 2, 4, 100]
    ]
    rs, _ = build_regions(
        [source(5, 8, "内墙采用砌块。", "body")], scene, [], guides, "s"
    )
    assert rs[0].kind == "paragraph"


def test_queries_use_attributes_not_finishing_words_and_honor_heading_exclusions():
    from cad_agent.analysis.query import answer_facts
    from cad_agent.contracts import ChatRequest

    a = region("住宅建筑砌筑墙体材料：", "residential")
    b = region("除住宅外的公建砌筑墙体材料：", "public")
    x = region(
        "内墙采用MU20水泥实心砖，DM-M20砂浆，粉刷DP-M15砂浆。",
        "x",
        context_region_ids=["residential"],
    )
    y = region("内墙采用MU10混凝土多孔砖。", "y", context_region_ids=["public"])
    fs = extract("s", [a, b, x, y], "mm")[0]
    facts = {"practices": [f.model_dump() for f in fs]}

    def ask(q):
        return answer_facts(facts, ChatRequest(snapshot_id="s", question=q))[
            "citations"
        ]

    assert len(ask("住宅内墙材料")) == 1
    assert len(ask("公建内墙材料")) == 1
    assert len(ask("M15砂浆材料")) == 0
    assert len(ask("M20砂浆材料")) == 1


def test_mixed_role_query_does_not_apply_inner_thickness_to_outer_wall():
    from cad_agent.analysis.query import answer_facts
    from cad_agent.contracts import ChatRequest

    f = extract(
        "s",
        [region("地上墙体采用蒸压加气混凝土砌块，外墙为240厚，内墙为200厚或100厚。")],
        "mm",
    )[0][0]
    assert f.attributes["thickness_mm"].state == "conflict"
    data = {"practices": [f.model_dump()]}
    assert not answer_facts(
        data, ChatRequest(snapshot_id="s", question="外墙100厚材料")
    )["citations"]
    assert (
        len(
            answer_facts(data, ChatRequest(snapshot_id="s", question="内墙100厚材料"))[
                "citations"
            ]
        )
        == 1
    )
