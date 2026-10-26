"""Bounded provenance coverage, not a claim of semantic completeness."""
import re
from cad_agent.analysis.typed_requirements import is_source_fact


def property_term(field):
    return re.sub(r'(?:原文|做法|情况|分别|是什么|有哪些|多少|[？?])', '', field).strip()


BOUND_READS = {'read_assembly', 'read_table_semantics', 'extract_planning_metrics', 'extract_building_schedule'}


def _registered(source, registry, snapshot):
    """Resolve an exact already-read source, never trust a supplied ID alone."""
    if not isinstance(source, dict):return None
    stored = registry.get(source.get('evidence_id'))
    keys = ('evidence_id', 'snapshot_id', 'region_id', 'text_span', 'quote')
    if (stored and stored.get('snapshot_id') == snapshot and stored.get('quote') and
            all(k in source and source[k] == stored.get(k) for k in keys)):
        return stored
    return None


def _registered_all(sources, registry, snapshot):
    found = [_registered(e, registry, snapshot) for e in sources]
    return found if found and all(found) else []


def _inside(source, spans):
    return any(s.get('region_id') == source['region_id'] and
               s['text_span'][0] <= source['text_span'][0] < source['text_span'][1] <= s['text_span'][1]
               for s in spans)


def _bound_records(tool, result, registry, observations):
    """Consume only registered tool receipts, retaining record and field edges."""
    observation = observations.get(result.get('observation_id'), {})
    if observation.get('tool') != tool:return []
    data = observation.get('data', {})
    sid = data.get('snapshot_id')
    key = 'record_id' if tool == 'read_assembly' else 'table_id'
    if (not sid or not data.get(key) or result.get(key) != data[key] or
            result.get('snapshot_id') != sid or observation.get('snapshot_id') != sid):return []
    if any(result.get(k) != value for k, value in data.items()):return []
    if tool in ('extract_planning_metrics','extract_building_schedule'):
        records=[]
        groups=data.get('records',[]) if tool=='extract_building_schedule' else [dict(fields=data.get('fields',[]))]
        for group in groups:
            for field in group['fields']:
                if field['state']!='observed':continue
                identity_sources=([e for i in group.get('identity',[]) for e in i['evidence']] or
                                  field['label_evidence'] or field.get('scope_evidence',[]) or field['header_evidence'])
                identity=_registered_all(identity_sources,registry,sid)
                values=_registered_all(field['evidence'],registry,sid)
                headers=_registered_all(field['header_evidence']+field['label_evidence'],registry,sid)
                if identity and values and headers:
                    records.append(dict(identity=identity,fields=[dict(values=values,headers=headers,
                        labels=field['header_path']+field['source_labels'])],proof=[]))
        return records
    if tool == 'read_assembly':
        binding = data.get('source_binding') or {}
        basis = binding.get('basis')
        if (data.get('view') != 'record' or binding.get('state') != 'source_bound' or
                binding.get('diagnostics') or data.get('reference_reads') or
                binding.get('layer_completeness') == 'requires_reference_read' or
                basis not in ('explicit_heading_bounded_numbered_section', 'explicit_header_and_closed_source_row')):
            return []
        # A last-page flag is insufficient: every segment of this exact record
        # must have a successful registered read with the same source binding.
        total, items, exhausted = data.get('total', 0), {}, False
        if not isinstance(total, int) or total <= 0:return []
        for obs in observations.values():
            page = obs.get('data', {})
            if (obs.get('tool') != tool or page.get(key) != data[key] or page.get('snapshot_id') != sid or
                    obs.get('snapshot_id') != sid or page.get('version') != data.get('version') or
                    page.get('target_region_id') != data.get('target_region_id') or page.get('view') != 'record' or
                    page.get('source_binding') != binding or page.get('total') != total):continue
            for position, item in enumerate(page.get('items', []), page.get('offset', 0)):
                source = _registered(item.get('evidence'), registry, sid)
                if (not source or item.get('order') != position or item.get('source_binding_state') != 'source_bound' or
                        item.get('source_binding_basis') != basis or item.get('truncated') or item.get('next_start') is not None):continue
                if position in items and items[position] != item:return []
                items[position] = item
            exhausted |= page.get('has_more') is False and page.get('offset', 0) + len(page.get('items', [])) == total
        if not exhausted or sorted(items) != list(range(total)):return []
        if basis == 'explicit_heading_bounded_numbered_section':
            title = _registered(binding.get('title_evidence'), registry, sid)
            boundary = _registered_all(binding.get('boundary_evidence', []), registry, sid)
            if (not title or not boundary or not binding.get('code') or binding['code'] not in title['quote'] or
                    binding.get('layer_completeness') != 'bounded_source_sequence'):return []
            # The title is identity, not the field value; a heading mentioning
            # a grade cannot stand in for its separate, still unread value.
            values = [i['evidence'] for i in items.values() if i['evidence']['evidence_id'] != title['evidence_id']]
            return [dict(identity=[title], fields=[dict(values=values, headers=[], labels=[])], proof=boundary)]
        headers = binding.get('headers', {})
        if headers.get('state') != 'source_bound':return []
        identities, fields = [], []
        for field in binding.get('fields', []):
            if field.get('value_state') != 'observed' or not field.get('has_value'):continue
            col = field['column_index']
            values = [i['evidence'] for i in items.values() if i.get('column_index') == col and
                      _inside(i['evidence'], field.get('source_spans', []))]
            if not values:continue
            heading = next((h for h in headers.get('fields', []) if h.get('column_index') == col), {})
            head_sources = _registered_all(field.get('header_evidence', []), registry, sid)
            if not head_sources:continue
            if col in binding.get('identity_columns', []):
                identities.extend(e for e in _registered_all(field.get('evidence', []), registry, sid)
                                  if _inside(e, field.get('source_spans', [])))
            fields.append(dict(values=values, headers=head_sources, labels=[heading.get('text', '')]))
        return [dict(identity=identities, fields=fields, proof=[])] if identities else []
    if tool == 'read_table_semantics':
        records = []
        for record in data.get('records', []):
            binding = record.get('binding', {})
            if (binding.get('state') != 'source_bound' or binding.get('basis') != 'explicit_header_and_source_grid' or
                    binding.get('diagnostics')):continue
            # Match this returned row's cells; never borrow another row's value.
            row = next((r for r in data.get('items', []) if r.get('row_index') == record.get('row_index')), {})
            row_cells = {c['cell_id']:c for c in row.get('cells', [])}
            identities, fields = [], []
            for field in record.get('fields', []):
                source_cell = row_cells.get(field.get('cell_id'), {})
                if (field.get('state') != 'observed' or not field.get('text', '').strip() or
                        field.get('text') != source_cell.get('text') or field.get('evidence') != source_cell.get('evidence')):continue
                values = _registered_all(field.get('evidence', []), registry, sid)
                headers = _registered_all(field.get('header_evidence', []), registry, sid)
                if not values or not headers:continue
                if field['cell_id'] in binding.get('identity_cell_ids', []):identities.extend(values)
                fields.append(dict(values=values, headers=headers, labels=field.get('header_path', [])))
            if identities:records.append(dict(identity=identities, fields=fields, proof=[]))
        return records
    return []


