"""Scope-to-geometry progression must preserve uncertainty and original paths."""
import pytest
from cad_agent.analysis.scope_objects import candidate_pairs
from cad_agent.analysis.scope_workflow import record_scope_failure, pending_scope_calls
from cad_agent.geometry.contracts import GeometryMeasure
from cad_agent.geometry.service import GeometryService
from test_b05_scope_objects import rec
from test_remediation_scope import session, invoke
from test_room_batch import room_fixture


def resolve(workspace):
    _, _, drawing = room_fixture(workspace, units=0)
    s = session(workspace, drawing)
    r = invoke(s, 'resolve_drawing_scope', dict(floor_title='三层平面图', task_ids=['q1']))
    return s, drawing, r['selected_scope']


def test_query_after_resolution_attaches_target_window(workspace):
    s, drawing, scope = resolve(workspace)
    assert scope['object_workflow']['anchor_count'] == 0
    result = invoke(s, 'query_entities', dict(query='试点房间', frame_id=scope['frame_id'], task_ids=['q1']))
    assert result['target_window_count'] >= 1
    call = result['target_window_calls'][0]
    page = invoke(s, call['tool'], call['arguments'] | {'task_ids':['q1']})
    assert page['window']['role'] == 'native_text_anchor'
    assert page['scope_binding']['scope_id'] == scope['scope_id']
    assert page['window']['bbox'] != scope['bbox']
    assert page['review_calls'][0]['tool'] == 'read_geometry'
    assert any(c['tool'] == 'read_geometry' for c in pending_scope_calls(s.task.data))
    assert all(not r['fulfilled'] for r in page['operation_receipts'])


def test_native_pairs_do_not_use_bbox_overlap_or_close_open_paths():
    a = rec('a', [[0,0],[4,0],[0,4],[0,0]])
    # Bounding boxes overlap, but the two triangular source faces do not.
    b = rec('b', [[4,4],[4,1],[1,4],[4,4]])
    rows, coverage = candidate_pairs([a,b],[-1,-1,5,5],set())
    assert len(rows) == 1 and rows[0]['raw_overlap_area'] == 0
    assert rows[0]['raw_edge_distance'] == pytest.approx(2**-.5)
    b['paths'][0] = b['paths'][0][:-1]
    rows, _ = candidate_pairs([a,b],[-1,-1,5,5],set())
    assert rows[0]['raw_overlap_area'] is None
    assert rows[0]['overlap_status'] == 'unresolved_open_geometry'
    assert rows[0]['identity_status'] == 'unconfirmed'
    assert rows[0]['measure_call']['arguments']['entity_ids'] == ['a','b']


def test_actual_closed_paths_report_raw_overlap_without_object_claim():
    a = rec('a',[[0,0],[4,0],[4,4],[0,4],[0,0]])
    b = rec('b',[[2,2],[6,2],[6,6],[2,6],[2,2]])
    rows, _ = candidate_pairs([a,b],[-1,-1,7,7],set())
    assert rows[0]['raw_overlap_area'] == 4
    assert rows[0]['raw_edge_distance'] == 0
    assert rows[0]['identity_status'] == 'unconfirmed'
    assert 'object_pair_identity_unconfirmed' in rows[0]['diagnostics']
    assert not candidate_pairs([a,b],[-1,-1,7,7],{'a'})[0]


def test_render_failure_preserves_guard_and_returns_in_scope_children(workspace):
    s, _, scope = resolve(workspace)
    bounds = scope['bbox']; oversized = [bounds[0]-.1,bounds[1]-.1,bounds[2]+.1,bounds[3]+.1]
    result = {'error': {'code':'INVALID_TOOL_INPUT','message':'SCOPE_BOUNDS：调用范围超出已选定平面。'}}
    s.trace.append(dict(task_ids=['q1']))
    record_scope_failure(s,'render_region',dict(layout_id=scope['layout_id'],bbox=oversized),result)
    assert result['error']['code'] == 'INVALID_TOOL_INPUT'
    recovery = result['recovery']
    assert recovery['search_bbox'] == bounds and len(recovery['next_calls']) == 2
    flow = s.task.data['drawing_scopes'][scope['scope_id']]['object_workflow']
    for call in recovery['next_calls']:
        w = next(w for w in flow['windows'] if w['window_id']==call['arguments']['window_id'])
        assert w['bbox'] != bounds
        assert bounds[0] <= w['bbox'][0] < w['bbox'][2] <= bounds[2]
        assert bounds[1] <= w['bbox'][1] < w['bbox'][3] <= bounds[3]
    assert 'operation_receipts' not in result
    stale = {'error': {'message':'SCOPE_STALE：来源改变'}}
    record_scope_failure(s,'render_region',dict(layout_id=scope['layout_id'],bbox=bounds),stale)
    assert 'recovery' not in stale


def test_unknown_unit_preserves_raw_distance_and_endpoint_evidence(workspace):
    _, _, drawing = room_fixture(workspace,units=0)
    geometry = GeometryService(workspace,drawing['id'])
    records = [r for r in geometry.index.data['entities'] if r['kind']=='LINE' and r['status']=='ready']
    args = GeometryMeasure(operation='distance',entity_ids=[r['entity_id'] for r in records[:2]])
    result = geometry.measure(args)
    assert result['status'] == 'unresolved' and result['value'] is None
    assert result['raw_value'] >= 0 and result['raw_value_unit'] == 'drawing_units'
    assert len(result['measurement_line']) == 2
    assert 'unknown_units' in result['diagnostics']
    assert geometry.summary(result)['raw_value'] == result['raw_value']
    assert result['identity_status'] == 'source_geometry_only'


