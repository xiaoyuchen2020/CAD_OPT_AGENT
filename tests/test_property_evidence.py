"""Field identity, source-column lineage and measurement-basis regressions."""
import json
from copy import deepcopy

import pytest

from cad_agent.analysis.property_evidence import (
    collect_property_evidence, property_claim_issues, query_task_property_evidence,
)
from cad_agent.analysis.tool_registry import Claim
from cad_agent.contracts import PropertyAssertion
from test_semantics import semantic_table


def ledger():
    evidence = {}
    def ev(key, quote):
        evidence[key] = dict(evidence_id=key, snapshot_id="snap", region_id=key,
                             quote=quote, text_span=[0, len(quote)], entity_ids=[key])
        return evidence[key]
    code, width, height = ev("code", "M101"), ev("width", "900"), ev("height", "2100")
    hc, hw, hh = ev("hc", "编号"), ev("hw", "宽度(mm)"), ev("hh", "高度(mm)")
    cells, fields = [], []
    for i, (value, header) in enumerate(((code, hc), (width, hw), (height, hh))):
        item = dict(cell_id=value["evidence_id"], text=value["quote"],
                    header_path=[header["quote"]], header_cell_ids=[header["evidence_id"]],
                    evidence=[value], header_evidence=[header], unit_evidence=[header] if i else [],
                    unit="mm" if i else None, unit_state="derived" if i else "unknown",
                    row_span=1, column_span=1, continuation_of=None, column_index=i,
                    numeric_value=float(value["quote"]) if i else None)
        cells.append(item); fields.append(deepcopy(item))
    data = dict(snapshot_id="snap", table_id="table", layout_id="layout",
                items=[dict(row_index=1, cells=cells)],
                records=[dict(row_index=1, fields=fields,
                              binding=dict(state="source_bound", identity_cell_ids=["code"]))])
    observations = {"table_obs": dict(observation_id="table_obs", snapshot_id="snap",
                                      tool="read_table_semantics", data=data)}
    return dict(snapshot_id="snap", evidence=evidence, observations=observations)


def width_field(task):
    return next(f for f in query_task_property_evidence(task)["items"] if f["property_name"] == "width")


def source_claim(field, **kwargs):
    refs = [r["ref_id"] for r in field["source_refs"] if r["kind"] == "evidence"]
    assertion = PropertyAssertion(**{k: field[k] for k in PropertyAssertion.model_fields})
    return Claim(text="M101宽度900mm", state="observed", evidence_ids=refs,
                 property_assertions=[assertion], **kwargs)


def test_table_field_reuses_exact_subject_header_value_and_unit_sources():
    task = ledger()
    original = deepcopy(task)
    field = width_field(task)
    assert field["value"] == 900 and field["unit"] == "mm"
    assert field["basis"] == "source_nominal"
    assert field["object_instance_state"] == "unverified"
    assert field["binding_state"] == "source_bound"
    assert {r["role"] for r in field["source_refs"]} >= {"subject", "property", "value", "unit"}
    fields, _ = collect_property_evidence(task["evidence"], task["observations"], {"snap"})
    assert not property_claim_issues(source_claim(field), fields)
    assert task == original


@pytest.mark.parametrize("key,value", [
    ("subject_id", "another_door"), ("property_name", "height"), ("basis", "measurement"),
    ("unit", "m"), ("value", 2100),
])
def test_typed_binding_rejects_wrong_object_column_basis_unit_and_value(key, value):
    task = ledger(); field = width_field(task)
    claim = source_claim(field)
    setattr(claim.property_assertions[0], key, value)
    fields, _ = collect_property_evidence(task["evidence"], task["observations"], {"snap"})
    assert any(key in issue for issue in property_claim_issues(claim, fields))


@pytest.mark.parametrize("text,refs", [
    ("宽度2100mm", ["code", "hh", "height"]),
    ("宽度900m", ["code", "hw", "width"]),
    ("高度900mm", ["code", "hw", "width"]),
])
def test_legacy_text_cannot_borrow_number_from_another_column_or_unit(text, refs):
    task = ledger()
    fields, _ = collect_property_evidence(task["evidence"], task["observations"], {"snap"})
    claim = Claim(text=text, state="observed", evidence_ids=refs)
    assert property_claim_issues(claim, fields)


