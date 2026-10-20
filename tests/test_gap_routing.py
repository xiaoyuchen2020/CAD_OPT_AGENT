"""Recovery advice is grounded, bounded, capability checked and non-authoritative."""
from copy import deepcopy
from types import SimpleNamespace

from cad_agent.analysis.gap_routing import record_result, refresh, context, pending_calls
from cad_agent.analysis.scheduling import schedule
from cad_agent.analysis.tool_registry import TOOL_SPECS


def fake_session(provider='responses_subscription'):
    data = dict(subtasks=[dict(id='q1', kind='lookup', status='pending', calls=0, stalled=0,
                              depends_on=[], delivered=False)], evidence={})
    return SimpleNamespace(task=SimpleNamespace(data=data, pending_work=lambda: [
        dict(operation='read_requested_fact', task_ids=['q1'])]),
        trace=[], calls=0, room_batches={}, observations={},
        request=SimpleNamespace(runtime='responses_subscription', source_read_batching=False),
        policy=SimpleNamespace(stalled=0, stop_reason=None),
        w=SimpleNamespace(settings=SimpleNamespace(provider=provider, agent_max_tool_calls=40)))


def observe(s, tool, args, result):
    s.trace.append(dict(tool=tool, arguments=args, task_ids=['q1']))
    record_result(s, tool, ['q1'], result)
    return list(s.task.data.get('gap_recovery', {}).values())


def identity():
    return dict(object_id='object-a', layout_id='layout-a',
                geometry=dict(entity_ids=['entity-a'], bbox=[0., 0., 10., 10.]),
                identity=dict(engineering_confirmed=False))


def test_header_reads_known_table_and_requires_actual_header_evidence():
    s = fake_session()
    gap, = observe(s, 'read_table', {'table_id': 'table-a'},
                   dict(table_id='table-a', header_state='unresolved'))
    rec = gap['recommendation']
    assert rec['tool'] == 'read_table_semantics' and rec['schema_valid']
    TOOL_SPECS[rec['tool']][0].model_validate(rec['arguments'], strict=True)
    observe(s, rec['tool'], rec['arguments'], dict(table_id='table-a', header_state='candidate', header_rows=[0]))
    assert gap['status'] == 'open' and len(gap['attempts']) == 1
    observe(s, 'read_table_semantics', {'table_id': 'table-b'},
            dict(table_id='table-b', header_rows=[0], items=[{'cells': [{'is_header': True, 'evidence': [{'evidence_id': 'b'}]}]}]))
    assert gap['status'] == 'open'
    observe(s, 'read_table_semantics', {'table_id': 'table-a', 'row_limit': 2},
            dict(table_id='table-a', header_rows=[0], items=[{'cells': [{'is_header': True, 'evidence': [{'evidence_id': 'a'}]}]}]))
    assert gap['status'] == 'resolved'
    assert not s.task.data['subtasks'][0]['delivered']


def test_cross_drawing_header_recovery_preserves_target_snapshot():
    s = fake_session()
    s.request.snapshot_id = 'plan'
    gap, = observe(s, 'read_door_schedule_table', {'snapshot_id': 'schedule', 'table_id': 't', 'row_offset': 3},
                   dict(snapshot_id='schedule', table_id='t', header_state='unresolved'))
    rec = gap['recommendation']
    assert rec['tool'] == 'read_door_schedule_table'
    assert rec['arguments']['snapshot_id'] == 'schedule'
    header = dict(table_id='t', header_rows=[0], items=[dict(cells=[dict(is_header=True, evidence=[{'evidence_id':'header'}])])])
    observe(s, 'read_table_semantics', {'table_id': 't'}, header | {'snapshot_id':'plan'})
    assert gap['status'] == 'open'
    observe(s, rec['tool'], rec['arguments'], header | {'snapshot_id':'schedule'})
    assert gap['status'] == 'resolved'