def test_catalog_has_executable_pagination(workspace):
    s, _, scope = resolve(workspace)
    result = invoke(s,'inspect_scope_objects',dict(scope_id=scope['scope_id'],task_ids=['q1']))
    more = next(c for c in result['next_calls'] if c['arguments'].get('offset') == result['next_offset'])
    page = invoke(s,more['tool'],more['arguments'] | {'task_ids':['q1']})
    assert {r['window_id'] for r in page['items']}.isdisjoint(r['window_id'] for r in result['items'])


def test_literal_mm_policy_applies_without_mutating_source_or_raw_record(workspace):
    _, _, drawing = room_fixture(workspace,units=0)
    geometry = GeometryService(workspace,drawing['id'])
    entity = next(r for r in geometry.index.data['entities'] if r['kind']=='LINE' and r['status']=='ready')
    args = GeometryMeasure(operation='length',entity_ids=[entity['entity_id']])
    raw = geometry.measure(args)
    policy = {'unit':'mm','condition':'explicit','quote':'本次按毫米测量'}
    measured = geometry.measure(args,unit_policy=policy)
    assert measured['status']=='measured' and measured['value']==pytest.approx(raw['raw_value']*.001)
    assert measured['unit_basis']=='user_instruction' and measured['unit_status']=='assumption'
    assert measured['source_unit']=='unknown' and measured['unit_resolution']['policy']==policy
    assert measured['measurement_id'] != raw['measurement_id']
    assert geometry.get(raw['measurement_id'])['status']=='unresolved'
    assert workspace.store.get('drawing',drawing['id'])['unit']=='unknown'
    assert geometry.measure(args,unit_policy=policy)['measurement_id']==measured['measurement_id']


def test_conditional_no_dimensions_does_not_authorize_entity_conversion(workspace):
    _, _, drawing = room_fixture(workspace,units=0)
    geometry = GeometryService(workspace,drawing['id'])
    entity = next(r for r in geometry.index.data['entities'] if r['kind']=='LINE' and r['status']=='ready')
    result = geometry.measure(GeometryMeasure(operation='length',entity_ids=[entity['entity_id']]),
        unit_policy={'unit':'mm','condition':'no_dimensions','quote':'没有尺寸时按毫米'})
    assert result['status']=='unresolved' and result['value'] is None and result['raw_value']>=0
    assert 'unit_policy_condition_not_verified' in result['diagnostics']


def test_known_source_unit_conflict_keeps_physical_value_unresolved(workspace):
    _, _, drawing = room_fixture(workspace,units=4)
    geometry = GeometryService(workspace,drawing['id'])
    entity = next(r for r in geometry.index.data['entities'] if r['kind']=='LINE' and r['status']=='ready')
    args = GeometryMeasure(operation='length',entity_ids=[entity['entity_id']])
    result = geometry.measure(args,unit_policy={'unit':'m','condition':'explicit','quote':'本次按米测量'})
    assert result['status']=='unresolved' and result['value'] is None
    assert result['unit_status']=='conflict' and result['source_unit']=='mm'
    assert 'unit_policy_conflicts_with_source' in result['diagnostics']
    # A fallback assumption does not override the already known source unit.
    fallback = geometry.measure(args,unit_policy={'unit':'m','condition':'unknown_units','quote':'单位未知时按米'})
    assert fallback['status']=='measured' and fallback['applied_unit']=='mm'
    assert fallback['unit_basis']=='dxf_header'


def test_crossing_native_sides_have_local_nearest_points_not_cropped_geometry():
    a = rec('a',[[-10,1],[20,1]],kind='LINE')
    b = rec('b',[[2,4],[8,4]],kind='LINE')
    pairs,_ = candidate_pairs([a,b],[0,0,10,10],set())
    assert pairs and pairs[0]['raw_edge_distance']==3
    assert pairs[0]['parallel_native_edges']
    assert all(0<=p[0]<=10 and 0<=p[1]<=10 for p in pairs[0]['nearest_points'])
    assert a['paths'][0]==[[-10,1],[20,1]]


def test_dense_pair_crop_shrinks_around_actual_source_points():
    from cad_agent.analysis.scope_objects import pair_render_call
    pair={'nearest_points':[[4500,4000],[4500,4300]],'raw_edge_distance':300,'entity_ids':['a','b']}
    result=pair_render_call({'layout_id':'L'},[0,0,10000,10000],pair)
    assert result['arguments']['bbox']==[3700,3200,5300,5100]
    assert result['arguments']['highlight_entity_ids']==['a','b']


def test_explicit_legend_in_native_enclosure_is_reference_not_placed_object():
    from cad_agent.analysis.scope_objects import legend_context
    border=rec('legend-border',[[0,0],[10,0],[10,10],[0,10],[0,0]])
    anchors=[{'region_id':'target','point':[5,5]}]
    titles=[{'region_id':'legend-title','text':'其它图例：','point':[1,9]}]
    context=legend_context([border],anchors,titles,set())
    assert context[0]['status']=='legend_reference_candidate'
    assert context[0]['anchor_region_ids']==['target']
    assert context[0]['border_entity_id']=='legend-border'
    assert not legend_context([border],anchors,titles,{'legend-border'})
    assert not legend_context([border],anchors,[dict(titles[0],point=[20,9])],set())
    opened=dict(border,paths=[border['paths'][0][:-1]])
    assert not legend_context([opened],anchors,titles,set())
