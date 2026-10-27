"""Runtime-neutral tool capability metadata. No session imports."""
GROUPS = {
    'sources': {'search_source_records','select_source_records','batch_read_sources','read_assembly','calculate_facts','resolve_drawing_scope','read_project_sources','search_sources','read_source_context','read_table','get_evidence','get_drawing_overview'},
    'facts': {'query_facts','extract_facts','get_evidence'},
    'rooms': {'prepare_discovery_review','review_discovered_spaces','inspect_enclosure','get_drawing_context','get_analysis_results','analyze_measurements','resolve_room_units','prepare_room_boundaries','select_room_boundaries','analyze_room_batch','discover_rooms','get_room_contours','measure_room_area','measure_room','get_measurement_evidence'},
    'geometry': {'inspect_scope_objects','get_drawing_context','read_dimensions','query_building_relations','get_component_evidence','query_entities','read_geometry','query_spatial','measure_geometry','get_measurement_evidence','inspect_region'},
    'vision': {'render_region','bind_visual_objects','query_entities','inspect_region','measure_geometry','measure_room','get_measurement_evidence'},
    'semantics': {'get_drawing_context','read_dimensions','get_component_evidence','query_annotations','locate_by_axes','trace_detail','read_table_semantics'},
    'components': {'locate_space_labels','prepare_space_connections','review_space_connections','check_dimension_chain','get_drawing_context','read_dimensions','query_building_relations','get_component_evidence'},
}
from cad_agent.extensions.programs.contracts import TOOLS as PROGRAM_TOOLS, RESULT_TOOLS
for _family in ('sources', 'geometry', 'semantics'):
    GROUPS[_family].update({'develop_method', 'run_task_method'} | PROGRAM_TOOLS)

READS = {'read_assembly','read_project_sources','read_source_context','read_table','get_evidence','read_table_semantics'}
READS.add('read_door_schedule_table')
READS.add('read_requirement_sources')
READS.update({'develop_method','run_task_method'} | RESULT_TOOLS)
BUSINESS_TABLE_TOOLS = {'extract_planning_metrics', 'extract_building_schedule'}
READS.update(BUSINESS_TABLE_TOOLS)
GROUPS['sources'].update(BUSINESS_TABLE_TOOLS)
GROUPS['semantics'].update(BUSINESS_TABLE_TOOLS)
GROUPS['sources'].add('read_requirement_sources')
GROUPS['semantics'].update({'read_door_schedule_table', 'trace_door_schedule'})
GROUPS['components'].update({'read_door_schedule_table', 'trace_door_schedule'})
GROUPS['geometry'].add('search_similar_objects')
GROUPS['vision'].add('search_similar_objects')
GROUPS['components'].add('search_similar_objects')
for _family in ('sources', 'facts', 'geometry', 'semantics', 'components'):
    GROUPS[_family].add('query_property_evidence')
CONTROL = {'plan_task','request_toolset','finish_analysis','read_evidence_record'}
SPATIAL_METHODS = {
    'inspect_spatial_boundary', 'measure_spatial_area', 'measure_spatial_relation',
    'inspect_door_sweep', 'measure_door_sweep_relation',
    'measure_spatial_sections', 'measure_elevation_relation',
}
GROUPS['geometry'].update({'bind_spatial_object', 'get_spatial_object'} | SPATIAL_METHODS)

OPERATION_TOOLS = {
    'area': {'analyze_measurements','measure_room_area','select_room_boundaries','measure_room'},
    'perimeter': {'analyze_measurements','measure_room_area'},
    'arithmetic': {'analyze_measurements','calculate_facts','check_dimension_chain'},
    'ranking': {'analyze_measurements','calculate_facts'}, 'selection': {'analyze_measurements','select_source_records'},
    'highlight': {'select_room_boundaries','analyze_measurements','render_region','check_dimension_chain'},
    'discovery': {'discover_rooms'}, 'contours': {'get_room_contours','select_room_boundaries'},
    'exclusions': {'measure_room_area','inspect_enclosure'}, 'relations': {'query_building_relations'},
    'dimensions': {'read_dimensions','check_dimension_chain'},
    'geometry': {'inspect_scope_objects','measure_geometry','read_dimensions','check_dimension_chain'},
    'vision': {'render_region','select_room_boundaries'},
    'units': {'resolve_room_units','analyze_room_batch'},
}
OPERATION_TOOLS['area'].add('measure_spatial_area')
OPERATION_TOOLS['perimeter'].add('measure_spatial_area')
OPERATION_TOOLS['contours'].add('inspect_spatial_boundary')
OPERATION_TOOLS['exclusions'].add('measure_spatial_area')
OPERATION_TOOLS['relations'].update({'measure_spatial_relation', 'measure_door_sweep_relation'})
OPERATION_TOOLS['geometry'].update(SPATIAL_METHODS | {'bind_spatial_object', 'get_spatial_object'})
OPERATION_TOOLS['relations'].add('measure_elevation_relation')
OPERATION_TOOLS['arithmetic'].add('measure_elevation_relation')


for _family in ('sources', 'semantics'):
    GROUPS[_family].update({'search_tables','inspect_table','read_table_content','recover_table_region'})
GROUPS['vision'].update({'recover_table_region','review_table_region'})
READS.add('read_table_content')
