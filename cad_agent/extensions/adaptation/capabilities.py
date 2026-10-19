"""Host-owned capability map. Generated code cannot extend this registry."""
from .contracts import SPECS

TRIGGERS = {
    'table_grid': ('read_table', 'read_table_semantics'),
    'text_spans': ('read_source_context', 'query_entities', 'read_table', 'read_table_semantics'),
    'segment_measurements': ('read_geometry', 'measure_geometry', 'query_spatial', 'query_entities', 'read_table'),
    'segment_distances': ('read_geometry', 'measure_geometry', 'query_spatial', 'query_entities'),
    'closed_contour': ('read_geometry', 'measure_geometry', 'query_spatial', 'discover_rooms', 'read_table'),
}
READ_TOOLS = frozenset(name for names in TRIGGERS.values() for name in names)


def capabilities():
    return {method: dict(**spec, trigger_tools=list(TRIGGERS[method]),
        scopes=['table'] if method == 'table_grid' else ['table', 'explicit_region'],
        code_entrypoint='adapt(bundle)', execution='isolated_container', output_mode='shadow_only',
        automatic_adoption=False, automatic_registration=False) for method, spec in SPECS.items()}