def _bound_evidence(cell, records):
    normalize = lambda text: re.sub(r'\s+', '', text)
    for record in records:
        identity = [e for e in record['identity'] if cell['object'] in e['quote']]
        if not identity:continue
        identity_ids = {e['evidence_id'] for e in record['identity']}
        for field in record['fields']:
            # Explicit header text or a literal value phrase only. A generic
            # layers role does not imply every material/thickness/grade field.
            header_match = normalize(cell['field']) in [normalize(s) for s in field['labels'] if s]
            values = field['values'] if header_match else [e for e in field['values']
                if e['evidence_id'] not in identity_ids and cell['field'] in e['quote']]
            if values:
                return {e['evidence_id'] for e in identity + field['headers'] + values + record['proof']}
    return set()


def record_cells(cells, result, evidence, *, tool=None, registry=None, observations=None):
    """Lexical source candidates only; neighboring text cannot establish binding.

    A same-clause match is a review candidate, never semantic completion. Source
    spans and their exact evidence IDs stay distinct, including in batch reads.
    """
    bound = _bound_records(tool, result, registry or {}, observations or {}) if tool in BOUND_READS else []
    excluded, truncated = set(), set()
    def visit(value):
        if isinstance(value, dict):
            own = value.get('evidence', {})
            if isinstance(own, dict) and own.get('evidence_id'):
                eid = own['evidence_id']
                if value.get('relationship') not in (None, 'target'):
                    excluded.add(eid)
                if value.get('next_start') is not None:
                    truncated.add(eid)
            for child in value.values():visit(child)
        elif isinstance(value, list):
            for child in value:visit(child)
    visit(result)
    objects = {cell['object'] for cell in cells}
    for cell in cells:
        # User conditions and acceptance wording are never drawing evidence.
        # Spatial and numeric cells require their existing operation receipts;
        # matching prose cannot become an executed measurement/calculation.
        if not is_source_fact(cell):continue
        if cell['state'] in ('addressed', 'unknown', 'conflict'):continue
        matched = _bound_evidence(cell, bound)
        if matched:
            cell['evidence_ids'] = sorted(set(cell['evidence_ids']) | matched)
            cell.update(state='source_candidate', gap_kind='attribution_unverified', source_binding_basis='registered_source_record')
            continue
        prop = property_term(cell['field'])
        candidates, object_seen, unbound = [], False, False
        for eid, source in evidence.items():
            text = source.get('quote', '')
            if cell['object'] not in text:continue
            object_seen = True
            if eid in excluded:
                unbound = True
                continue
            if eid in truncated:continue
            paired = False
            for clause in re.split(r'[。；;\n]', text):
                active = set()
                for segment in re.split(r'[，,]', clause):
                    explicit = {obj for obj in objects if obj in segment}
                    if explicit:active = explicit
                    if active == {cell['object']} and (not prop or prop in segment):paired = True
            if paired and tool not in BOUND_READS:
                candidates.append(eid)
            elif not prop or prop in text:
                unbound = True
        if candidates:
            cell['evidence_ids'] = sorted(set(cell['evidence_ids']) | set(candidates))
            cell.update(state='source_candidate', gap_kind='attribution_unverified')
        elif cell['state'] != 'source_candidate':
            if unbound:
                cell.update(state='attribution_unknown', gap_kind='attribution_unknown')
            elif object_seen and not truncated:
                cell.update(state='not_found_in_read_scope', gap_kind='not_found_in_read_scope')
            else:
                cell.setdefault('gap_kind', 'unread_source')


