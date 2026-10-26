"""Typed, read-only field views over the existing evidence/observation ledger.

No matching by name, fresh CAD parsing, implicit unit conversion, or credit for
unread archived results. Producer paths and source roles remain inspectable.
"""
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from cad_agent.contracts import PropertyEvidence, PropertySourceRef, stable_id


ALIASES = {"宽度": "width", "宽": "width", "高度": "height", "高": "height",
           "厚度": "thickness", "厚": "thickness", "长度": "length", "面积": "area",
           "数量": "count", "完成面净宽": "finished_clear_width", "净宽": "clear_width"}
UNITS = {"毫米": "mm", "厘米": "cm", "米": "m", "平方米": "m2", "㎡": "m2",
         "m²": "m2", "mm²": "mm2", "cm²": "cm2"}
MEASUREMENT_TOOLS = {"measure_geometry", "measure_room", "measure_room_area",
                     "get_measurement_evidence", "measure_spatial_area",
                     "measure_spatial_relation", "measure_spatial_sections",
                     "measure_elevation_relation", "inspect_door_sweep",
                     "measure_door_sweep_relation"}
NOTE = "仅投影本任务已登记字段；表行、标注和几何对象不按名称合并。口径与单位分别保留，字段绑定不确认工程身份、完成面净宽或全图完整性。"


def _unit(value):
    return UNITS.get(value, value) if isinstance(value, str) and value not in ("", "unknown") else None


def _scalar(value):
    return value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _equal(a, b):
    if a is None or b is None:
        return a is b
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return type(a) is type(b) and a == b


def _property(label):
    label = re.sub(r"[（(][^）)]*[）)]", "", label).strip()
    return ALIASES.get(label, label)


def _scope(data, row=None):
    result = {k: data[k] for k in ("scope_id", "layout_id", "frame_id", "source_sha256",
                                  "contour_id", "other_object_id", "exclusion_contour_ids") if data.get(k)}
    if row:
        result.update({k: row[k] for k in ("scope_id", "layout_id", "frame_id") if row.get(k)})
    return result


def _eref(evidence, refs, role, sid, diagnostics):
    result = []
    for ref in refs:
        eid = ref.get("evidence_id")
        source = evidence.get(eid)
        if (not source or source.get("snapshot_id") != sid
                or source.get("quote") != ref.get("quote")):
            diagnostics.append("SOURCE_REF_UNREAD_OR_MISMATCH")
            continue
        result.append(PropertySourceRef(kind="evidence", ref_id=eid, role=role))
    return result


def _field(oid, sid, subject, kind, prop, label, value, basis, path, data, **kwargs):
    # Source path distinguishes cells, nested measurements and annotation channels.
    return PropertyEvidence(
        field_id=stable_id("property", sid, oid, json.dumps(path), basis),
        snapshot_id=sid, subject_id=subject, subject_kind=kind,
        property_name=prop, property_label=label, value=value, basis=basis,
        scope=_scope(data),
        source_refs=[PropertySourceRef(kind="observation", ref_id=oid, role="value", path=path)],
        **kwargs)