def test_native_then_local_visual_no_automatic_replay_or_confirmation():
    s = fake_session()
    gap, = observe(s, 'bind_spatial_object', {}, identity())
    first = deepcopy(gap['recommendation'])
    assert first['tool'] == 'read_geometry'
    observe(s, first['tool'], first['arguments'], {'items': []})
    second = gap['recommendation']
    assert second['tool'] == 'render_region' and second['arguments']['bbox'] == [0., 0., 10., 10.]
    assert second['schema_valid'] and not second['automatic_replay']
    observe(s, second['tool'], second['arguments'], {'image_id': 'image-a', 'status': 'success'})
    assert gap['status'] == 'open' and gap['recommendation'] is None
    assert gap['stop_reason'] == 'no_untried_source_action'


def test_runtime_drift_blocks_visual_without_paid_fallback():
    s = fake_session()
    gap, = observe(s, 'bind_spatial_object', {}, identity())
    rec = gap['recommendation']
    observe(s, rec['tool'], rec['arguments'], {'items': []})
    s.w.settings.provider = 'deepseek'
    refresh(s)
    assert gap['recommendation'] is None and gap['stop_reason'] == 'runtime_not_supported'
    assert not pending_calls(s.task.data)


def test_unit_gap_never_suggests_override_or_confirmation():
    s = fake_session()
    gap, = observe(s, 'read_table_semantics', {'table_id': 't', 'row_offset': 3},
                   dict(table_id='t', items=[dict(cells=[dict(unit_state='unknown')])]))
    assert gap['recommendation']['arguments'] == {'table_id': 't', 'row_offset': 0}
    assert 'unit_override' not in gap['recommendation']['arguments']
    assert gap['status'] == 'open'


def test_unit_resolution_requires_same_cell_explicit_source():
    s = fake_session()
    gap, = observe(s, 'read_table_semantics', {'table_id': 't', 'row_offset': 3},
                   dict(table_id='t', items=[dict(cells=[dict(cell_id='c1', unit_state='unknown')])]))
    observe(s, 'read_table_semantics', {'table_id': 't'},
            dict(table_id='t', items=[dict(cells=[dict(cell_id='c2', unit='mm', unit_state='derived', unit_evidence=[{'evidence_id': 'e'}])])]))
    assert gap['status'] == 'open'
    observe(s, 'read_table_semantics', {'table_id': 't'},
            dict(table_id='t', items=[dict(cells=[dict(cell_id='c1', unit='mm', unit_state='derived', unit_evidence=[{'evidence_id': 'e'}])])]))
    assert gap['status'] == 'resolved'
    assert gap['recommendations']  # advice is retained even after resolution


def test_calculation_retry_requires_fresh_complete_inputs():
    s = fake_session()
    gap, = observe(s, 'calculate_facts', {'calculation_key': 'calc-a'},
                   dict(calculation_key='calc-a', missing_operands=[{'label': '数量'}], status='missing_input'))
    assert gap['status'] == 'open'
    # Exact old partial call is never suggested again.
    assert not gap['recommendation']
    s.task.data['calculation_inputs'] = {'q1': {'next_calls': [dict(tool='calculate_facts',
        arguments=dict(operation='sum', calculation_key='calc-a', task_ids=['q1'],
                       quantities=[dict(origin='user_input', value='2', unit='m', question_quote='给定2m')]))]}}
    refresh(s)
    rec = gap['recommendation']
    assert rec and rec['schema_valid'] and rec['tool'] == 'calculate_facts'
    observe(s, rec['tool'], rec['arguments'], dict(calculation_key='calc-a', status='calculated',
            missing_operands=[], target_binding_status='unconfirmed'))
    assert gap['status'] == 'open'

def test_real_single_side_diagnostic_routes_record_relations():
    s = fake_session()
    gap, = observe(s, 'query_building_relations', {},
        dict(record_id='record-a', layout_id='layout-a', items=[
            dict(object_id='opening-a', bbox=[0., 0., 10., 10.], passage_status='not_established',
                 connection_status='unresolved', diagnostics=['two_sided_space_connection_unresolved'])]))
    rec = gap['recommendation']
    assert rec['arguments'] == {'record_id': 'record-a', 'view': 'relations'}
    assert rec['schema_valid']
    observe(s, rec['tool'], rec['arguments'],
            dict(record_id='record-a', items=[dict(object_id='other', passage_status='supported_door_between_two_spaces')]))
    assert gap['status'] == 'open'  # another opening in the same component does not close this gap
    observe(s, 'get_component_evidence', {'record_id': 'record-a', 'view': 'openings'},
            dict(record_id='record-a', items=[dict(object_id='opening-a', passage_status='supported_door_between_two_spaces')]))
    assert gap['status'] == 'resolved'