def question_parts(question):
    # Explicit user enumeration only. Do not invent a domain-specific checklist.
    markers = list(re.finditer(r"[①②③④⑤⑥⑦⑧⑨⑩]|(?:^|\n)\s*\d{1,2}[、.)）]\s*", question))
    if not 2 <= len(markers) <= 10:
        return []
    return [{"id": f"q{i+1}", "text": question[m.end():markers[i+1].start() if i+1 < len(markers) else len(question)].strip()}
            for i, m in enumerate(markers)]


class EvidenceCoverage:
    def __init__(self, question):
        self.parts = question_parts(question)
        self.candidates = {}
        self.read_spans = {}
        self.reserve_used = False
        self.task_owners = {}

    def record(self, name, result, task_ids=None):
        if name == "search_sources" and result.get("items"):
            # Keep two leading candidates: the top semantic hit can be a general
            # clause while the next is the requested object's table row.
            # Reverse insertion preserves rank order within the newest search.
            for item in reversed(result["items"][:2]):
                rid = item.get("region_id")
                if rid:
                    start = item.get("snippet_start", 0)
                    self.task_owners.setdefault((rid,start),set()).update(task_ids or [])
                    self.candidates.pop((rid, start), None)
                    self.candidates[(rid, start)] = {"region_id": rid, "start": start, "neighbors": 1}
        def visit(value):
            if isinstance(value, dict):
                if "evidence_id" in value and "quote" in value and "text_span" in value:
                    self.read_spans.setdefault(value["region_id"], []).append(value["text_span"])
                else:
                    for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)
        visit(result)

    def owners(self,args):
        return sorted(self.task_owners.get((args["region_id"],args["start"]),set()))

    def pending(self):
        return [args for (rid, start), args in reversed(list(self.candidates.items()))
                if not any(a <= start < b for a, b in self.read_spans.get(rid, []))][:2]

    def summary(self):
        return {"question_parts": self.parts, "pending_source_reads": self.pending(),
                "read_region_count": len(self.read_spans),
                "note": "候选未读不等于必须适用；已读数量不证明完整性。逐项回答或明确标 unknown。"}