def test_missing_or_foreign_unit_header_does_not_use_drawing_unit():
    task = ledger()
    task["evidence"]["hw"]["snapshot_id"] = "other"
    field = width_field(task)
    assert field["unit"] is None and field["state"] == "unknown"
    assert "SOURCE_REF_UNREAD_OR_MISMATCH" in field["diagnostics"]
    assert property_claim_issues(source_claim(field), {field["field_id"]: field})


def test_projected_unit_must_match_actual_header_declaration():
    task = ledger()
    task["observations"]["table_obs"]["data"]["records"][0]["fields"][1]["unit"] = "m"
    field = width_field(task)
    assert field["unit"] is None and field["state"] == "unknown"
    assert "UNIT_SOURCE_MISMATCH" in field["diagnostics"]


def test_unknown_blanks_are_not_filled_and_same_code_rows_are_not_joined():
    task = ledger()
    data = task["observations"]["table_obs"]["data"]
    second = deepcopy(data["records"][0]); second["row_index"] = 2
    blank = second["fields"][1]
    blank.update(text="", evidence=[], unit=None, unit_state="unknown", cell_id="blank")
    data["records"].append(second)
    result = query_task_property_evidence(task, property_name="width")
    assert len(result["items"]) == 2
    assert len({f["subject_id"] for f in result["items"]}) == 2
    assert any(f["value"] is None and f["state"] == "unknown" for f in result["items"])


def test_cross_snapshot_observations_and_payload_mismatch_are_excluded():
    task = ledger()
    task["observations"]["foreign"] = deepcopy(task["observations"]["table_obs"])
    task["observations"]["foreign"]["snapshot_id"] = "foreign"
    fields, diagnostics = collect_property_evidence(task["evidence"], task["observations"], {"snap"})
    assert len(fields) == 2 and diagnostics[0]["code"] == "PROPERTY_SNAPSHOT_MISMATCH"
    task["observations"]["table_obs"]["data"]["snapshot_id"] = "foreign"
    assert not collect_property_evidence(task["evidence"], task["observations"], {"snap"})[0]


def test_related_door_schedule_keeps_actual_authorized_snapshot():
    task = ledger()
    task["snapshot_id"] = "primary"
    task["related_drawings"] = [dict(snapshot_id="snap")]
    task["observations"]["table_obs"]["tool"] = "read_door_schedule_table"
    result = query_task_property_evidence(task)
    assert result["total"] == 2
    assert {f["snapshot_id"] for f in result["items"]} == {"snap"}
    assert query_task_property_evidence(task, {"primary"})["total"] == 0


def test_native_annotation_900_and_geometry_920_are_parallel_not_overwritten():
    task = ledger()
    task["observations"]["dim"] = dict(observation_id="dim", snapshot_id="snap", tool="read_dimensions",
        data=dict(kind="dimensions", layout_id="layout", unit="mm", items=[
            dict(object_id="dimension1", kind="dimension", text_override="900mm", text_state="explicit_override",
                 actual_measurement=920, geometry_recomputed=920, measurement_unit="drawing_units",
                 geometry_length_m=.92, target_binding=dict(confirmed_entity_ids=[]))]))
    result = query_task_property_evidence(task)
    assert any(f["basis"] == "source_nominal" and f["value"] == 900 for f in result["items"])
    dim = [f for f in result["items"] if f["subject_id"] == "dimension1"]
    assert len(dim) == 4
    assert any(f["basis"] == "annotation" and f["value"] == "900" and f["unit"] == "mm" for f in dim)
    assert any(f["basis"] == "measurement" and f["value"] == 920 for f in dim)
    assert all(f["binding_state"] == "candidate" for f in dim)
    assert {f["subject_id"] for f in result["items"] if f["basis"] == "source_nominal"} != {"dimension1"}


