"""Task-specific model views; never changes source, read or completion authority.

Policies compose by union. Unknown tools/requirements keep their full payload.
Only known presentation fields are removable; source semantics are not allowlisted.
"""
from copy import deepcopy
from dataclasses import dataclass, field

VERSION = 'context-policy-v1'
SOURCE_TOOLS = {'read_assembly', 'read_source_context', 'read_requirement_sources', 'get_evidence'}
TABLE_TOOLS = {'read_table', 'read_table_semantics', 'read_door_schedule_table',
               'extract_planning_metrics', 'extract_building_schedule',
               'search_tables', 'inspect_table', 'read_table_content'}
CALCULATION_TOOLS = {'calculate_facts', 'analyze_measurements', 'check_dimension_chain'}
# A tool being exposed does not prove its results were observed. This set only
# controls what metadata must survive when an actual result is serialized.
from cad_agent.analysis.tool_metadata import SPATIAL_METHODS, GROUPS
SPATIAL_TOOLS = (GROUPS['geometry'] | GROUPS['vision'] | GROUPS['rooms'] | SPATIAL_METHODS) - SOURCE_TOOLS - TABLE_TOOLS - CALCULATION_TOOLS
KNOWN_TOOLS = SOURCE_TOOLS | TABLE_TOOLS | CALCULATION_TOOLS | SPATIAL_TOOLS
TYPE_FEATURES = {
    'SOURCE_FACT': {'source'}, 'SPATIAL_FACT': {'spatial'},
    'DERIVED_MEASURE': {'spatial', 'numeric'},
    'DERIVED_CALCULATION': {'numeric', 'calculation'},
    'USER_CONDITION': {'user_condition'}, 'DELIVERY_REQUIREMENT': {'delivery'},
}
SPATIAL_OPERATIONS = {'area', 'perimeter', 'geometry', 'relations', 'vision',
                      'contours', 'discovery', 'exclusions', 'highlight', 'dimensions'}


@dataclass(frozen=True)
class ContextPolicy:
    features: frozenset[str] = frozenset({'source'})
    conservative: bool = False

    @property
    def full_numbers(self):
        return self.conservative or bool(self.features & {'numeric', 'calculation', 'spatial'})

    @property
    def spatial(self):
        return self.conservative or 'spatial' in self.features


def resolve_context_policy(requirement_types=(), operations=(), observation_tools=()):
    features = {'source'}
    conservative = False
    for kind in requirement_types:
        if kind not in TYPE_FEATURES:
            conservative = True
        features.update(TYPE_FEATURES.get(kind, ()))
    for operation in operations:
        if operation in SPATIAL_OPERATIONS:
            features.update(('spatial', 'numeric'))
        elif operation in ('arithmetic', 'ranking'):
            features.update(('numeric', 'calculation'))
        elif operation == 'units':
            features.add('numeric')
        elif operation not in ('selection', 'source_read'):
            conservative = True
    for tool in observation_tools:
        if tool in TABLE_TOOLS:
            features.add('table')
        elif tool in CALCULATION_TOOLS:
            features.update(('numeric', 'calculation'))
        elif tool in SPATIAL_TOOLS:
            features.update(('spatial', 'numeric'))
        elif tool not in SOURCE_TOOLS:
            conservative = True
    return ContextPolicy(frozenset(features), conservative)


def policy_for_session(session):
    from cad_agent.analysis.typed_requirements import requirement_type
    kinds, operations = [], []
    for step in getattr(getattr(session, 'task', None), 'data', {}).get('subtasks', []):
        operations.extend(step.get('operations', []))
        for cell in step.get('coverage', []):
            kinds.append(requirement_type(cell, step))
    policy = resolve_context_policy(kinds, operations,
        (o.get('tool') for o in getattr(session, 'observations', {}).values()))
    # Before a typed plan exists, preserve numeric/spatial information. The query
    # keyword profile alone cannot authorize deleting task-relevant metadata.
    if not kinds and not operations:
        return ContextPolicy(policy.features, conservative=True)
    return policy


def reference_dependencies(value):
    """Find structured source/observation references, never text lookalikes."""
    evidence, observations = set(), set()
    def visit(node):
        if isinstance(node, list):
            for item in node: visit(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                # observation_id defines this observation; only nested references
                # are dependencies (the caller removes the root identity).
                values = item if isinstance(item, list) else [item]
                if key.endswith(('evidence_id', 'evidence_ids')) or key == 'evidence_roles':
                    if isinstance(item, dict):
                        values = [v for ids in item.values() for v in (ids if isinstance(ids, list) else [ids])]
                    evidence.update(v for v in values if isinstance(v, str) and v)
                if key.endswith(('observation_id', 'observation_ids')):
                    observations.update(v for v in values if isinstance(v, str) and v)
                visit(item)
    visit(value)
    if isinstance(value, dict):
        observations.discard(value.get('observation_id'))
    return evidence, observations


@dataclass
class ProjectedObservation:
    payload: dict
    required_evidence_ids: set[str] = field(default_factory=set)
    required_observation_ids: set[str] = field(default_factory=set)
    diagnostics: list[str] = field(default_factory=list)


def project_observation(observation, policy):
    out = deepcopy(observation)
    tool = out.get('tool')
    diagnostics = []
    if tool not in KNOWN_TOOLS:
        diagnostics.append('UNSUPPORTED_TOOL_PRESERVED:' + str(tool))
    elif tool in SOURCE_TOOLS | TABLE_TOOLS and not policy.spatial:
        # Only tool-level display affordances, not arbitrary nested keys. Header,
        # unit, row, condition, conflict and newly added fields survive by default.
        data = out.get('data', {})
        for key in ('preview_url', 'highlight_action'):
            data.pop(key, None)
    evidence, observations = reference_dependencies(out)
    return ProjectedObservation(out, evidence, observations, diagnostics)


def project_numbers(source):
    """Remove only reproducible provenance copies, retaining numeric semantics.

    Full source spans/hash stay in the ledger. Units, ambiguous interpretations,
    identifiers and clipped source_raw tokens are never omitted.
    """
    numeric = deepcopy(source.get('numeric_readings'))
    if not numeric:
        return numeric
    numeric.pop('quote_sha256', None)
    quote, source_span = source.get('quote', ''), source.get('text_span')
    for atom in numeric.get('items', []):
        span = atom.get('text_span')
        if not isinstance(span, list) or len(span) != 2:
            continue
        a, b = span
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(quote):
            continue
        if atom.get('raw') == quote[a:b]:
            atom.pop('raw')
        if source_span and atom.get('source_span') == [source_span[0]+a, source_span[0]+b]:
            atom.pop('source_span')
    return numeric


def observation_closure(root, observations):
    """All linked observations and sources, cycle-safe and all-or-nothing."""
    selected, evidence, missing = {}, set(), set()
    def visit(observation):
        oid = observation['observation_id']
        if oid in selected: return
        selected[oid] = observation
        refs, dependencies = reference_dependencies(observation)
        evidence.update(refs)
        for dependency in sorted(dependencies):
            if dependency not in observations:
                missing.add(dependency)
            else:
                visit(observations[dependency])
    visit(root)
    return list(selected.values()), evidence, missing
