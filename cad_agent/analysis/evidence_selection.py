"""Publication uses only validated claim references; investigation stays archived."""
from cad_agent.contracts import stable_id

VERSION = 'claim-selection-v1'


def manifest(result):
    selected, uses = [], []
    available = {e['evidence_id']: e for e in result.get('citations', [])}
    for index, claim in enumerate(result.get('claims', [])):
        ids = claim.get('evidence_ids', [])
        for eid in ids:
            if eid not in available:
                raise ValueError('DELIVERY_SOURCE_MISSING: validated claim source is absent')
            if eid not in selected:
                selected.append(eid)
        uses.append(dict(claim_id=stable_id('claim', str(index), claim.get('text', '')),
            requirement_ids=claim.get('question_part_ids', []), evidence_ids=list(ids),
            evidence_roles=claim.get('evidence_roles', {}), state=claim.get('state'),
            support_status='structural_checks_passed_semantics_requires_review'))
    return dict(version=VERSION, selected_evidence_ids=selected, claim_uses=uses,
                requirement_outcomes=result.get('requirement_outcomes', []),
                semantic_verification='not_independently_verified')


def publish(result, session=None):
    selection = manifest(result)
    allowed = set(selection['selected_evidence_ids'])
    citations = [e for e in result.get('citations', []) if e['evidence_id'] in allowed]
    numbers = {e['evidence_id']: i for i, e in enumerate(citations, 1)}
    for claim in result.get('claims', []):
        claim['citation_numbers'] = [numbers[e] for e in claim.get('evidence_ids', [])]
    result['citations'] = citations
    if session is not None and hasattr(session, 'observations'):
        selection['source_bindings'] = []
        for observation in session.observations.values():
            data = observation.get('data', {})
            if observation.get('tool') != 'read_assembly':
                continue
            ids = {i.get('evidence', {}).get('evidence_id') for i in data.get('items', [])} & allowed
            if ids:
                binding = data.get('source_binding') or {}
                selection['source_bindings'].append(dict(record_id=data.get('record_id'),
                    evidence_ids=sorted(ids), state=binding.get('state', 'unresolved'),
                    basis=binding.get('basis'), diagnostics=data.get('diagnostics', []),
                    scope=data.get('source_scope'), engineering_applicability='unverified'))
        frames = {}
        for c in citations:
            sid = c.get('snapshot_id')
            if not sid or not hasattr(session, 'w'):
                continue
            if sid not in frames:
                frames[sid] = {f['frame_id']: f.get('name', '') for f in session.w.store.get('drawing', sid).get('frames', [])}
            c['source_context'] = dict(frame_id=c.get('frame_id'), frame_name=frames[sid].get(c.get('frame_id'), ''),
                                       basis='parser_frame_metadata')
    result['delivery_manifest'] = selection
    return result