def test_missing_parameters_are_explicit_not_fake_ids():
    s = fake_session()
    gap, = observe(s, 'read_table', {}, {'header_state': 'unresolved'})
    rec = gap['recommendation']
    assert not rec['schema_valid'] and rec['arguments_required']
    assert 'table_id' in rec['required_parameters'] and 'table_id' not in rec['arguments']


def test_budget_and_attempt_limits_preserve_ledger_counters():
    s = fake_session()
    gap, = observe(s, 'bind_spatial_object', {}, identity())
    s.task.data['subtasks'][0]['calls'] = 12
    refresh(s)
    assert gap['stop_reason'] == 'subtask_tool_budget' and not gap['recommendation']
    s.task.data['subtasks'][0]['calls'] = 2
    gap['attempts'] = [dict(call_key=str(i)) for i in range(3)]
    refresh(s)
    assert gap['stop_reason'] == 'gap_attempt_limit'
    assert s.task.data['subtasks'][0]['calls'] == 2


def test_error_attempt_does_not_resolve_or_reset_budget():
    s = fake_session()
    gap, = observe(s, 'bind_spatial_object', {}, identity())
    rec = gap['recommendation']
    observe(s, rec['tool'], rec['arguments'], {'error': {'code': 'INVALID'}})
    assert gap['attempts'][0]['outcome'] == 'tool_error' and gap['status'] == 'open'
    assert gap['recommendation']['tool'] == 'render_region'


def test_blocked_owner_does_not_hide_recovery_for_independent_owner():
    s = fake_session()
    s.task.data['subtasks'].append(dict(id='q2', kind='lookup', status='waiting_input',
                                      calls=0, stalled=0, depends_on=[], delivered=False))
    s.trace.append(dict(tool='bind_spatial_object', arguments={}))
    record_result(s, 'bind_spatial_object', ['q1', 'q2'], identity())
    gap, = s.task.data['gap_recovery'].values()
    assert gap['recommendation']['task_ids'] == ['q1']
    assert not gap['stop_reason']


def test_schedule_advice_is_not_a_new_completion_obligation():
    s = fake_session()
    baseline = schedule(s)
    observe(s, 'bind_spatial_object', {}, identity())
    updated = schedule(s)
    assert len(updated) == len(baseline) == 1
    assert updated[0]['next_action'] == baseline[0]['next_action']
    assert updated[0]['recovery_actions'][0]['tool'] == 'read_geometry'
    assert context(s.task.data)[0]['status'] == 'open'


def test_no_new_evidence_stops_without_inventing_query():
    s = fake_session()
    s.policy.stalled = 2
    gap, = observe(s, 'search_sources', {'query': '对象'}, {'items': []})
    assert gap['kind'] == 'no_new_evidence'
    assert gap['stop_reason'] == 'no_known_source_action' and not gap['recommendation']
    s.policy.stalled = 0
    observe(s, 'read_source_context', {'region_id': 'r'},
            {'items': [{'evidence_id': 'ev-new', 'quote': '原文'}]})
    assert gap['status'] == 'resolved'
    assert not s.task.data['subtasks'][0]['delivered']


def test_focus_integration_persists_and_exposes_advice(workspace):
    from test_task_orchestration import session, plan
    s, _ = session(workspace)
    s.task.plan(plan(s))
    s.trace.append(dict(tool='read_table', arguments={'table_id': 't'}, task_ids=['q4']))
    s.focus.record('read_table', ['q4'], {'table_id': 't', 'header_state': 'unresolved'})
    assert 'read_table_semantics' in s.focus.names()
    assert s.focus.state()['gap_recovery'][0]['kind'] == 'header_unread'
    saved = workspace.store.get('agent_task', s.task.data['task_id'])
    assert saved['gap_recovery']
    messages = s.focus.messages([{'role': 'user', 'content': 'question'}])
    assert any('"gap_recovery"' in m.get('content', '') for m in messages)