def test_rendered_dimension_conflict_is_preserved_as_annotation():
    observations = {"d": dict(observation_id="d", snapshot_id="snap", tool="read_dimensions",
        data=dict(kind="dimensions", items=[dict(kind="dimension", object_id="d1", status="conflict",
            rendered_texts=["900"], actual_measurement=920, measurement_unit="drawing_units",
            diagnostics=["measurement_channels_disagree"])]))}
    fields, _ = collect_property_evidence({}, observations, {"snap"})
    annotations = [f for f in fields.values() if f["basis"] == "annotation"]
    assert annotations[0]["value"] == "900" and annotations[0]["unit"] is None
    assert annotations[0]["state"] == "conflict"


def test_same_object_same_basis_conflict_is_retained_not_last_write_wins():
    task = ledger()
    for oid, value in (("m1", 900), ("m2", 920)):
        task["observations"][oid] = dict(observation_id=oid, snapshot_id="snap", tool="measure_spatial_sections",
            data=dict(object_id="opening", layout_id="layout", status="source_geometry_sampled",
                      measurement_basis="source_opposing_faces", unit=dict(unit="mm"),
                      measurements=dict(width_drawing_units=value)))
    fields, _ = collect_property_evidence(task["evidence"], task["observations"], {"snap"})
    measured = [f for f in fields.values() if f["subject_id"] == "opening"]
    assert {f["value"] for f in measured} == {900, 920}
    assert all(f["state"] == "conflict" and f["conflicting_field_ids"] for f in measured)
    claim = Claim(text="洞口宽度900mm", state="derived", evidence_basis="tool_observation",
                  observation_ids=["m1"], property_evidence_ids=[measured[0]["field_id"]])
    assert any("冲突" in issue for issue in property_claim_issues(claim, fields))


@pytest.mark.parametrize("text,blocked", [
    ("完成面净宽920mm", True), ("门洞净宽920mm", True),
    ("几何洞口宽度920mm，不等于完成面净宽。", False),
    ("采样宽度920mm，完成面净宽未确认。", False),
])
def test_geometry_does_not_establish_finished_clear_width(text, blocked):
    task = dict(snapshot_id="snap", evidence={}, observations={
        "m": dict(observation_id="m", snapshot_id="snap", tool="measure_spatial_sections",
                  data=dict(object_id="opening", unit=dict(unit="mm"),
                            measurements=dict(width_drawing_units=920)))})
    fields, _ = collect_property_evidence(task["evidence"], task["observations"], {"snap"})
    claim = Claim(text=text, state="derived", evidence_basis="tool_observation", observation_ids=["m"])
    assert bool(property_claim_issues(claim, fields)) is blocked


def test_query_filters_and_pagination_do_not_mutate_task_or_return_unrelated_sources():
    task = ledger()
    page = query_task_property_evidence(task, limit=1)
    assert page["total"] == 2 and page["has_more"] and page["next_offset"] == 1
    assert query_task_property_evidence(task, offset=1, limit=1)["next_offset"] is None
    assert query_task_property_evidence(task, subject_id="not-read")["total"] == 0
    with pytest.raises(ValueError):
        query_task_property_evidence(task, limit=101)


def test_different_sample_stations_are_not_conflicting_widths():
    observations = {"m": dict(observation_id="m", snapshot_id="snap", tool="measure_spatial_sections",
        data=dict(object_id="opening", unit=dict(unit="mm"), measurements=dict(samples=[
            dict(station_drawing_units=100, width_drawing_units=900),
            dict(station_drawing_units=200, width_drawing_units=920)])))}
    fields, _ = collect_property_evidence({}, observations, {"snap"})
    widths = [f for f in fields.values() if f["property_name"] == "width"]
    assert {f["value"] for f in widths} == {900, 920}
    assert all(f["comparison_state"] == "uncompared" for f in widths)
    assert {f["scope"]["measurement_context"]["station_drawing_units"] for f in widths} == {100, 200}


