"""Registered spatial methods with persistent, source-bound delivery records."""
from copy import deepcopy


def execute_spatial_method(session, name, args):
    from cad_agent.geometry import spatial_boundaries, spatial_relations, spatial_sections
    handlers = {
        'inspect_spatial_boundary': spatial_boundaries.inspect_spatial_boundary,
        'measure_spatial_area': spatial_boundaries.measure_spatial_area,
        'measure_spatial_relation': spatial_relations.measure_spatial_relation,
        'inspect_door_sweep': spatial_relations.inspect_door_sweep,
        'measure_door_sweep_relation': spatial_relations.measure_door_sweep_relation,
        'measure_spatial_sections': spatial_sections.measure_spatial_sections,
        'measure_elevation_relation': spatial_sections.measure_elevation_relation,
    }
    result = handlers[name](session, args)
    # Public facts remain available on reload; full native geometry stays in
    # the source object and immutable tool archive, not a guessed UI outline.
    public = {key: deepcopy(result[key]) for key in (
        'kind', 'object_id', 'other_object_id', 'snapshot_id', 'source_sha256',
        'scope_id', 'layout_id', 'frame_id', 'status', 'measurement_basis',
        'spatial_fact_scope', 'unit', 'measurements', 'coverage', 'diagnostics',
        'relations', 'distance_interpretation', 'geometry_selections',
    ) if key in result}
    public['tool'] = name
    rows = session.task.data.setdefault('spatial_results', [])
    signature = (name, args.model_dump())
    # Repeating exactly one operation replaces its display row, not other
    # measurements, roles or selected exclusion bases on the same source.
    public['arguments'] = deepcopy(signature[1])
    rows[:] = [row for row in rows
               if (row.get('tool'), row.get('arguments')) != signature]
    rows.append(public)
    return result