def _table_fields(oid, sid, data, evidence):
    cells = {c["cell_id"]: c for row in data.get("items", []) for c in row.get("cells", [])}
    for ri, record in enumerate(data.get("records", [])):
        fields = record.get("fields", [])
        binding = record.get("binding", {})
        identity_ids = binding.get("identity_cell_ids", record.get("identity_cell_ids", []))
        identity = [f for f in fields if f.get("cell_id") in identity_ids]
        label = "；".join(f.get("text", "") for f in identity)
        subject = stable_id("table-row", sid, data["table_id"], str(record["row_index"]))
        for fi, field in enumerate(fields):
            headers = field.get("header_path", [])
            if not headers or field.get("cell_id") in identity_ids:
                continue
            cell = cells.get(field["cell_id"], {})
            diagnostics = []
            refs = (_eref(evidence, field.get("evidence", []), "value", sid, diagnostics)
                    + _eref(evidence, field.get("header_evidence", []), "property", sid, diagnostics)
                    + _eref(evidence, cell.get("unit_evidence", []), "unit", sid, diagnostics)
                    + _eref(evidence, [e for f in identity for e in f.get("evidence", [])], "subject", sid, diagnostics))
            for role in ("value", "property", "subject"):
                if not any(r.role == role for r in refs):
                    diagnostics.append("MISSING_" + role.upper() + "_SOURCE")
            # Source-bound IDs alone do not license a changed cell/header payload.
            value_quotes = "\n".join(evidence[r.ref_id]["quote"] for r in refs if r.role == "value")
            header_quotes = "\n".join(evidence[r.ref_id]["quote"] for r in refs if r.role == "property")
            if field.get("text", "").strip() and field["text"].strip() not in value_quotes:
                diagnostics.append("CELL_TEXT_SOURCE_MISMATCH")
            if any(h not in header_quotes for h in headers):
                diagnostics.append("HEADER_TEXT_SOURCE_MISMATCH")
            unit = _unit(field.get("unit"))
            unit_state = "conflict" if field.get("unit_state") == "conflict" else "unknown"
            if unit and any(r.role == "unit" for r in refs):
                from cad_agent.semantics.tables import explicit_units
                declared = {u for r in refs if r.role == "unit" for u in explicit_units(evidence[r.ref_id]["quote"])}
                if unit not in declared:
                    diagnostics.append("UNIT_SOURCE_MISMATCH")
                    unit = None
                elif len(declared) > 1:
                    unit, unit_state = None, "conflict"
                else:
                    unit_state = "explicit"
            elif unit:
                diagnostics.append("MISSING_UNIT_SOURCE")
                unit = None
            value = cell.get("numeric_value")
            if value is None:
                value = field.get("text") or None
            # Numeric values are usable only when their own source cell contains them.
            from cad_agent.analysis.grounding import contains_number
            if isinstance(value, (int, float)) and not contains_number(value_quotes, str(value)):
                diagnostics.append("CELL_VALUE_SOURCE_MISMATCH")
            state = "unknown" if value is None or diagnostics else "conflict" if unit_state == "conflict" else "derived"
            out = _field(oid, sid, subject, "source_record", _property(headers[-1]), headers[-1],
                         value, "source_nominal", ["records", ri, "fields", fi, "text"], data,
                         subject_label=label, unit=unit, unit_state=unit_state, state=state,
                         binding_state="source_bound" if binding.get("state") == "source_bound" and not diagnostics else "candidate",
                         diagnostics=sorted(set(diagnostics)),
                         measurement_basis="explicit_header_and_source_grid")
            out.source_refs.extend(refs)
            out.scope.update(table_id=data["table_id"], row_index=record["row_index"],
                             cell_id=field["cell_id"], header_cell_ids=field.get("header_cell_ids", []),
                             column_index=cell.get("column_index"), row_span=field.get("row_span", 1),
                             column_span=field.get("column_span", 1), continuation_of=field.get("continuation_of"))
            yield out


def _dimension_fields(oid, sid, data):
    rows = data.get("items", data.get("dimensions", []))
    key = "items" if "items" in data else "dimensions"
    for i, row in enumerate(rows):
        if row.get("kind") != "dimension":
            continue
        subject = row["object_id"]  # dimension entity, never a nearby wall/door
        channels = [(name, basis, unit, row.get(name), [key, i, name]) for name, basis, unit in (
            ("text_override", "annotation", None),
            ("actual_measurement", "measurement", _unit(row.get("measurement_unit"))),
            ("geometry_recomputed", "measurement", _unit(row.get("measurement_unit"))),
            ("geometry_length_m", "measurement", "m"))]
        channels.extend(("rendered_texts", "annotation", None, text, [key, i, "rendered_texts", j])
                        for j, text in enumerate(row.get("rendered_texts", [])))
        for name, basis, unit, value, path in channels:
            if value is None or name == "text_override" and row.get("text_state") != "explicit_override":
                continue
            if basis == "annotation":
                parsed = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|m|毫米|厘米|米)?\s*", value)
                if parsed:
                    value, unit = parsed[1], _unit(parsed[2])
            out = _field(oid, sid, subject, "dimension", "dimension_value", name, value, basis,
                         path, data, unit=unit,
                         unit_state="conditional" if data.get("unit_status") == "assumption" and name == "geometry_length_m"
                         else "explicit" if unit else "unknown",
                         state="conflict" if row.get("status") == "conflict" else "derived",
                         binding_state="candidate", measurement_basis=name,
                         diagnostics=["DIMENSION_TARGET_UNCONFIRMED"] + [
                             d for d in row.get("diagnostics", []) if isinstance(d, str)])
            out.scope.update(_scope(data, row))
            yield out