def test_annotation_difference_does_not_become_geometry_measurement():
    observations = {"m": dict(observation_id="m", snapshot_id="snap", tool="measure_elevation_relation",
        data=dict(object_id="labels", unit=dict(unit="mm"), measurement_basis="source_annotation_difference",
                  measurements=dict(lower_elevation_m=0, upper_elevation_m=3,
                                    elevation_difference_m=3, conditional=True)))}
    fields, _ = collect_property_evidence({}, observations, {"snap"})
    by_property = {f["property_name"]: f for f in fields.values()}
    assert by_property["lower_elevation"]["basis"] == "annotation"
    assert by_property["upper_elevation"]["basis"] == "annotation"
    assert by_property["elevation_difference"]["basis"] == "calculation"
    assert all(f["unit_state"] == "conditional" for f in fields.values())


def test_existing_measurement_collection_retains_source_object_and_basis():
    observations = {"a": dict(observation_id="a", snapshot_id="snap", tool="analyze_measurements",
        data=dict(batch_id="b", values=[dict(measurement_ref="v1", object_id="room1",
            field="area", value=12.5, unit="m2", basis="selected_enclosure", batch_id="b")]))}
    fields, _ = collect_property_evidence({}, observations, {"snap"})
    field = next(iter(fields.values()))
    assert field["subject_id"] == "room1" and field["measurement_basis"] == "selected_enclosure"
    assert field["scope"]["measurement_ref"] == "v1"
    assert field["source_refs"][0]["path"] == ["values", 0, "value"]


def test_typed_conditional_unit_cannot_become_unconditional_fact():
    observations = {"m": dict(observation_id="m", snapshot_id="snap", tool="measure_geometry",
        data=dict(measurement_id="m1", operation="length", status="measured",
                  value=2, value_unit="m", unit_status="assumption"))}
    fields, _ = collect_property_evidence({}, observations, {"snap"})
    field = next(iter(fields.values()))
    claim = Claim(text="长度2m", state="derived", evidence_basis="tool_observation",
                  observation_ids=["m"], property_evidence_ids=[field["field_id"]])
    assert any("条件" in issue for issue in property_claim_issues(claim, fields))
    claim.text = "按用户给定单位条件，长度2m。"
    assert not property_claim_issues(claim, fields)


def test_real_semantic_table_projection_retains_merged_identity_and_empty_value(semantic_table):
    from cad_agent.semantics.contracts import SemanticTable
    drawing, service, table = semantic_table
    data = service.table(SemanticTable(table_id=table["table_id"], row_limit=5))
    evidence = {}
    for row in data["items"]:
        for cell in row["cells"]:
            for key in ("evidence", "header_evidence", "unit_evidence"):
                evidence.update({e["evidence_id"]: e for e in cell.get(key, [])})
    fields, diagnostics = collect_property_evidence(evidence, {
        "o": dict(observation_id="o", snapshot_id=drawing["id"], tool="read_table_semantics", data=data)
    }, {drawing["id"]})
    assert not diagnostics
    widths = [f for f in fields.values() if f["property_name"] == "width"]
    assert len(widths) == 2
    assert any(f["value"] == 900 and f["unit"] == "mm" and not f["diagnostics"] for f in widths)
    assert any(f["value"] is None and f["state"] == "unknown" for f in widths)


def test_delivery_publishes_field_view_and_rejects_typed_wrong_unit(workspace, demo_bytes):
    from test_siwc import make_session
    session = make_session(workspace, demo_bytes)
    task = ledger()
    sid = session.request.snapshot_id
    for e in task["evidence"].values():
        e["snapshot_id"] = sid
    task["observations"]["table_obs"]["snapshot_id"] = sid
    task["observations"]["table_obs"]["data"]["snapshot_id"] = sid
    task["snapshot_id"] = sid
    session.evidence = task["evidence"]; session.observations = task["observations"]
    field = width_field(task)
    claim = source_claim(field)
    result = session.validate_answer(json.dumps(dict(claims=[claim.model_dump()])))
    assert result["property_evidence"]
    claim.property_assertions[0].unit = "m"
    with pytest.raises(ValueError, match="unit"):
        session.validate_answer(json.dumps(dict(claims=[claim.model_dump()])))
