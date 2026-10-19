"""Runtime-neutral tool capability metadata. No session imports."""
GROUPS = {
    'sources': {'read_assembly','calculate_facts','resolve_drawing_scope','read_project_sources','search_sources','read_source_context','read_table','get_evidence','get_drawing_overview'},
    'facts': {'query_facts','extract_facts','get_evidence'},
    'rooms': {'prepare_discovery_review','review_discovered_spaces','inspect_enclosure','get_drawing_context','get_analysis_results','analyze_measurements','resolve_room_units','prepare_room_boundaries','select_room_boundaries','analyze_room_batch','discover_rooms','get_room_contours','measure_room_area','measure_room','get_measurement_evidence'},
    'geometry': {'get_drawing_context','read_dimensions','query_building_relations','get_component_evidence','query_entities','read_geometry','query_spatial','measure_geometry','get_measurement_evidence','inspect_region'},
    'vision': {'render_region','bind_visual_objects','query_entities','inspect_region','measure_geometry','measure_room','get_measurement_evidence'},
    'semantics': {'get_drawing_context','read_dimensions','get_component_evidence','query_annotations','locate_by_axes','trace_detail','read_table_semantics'},
    'components': {'locate_space_labels','prepare_space_connections','review_space_connections','check_dimension_chain','get_drawing_context','read_dimensions','query_building_relations','get_component_evidence'},
}
READS = {'read_assembly','read_project_sources','read_source_context','read_table','get_evidence','read_table_semantics'}
CONTROL = {'plan_task','request_toolset','finish_analysis'}

OPERATION_TOOLS = {
    'area': {'analyze_measurements','measure_room_area','select_room_boundaries','measure_room'},
    'perimeter': {'analyze_measurements','measure_room_area'},
    'arithmetic': {'analyze_measurements','calculate_facts','check_dimension_chain'},
    'ranking': {'analyze_measurements','calculate_facts'}, 'selection': {'analyze_measurements'},
    'highlight': {'select_room_boundaries','analyze_measurements','render_region','check_dimension_chain'},
    'discovery': {'discover_rooms'}, 'contours': {'get_room_contours','select_room_boundaries'},
    'exclusions': {'measure_room_area','inspect_enclosure'}, 'relations': {'query_building_relations'},
    'dimensions': {'read_dimensions','check_dimension_chain'},
    'geometry': {'measure_geometry','read_dimensions','check_dimension_chain'},
    'vision': {'render_region','select_room_boundaries'},
    'units': {'resolve_room_units','analyze_room_batch'},
}