def _measurement_fields(oid, sid, data):
    # Explicit numeric containers only; bbox/coordinates/IDs never become properties.
    subject = data.get("object_id") or data.get("candidate_id") or data.get("measurement_id")
    if not subject:
        return
    unit_info = data.get("unit")
    raw_unit = _unit(unit_info.get("unit", unit_info.get("value")) if isinstance(unit_info, dict)
                     else data.get("applied_unit", unit_info))
    conditional = (data.get("conditional") or data.get("unit_status") == "assumption"
                   or isinstance(unit_info, dict) and unit_info.get("unit_status") == "assumption"
                   or isinstance(data.get("measurements"), dict) and data["measurements"].get("conditional") is True)
    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    yield from walk(value, path + [key])
                elif _scalar(value) and (value is None or isinstance(value, (int, float))):
                    unit, prop = None, key
                    for suffix, candidate in (("_drawing_units_squared", raw_unit + "2" if raw_unit else None),
                                               ("_drawing_units", raw_unit or "drawing_units"),
                                               ("_m2", "m2"), ("_m", "m"), ("_degrees", "degrees")):
                        if key.endswith(suffix):
                            prop, unit = key[:-len(suffix)], candidate
                            break
                    # Untyped flags, indices and counts in method metadata are not measures.
                    if unit is None:
                        continue
                    yield key, prop, value, unit, path + [key]
        elif isinstance(node, list):
            for i, item in enumerate(node):
                yield from walk(item, path + [i])
    scalars = list(walk(data.get("measurements", {}), ["measurements"]))
    for key in ("value", "area_m2", "candidate_area_m2", "basis_area_m2", "perimeter_m"):
        if key not in data or not _scalar(data[key]):
            continue
        unit = _unit(data.get("value_unit")) if key == "value" else "m" if key.endswith("_m") else "m2"
        prop = data.get("operation", "value") if key == "value" else key.rsplit("_", 1)[0]
        scalars.append((key, prop, data[key], unit, [key]))
    for key, prop, value, unit, path in scalars:
        unresolved = data.get("status") in ("unresolved", "unknown", "failed", "needs_unit_confirmation") or value is None
        basis = ("annotation" if prop in ("lower_elevation", "upper_elevation") else
                 "calculation" if prop == "elevation_difference" else "measurement")
        field = _field(oid, sid, subject, "geometry_object", prop, key, value, basis, path, data,
                     unit=unit, unit_state="conditional" if conditional else "explicit" if unit else "unknown",
                     state="unknown" if unresolved else "derived", binding_state="candidate",
                     measurement_basis=str(data.get("measurement_basis", data.get("basis", data.get("kind", "")))),
                     diagnostics=["ENGINEERING_MEASUREMENT_BASIS_UNCONFIRMED"])
        # Different stations/sections are distinct measurements, not conflicting
        # values for one field. Retain the actual producer context, not just index.
        if len(path) > 2:
            node = data
            for segment in path[:-1]:
                node = node[segment]
            field.scope["measurement_context"] = {
                "path": path[:-1],
                **{k: v for k, v in node.items() if k in ("station_drawing_units", "station_m", "section_id", "sample_id")},
            }
        yield field


