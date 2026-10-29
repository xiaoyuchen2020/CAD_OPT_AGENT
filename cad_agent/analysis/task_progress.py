"""Requirement-owned progress. Retrieval novelty is not accepted fact growth."""
import hashlib
import json
import re

VERSION = 'requirement-progress-v1'
SOURCE_TOOLS = {'search_sources', 'read_source_context', 'read_assembly', 'read_table',
                'read_table_semantics', 'read_requirement_sources', 'get_evidence', 'query_property_evidence'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def terms(step):
    result = {c['object'] for c in step.get('coverage', []) if c.get('object')}
    result.update(re.findall(r'外墙面?|内墙面?|楼地面|屋面|顶棚|踢脚|墙体|砂浆|混凝土|门窗|楼板', step['requirement']))
    return sorted(result)


def register_search_terms(steps, ids, result):
    """Literal query fragments are exploration hints, never acceptance credit."""
    words = {w for group in result.get('query_variants', []) for w in group
             if isinstance(w, str) and len(w.strip()) >= 2}
    for step in steps:
        if step['id'] in ids:
            allowed = {w for w in words if any(w in t for t in terms(step))}
            step['progress_search_terms'] = sorted(set(step.get('progress_search_terms', [])) | allowed)[:32]
    by_id = {s['id']: s for s in steps}
    for step in steps:
        # An explicitly dependent requirement can investigate its producer's
        # source too. This inherits exploration terms only, not verified facts,
        # coverage states, operation receipts or completion.
        if step['id'] in ids:
            inherited = {w for dep in step.get('depends_on', []) if dep in ids
                         for w in by_id.get(dep, {}).get('progress_search_terms', [])}
            step['progress_search_terms'] = sorted(set(step.get('progress_search_terms', [])) | inherited)[:32]


def record(step, name, result, evidence, duplicate, receipts=(), defer_stall=False):
    state = step.setdefault('quality_progress', dict(version=VERSION, verified=[], candidates=[], diagnostics=[], stalled=0))
    old = {k: set(state[k]) for k in ('verified', 'candidates', 'diagnostics')}
    new = {k: set() for k in old}
    targets = terms(step)
    relevant = lambda text: not targets or any(t in text for t in targets)
    candidate_relevant = lambda text: relevant(text) or any(t in text for t in step.get('progress_search_terms', []))
    binding = result.get('source_binding') or {}
    identity = json.dumps([binding.get('title_evidence'), binding.get('fields'), result.get('title_evidence')], ensure_ascii=False)
    bound_relevant = relevant(identity)
    if not duplicate:
        if name in SOURCE_TOOLS:
            for item in result.get('items', []):
                ref = item.get('evidence') or {}
                text = ref.get('quote') or item.get('snippet') or item.get('text', '')
                if candidate_relevant(text) or bound_relevant:
                    # Search snippets and actual reads are different stages;
                    # reading a known candidate must not look like repetition.
                    fp = digest(['read' if ref else 'candidate', ref.get('snapshot_id'), item.get('frame_id'), ref.get('bbox'), ref.get('text_span'), text])
                    key = 'verified' if binding.get('state') == 'source_bound' and bound_relevant else 'candidates'
                    new[key].add(fp)
            for ref in evidence.values():
                if candidate_relevant(ref.get('quote', '')):
                    new['candidates'].add(digest([ref.get('snapshot_id'), ref.get('bbox'), ref.get('text_span'), ref.get('quote')]))
            for cell in step.get('coverage', []):
                if cell.get('source_binding_basis') or cell.get('state') in ('addressed', 'conflict'):
                    new['verified'].add(digest({k: cell.get(k) for k in ('object', 'field', 'state', 'source_binding_basis', 'evidence_ids')}))
            diagnostics = result.get('diagnostics', [])
            if diagnostics and (bound_relevant or any(relevant(e.get('quote', '')) for e in evidence.values())):
                new['diagnostics'].add(digest([name, result.get('target_region_id'), diagnostics]))
        else:
            # Preserve domain operation receipts; these do not assert source facts.
            for receipt in receipts:
                if receipt.get('fulfilled'):
                    new['verified'].add(digest(receipt))
            # Existing geometry discovery has its own identity/measurement gates.
            if name not in {'read_evidence_record', 'get_drawing_overview'}:
                semantic = {k: v for k, v in result.items() if k not in ('observation_id', 'created_at', 'elapsed_seconds', 'evidence_record')}
                if semantic and not result.get('error'):
                    new['candidates'].add(digest([name, semantic]))
    delta = {k: sorted(new[k] - old[k]) for k in old}
    if delta['verified']:
        kind = 'verified_progress'
    elif delta['candidates'] and len(old['candidates']) < 64:
        kind = 'candidate_progress'
    elif delta['diagnostics'] and len(old['diagnostics']) < 2:
        kind = 'diagnostic_progress'
    else:
        kind = 'no_progress'
    for key in old:
        state[key] = sorted(old[key] | new[key])[:128]
    if not defer_stall or kind != 'no_progress':
        state['stalled'] = state['stalled'] + 1 if kind == 'no_progress' else 0
    event = dict(version=VERSION, requirement_id=step['id'], tool=name, kind=kind,
                 delta_counts={k: len(v) for k, v in delta.items()}, stalled=state['stalled'])
    step['last_progress'] = event
    return event


def signature(data):
    return digest([dict(id=s['id'], progress={k: s.get('quality_progress', {}).get(k, [])
                         for k in ('verified', 'candidates')}, delivered=s.get('delivered'),
                        receipts=s.get('operation_receipts'), coverage=s.get('coverage'))
                   for s in data.get('subtasks', [])])