def _business_fields(oid, sid, data, evidence):
    from cad_agent.analysis.business_tables import clean, field_key, units_in, parse_value, ALIASES
    groups = data.get('records', []) if data.get('kind')=='building_schedule' else [dict(fields=data.get('fields',[]))]
    for ri, record in enumerate(groups):
        for fi, field in enumerate(record['fields']):
            diagnostics=list(field.get('diagnostics',[]))
            refs=(_eref(evidence,field['evidence'],'value',sid,diagnostics) +
                  _eref(evidence,field['header_evidence']+field['label_evidence'],'property',sid,diagnostics) +
                  _eref(evidence,field['unit_evidence'],'unit',sid,diagnostics))
            identity=([e for i in record.get('identity',[]) for e in i['evidence']] or
                      field['label_evidence'] or field.get('scope_evidence',[]) or field['header_evidence'])
            refs+=_eref(evidence,identity,'subject',sid,diagnostics)
            for role in ('value','property','subject'):
                if not any(r.role==role for r in refs): diagnostics.append('MISSING_'+role.upper()+'_SOURCE')
            quotes='\n'.join(evidence[r.ref_id]['quote'] for r in refs if r.role=='value')
            if clean(field['raw_text']) not in clean(quotes): diagnostics.append('CELL_TEXT_SOURCE_MISMATCH')
            parsed,qualifier,_=parse_value(field['raw_text'],field['field'])
            if parsed!=field['value'] or qualifier!=field['qualifier']: diagnostics.append('CELL_VALUE_SOURCE_MISMATCH')
            labels=field['source_labels'] or field['header_path']
            if field_key(labels,ALIASES)!=field['field']: diagnostics.append('BUSINESS_FIELD_SOURCE_MISMATCH')
            property_quotes=clean('\n'.join(evidence[r.ref_id]['quote'] for r in refs if r.role=='property'))
            if any(clean(h) not in property_quotes for h in labels): diagnostics.append('HEADER_TEXT_SOURCE_MISMATCH')
            unit=field['unit']
            unit_state=field['unit_state'] if field['unit_state'] in ('explicit','conflict') else 'unknown'
            if unit and not any(r.role=='unit' for r in refs):
                diagnostics.append('MISSING_UNIT_SOURCE'); unit=None; unit_state='unknown'
            if unit:
                declared={u for r in refs if r.role=='unit' for u in
                          units_in(evidence[r.ref_id]['quote'])+parse_value(evidence[r.ref_id]['quote'],field['field'])[2]}
                if field.get('unit_basis')=='count_label' and field_key(labels,ALIASES)==field['field']:
                    declared.add('层' if field['field']=='storeys' else '位' if field['field']=='parking_spaces' else '')
                if unit not in declared:
                    diagnostics.append('UNIT_SOURCE_MISMATCH'); unit=None; unit_state='unknown'
            state='conflict' if field['state']=='conflict' else 'derived' if field['state']=='observed' and not diagnostics else 'unknown'
            path=['records',ri,'fields',fi,'value'] if data.get('kind')=='building_schedule' else ['fields',fi,'value']
            subject=record.get('record_id') or field['record_id']
            value=field['raw_text'] if field['qualifier'] else field['value']
            out=_field(oid,sid,subject,'source_record',field['field'], ' / '.join(labels),value,
                'source_nominal',path,data,subject_label=' / '.join(i['text'] for i in record.get('identity',[])) or ' / '.join(field['source_labels']),
                unit=unit,unit_state=unit_state,state=state,
                binding_state='source_bound' if state=='derived' else 'candidate',
                measurement_basis='explicit_business_table_source',diagnostics=sorted(set(diagnostics)))
            out.source_refs.extend(refs)
            out.scope.update(data.get('source_scope',{}))
            out.scope.update(table_id=data['table_id'],row_index=field['row_index'],cell_id=field['cell_id'],
                header_path=field['header_path'],qualifier=field['qualifier'],qualifiers=field.get('qualifiers',[]),
                value_kind=field.get('value_kind'),unit_basis=field.get('unit_basis'))
            yield out


def collect_property_evidence(evidence, observations, allowed_snapshots):
    fields, diagnostics = {}, []
    allowed_snapshots = set(allowed_snapshots)
    for oid, observation in observations.items():
        data = observation.get("data", {})
        sid = observation.get("snapshot_id")
        if sid not in allowed_snapshots or data.get("snapshot_id", sid) != sid:
            diagnostics.append(dict(code="PROPERTY_SNAPSHOT_MISMATCH", observation_id=oid))
            continue
        name = observation.get("tool")
        projected = []
        if name in ('extract_planning_metrics','extract_building_schedule'):
            projected = _business_fields(oid,sid,data,evidence)
        elif name in ("read_table_semantics", "read_door_schedule_table"):
            projected = _table_fields(oid, sid, data, evidence)
        elif name in ("read_dimensions", "get_component_evidence") and data.get("kind") == "dimensions":
            projected = _dimension_fields(oid, sid, data)
        elif name in MEASUREMENT_TOOLS:
            projected = _measurement_fields(oid, sid, data)
        elif name == "analyze_measurements":
            projected = [
                _field(oid, sid, row["object_id"], "geometry_object", row["field"], row["field"],
                       row["value"], "measurement", ["values", i, "value"], data,
                       unit=_unit(row.get("unit")), unit_state="explicit" if _unit(row.get("unit")) else "unknown",
                       state="derived", binding_state="candidate",
                       measurement_basis=row.get("basis", ""),
                       diagnostics=["SELECTED_COLLECTION_ONLY"])
                for i, row in enumerate(data.get("values", []))
                if all(k in row for k in ("object_id", "field", "value", "measurement_ref"))
            ]
            for field, row in zip(projected, [r for r in data.get("values", []) if all(
                    k in r for k in ("object_id", "field", "value", "measurement_ref"))]):
                field.scope.update(batch_id=row.get("batch_id"), measurement_ref=row["measurement_ref"])
        elif name == "calculate_facts" and "value" in data:
            projected = [_field(oid, sid, stable_id("calculation-subject", sid, data.get("calculation_key", oid)),
                                "calculation", data.get("operation", "value"), data.get("target", ""), data["value"],
                                "calculation", ["value"], data, subject_label=data.get("target", ""),
                                unit=_unit(data.get("unit")), unit_state="conditional" if data.get("conditional")
                                else "explicit" if _unit(data.get("unit")) else "unknown",
                                state="derived" if data.get("status") == "calculated" else "unknown",
                                binding_state="candidate", measurement_basis=data.get("operation", ""))]
        for field in projected:
            fields[field.field_id] = field
    business_conflicts={(o.get('snapshot_id'),o.get('data',{}).get('table_id'),cell)
        for o in observations.values() if o.get('tool') in ('extract_planning_metrics','extract_building_schedule')
        for cell in o.get('data',{}).get('read_coverage',{}).get('conflicting_cells',[])}
    for field in fields.values():
        if (field.snapshot_id,field.scope.get('table_id'),field.scope.get('cell_id')) in business_conflicts:
            field.state='conflict'
            field.diagnostics.append('BUSINESS_TABLE_CROSS_PAGE_CONFLICT')
    _comparisons(fields.values())
    return {key: field.model_dump() for key, field in fields.items()}, diagnostics


def _comparisons(fields):
    groups = defaultdict(list)
    for field in fields:
        scope = {k: v for k, v in field.scope.items() if k in (
            "scope_id", "layout_id", "frame_id", "contour_id", "other_object_id",
            "exclusion_contour_ids", "measurement_context", "batch_id", "qualifiers", "value_kind", "qualifier")}
        groups[(field.snapshot_id, field.subject_id, field.property_name, json.dumps(scope, sort_keys=True))].append(field)
    for group in groups.values():
        for field in group:
            peers = [p for p in group if p.field_id != field.field_id and p.value is not None and field.value is not None]
            if not peers:
                continue
            # No automatic physical conversion or equivalence of semantic bases.
            comparable = [p for p in peers if p.unit == field.unit and p.unit is not None]
            same = [p for p in comparable if (p.basis, p.measurement_basis) == (field.basis, field.measurement_basis)]
            conflicts = [p for p in same if not _equal(field.value, p.value)]
            if conflicts:
                field.comparison_state = "conflict"
                field.state = "conflict"
                field.conflicting_field_ids = [p.field_id for p in conflicts]
            elif len(same) != len(peers):
                field.comparison_state = "different_bases" if len(comparable) == len(peers) else "unit_unresolved"
            else:
                field.comparison_state = "consistent"


def query_task_property_evidence(task_data, allowed_snapshots=None, *, subject_id="", property_name="", offset=0, limit=30):
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
        raise ValueError("字段证据分页须 offset≥0、1≤limit≤100。")
    if not isinstance(subject_id, str) or not isinstance(property_name, str) or max(len(subject_id), len(property_name)) > 200:
        raise ValueError("字段过滤须为不超过200字符的 subject_id/property_name。")
    if allowed_snapshots is None:
        allowed_snapshots = {task_data.get("snapshot_id")} | {d["snapshot_id"] for d in task_data.get("related_drawings", [])}
    fields, diagnostics = collect_property_evidence(task_data.get("evidence", {}), task_data.get("observations", {}), allowed_snapshots)
    items = [f for f in fields.values() if (not subject_id or f["subject_id"] == subject_id)
             and (not property_name or f["property_name"] == property_name)]
    items.sort(key=lambda f: (f["snapshot_id"], f["subject_id"], f["property_name"], f["field_id"]))
    page = items[offset:offset + limit]
    ids = {r["ref_id"] for f in page for r in f["source_refs"] if r["kind"] == "evidence"}
    oids = {r["ref_id"] for f in page for r in f["source_refs"] if r["kind"] == "observation"}
    return dict(items=page, total=len(items), offset=offset, limit=limit, has_more=offset + len(page) < len(items),
                next_offset=offset + len(page) if offset + len(page) < len(items) else None,
                evidence=[task_data["evidence"][eid] for eid in sorted(ids)],
                observations=[{k: task_data["observations"][oid][k] for k in ("observation_id", "snapshot_id", "tool")
                               if k in task_data["observations"][oid]} for oid in sorted(oids)],
                diagnostics=diagnostics, note=NOTE)


def query_property_evidence(session, subject_id="", property_name="", offset=0, limit=30):
    return query_task_property_evidence(dict(evidence=session.evidence, observations=session.observations),
                                       session.allowed_snapshots, subject_id=subject_id,
                                       property_name=property_name, offset=offset, limit=limit)


def property_claim_issues(claim, fields):
    """Check typed bindings; legacy prose gets only conservative lexical guards."""
    selected_ids = list(getattr(claim, "property_evidence_ids", []))
    assertions = getattr(claim, "property_assertions", [])
    selected_ids.extend(a.field_id for a in assertions)
    issues = []
    for assertion in assertions:
        field = fields.get(assertion.field_id)
        if field:
            for key in ("subject_id", "property_name", "basis", "unit", "value"):
                equal = _equal(field[key], getattr(assertion, key)) if key == "value" else field[key] == getattr(assertion, key)
                if not equal:
                    issues.append(f"字段证据 {assertion.field_id} 的 {key} 不匹配；禁止借用其他对象、列、单位或口径。")
    for field_id in set(selected_ids):
        field = fields.get(field_id)
        if not field:
            issues.append(f"字段证据 {field_id} 未在当前授权快照的已读来源中登记。")
            continue
        refs = field["source_refs"]
        if claim.evidence_basis == "source":
            required = {r["ref_id"] for r in refs if r["kind"] == "evidence"}
            if field["basis"] != "source_nominal" or not required or not required <= set(claim.evidence_ids):
                issues.append(f"字段证据 {field_id} 必须引用其对象、表头、值与单位来源，测量/计算不得冒充原文。")
        else:
            required = {r["ref_id"] for r in refs if r["kind"] == "observation"}
            if not required <= set(claim.observation_ids):
                issues.append(f"字段证据 {field_id} 缺少对应已读工具观测。")
        if field["state"] == "unknown" and claim.state != "unknown":
            issues.append(f"字段证据 {field_id} 的值、单位或来源未解决，结论须保持 unknown。")
        if field["state"] == "conflict" and claim.state not in ("conflict", "unknown"):
            issues.append(f"字段证据 {field_id} 存在冲突，不能静默选值。")
        if field["unit_state"] == "conditional" and claim.state != "unknown" and not re.search(
                r"假设|约定|暂按|条件|用户|题设|给定", claim.text):
            issues.append(f"字段证据 {field_id} 使用条件单位或基准，正文须保留该条件。")
    # A geometry width is never proof of a finished-face clear width.
    relevant = [f for f in fields.values() if any(r["kind"] == "observation" and r["ref_id"] in claim.observation_ids for r in f["source_refs"])]
    source_fields = [f for f in fields.values() if f["property_name"] in ALIASES.values() and any(
        r["kind"] == "evidence" and r["role"] == "value" and r["ref_id"] in claim.evidence_ids
        for r in f["source_refs"])]
    if claim.state != "unknown":
        from cad_agent.analysis.grounding import contains_number
        # Only explicit adjacent field/value phrases; do not guess arbitrary prose.
        pattern = r"(完成面净宽|宽度|高度|厚度|长度|面积|数量|净宽)\s*(?:为|是|[:：=])?\s*([+-]?\d+(?:\.\d+)?)\s*(mm2|cm2|m2|mm²|cm²|m²|平方米|平方毫米|毫米|厘米|mm|cm|㎡|米|m)?"
        for match in re.finditer(pattern, claim.text):
            prop, number, unit = match.groups()
            candidates = source_fields if claim.evidence_basis == "source" else relevant
            typed = [f for f in candidates if f["property_name"] == _property(prop)]
            # A referenced source cell from another column cannot provide this value.
            if source_fields and not typed and _property(prop) in ALIASES.values():
                issues.append(f"字段“{prop}”不属于引用的表格值单元格；请核对对象行和表头列。")
            elif typed and not any(contains_number(str(f["value"]), number)
                                   and (not unit or f["unit"] == _unit(unit)) for f in typed):
                issues.append(f"字段“{prop}”的数值或单位不匹配已读字段；不能从其他列借值或把绘图单位当物理单位。")
    if any(f["basis"] == "measurement" for f in relevant) and claim.state != "unknown":
        for match in re.finditer(r"完成面净宽|(?:洞口|通行|通道|门洞|门扇)?净宽", claim.text):
            context = claim.text[max(0, match.start()-14):match.end()+18]
            if not re.search(r"不能|不等于|不代表|尚未|未确认|无法|不是|并非|待确认", context):
                issues.append("几何测量不确认完成面或通行净宽；须保留原测量口径并明确净宽未确认。")
                break
    return list(dict.fromkeys(issues))
