"""Public tool pipeline: native source -> scoped object -> method -> delivery."""
import json
from copy import deepcopy

import pytest

from cad_agent.analysis.delivery import AnswerValidationError
from cad_agent.analysis.scheduling import schedule
from cad_agent.analysis.tasks import TaskLedger
from cad_agent.analysis.tool_metadata import SPATIAL_METHODS
from cad_agent.analysis.tool_registry import TOOL_SPECS, MUTATING_TOOLS
from cad_agent.geometry.service import GeometryService
from test_round3_spatial_identity import setup_object
from test_remediation_scope import invoke


def bound_native_object(workspace):
    from test_remediation_scope import session as make_session
    from test_room_batch import room_fixture
    _, _, drawing = room_fixture(workspace, units=4)
    session = make_session(workspace, drawing, question='测量三层平面图中所选原生闭合路径的面积和周长。')
    session.task.data['subtasks'][0]['operations'] = ['area', 'perimeter']
    resolved = invoke(session, 'resolve_drawing_scope',
                      dict(floor_title='三层平面图', task_ids=['q1']))
    assert 'error' not in resolved, resolved
    scope = session.task.data['drawing_scopes'][resolved['selected_scope']['scope_id']]
    service = GeometryService(workspace, session.request.snapshot_id)
    records = list(service.index.by_id.values())
    inner = [r['entity_id'] for r in records if r['kind'] == 'LINE'
             and r['bbox'][0] >= 0 and r['bbox'][1] >= 0
             and r['bbox'][2] <= 6000 and r['bbox'][3] <= 4000]
    assert len(inner) == 4
    read = invoke(session, 'read_geometry', {'entity_ids': inner, 'task_ids': ['q1']})
    assert 'error' not in read, read
    result = invoke(session, 'bind_spatial_object', {
        'scope_id': scope['scope_id'], 'entity_ids': inner,
        'role': 'source_geometry', 'task_ids': ['q1']})
    assert 'error' not in result, result
    return session, result['object_id']


def measure_native(workspace):
    s, oid = bound_native_object(workspace)
    boundary = invoke(s, 'inspect_spatial_boundary', {'object_id': oid, 'task_ids': ['q1']})
    assert 'error' not in boundary, boundary
    assert len(boundary['contours']) == 1
    args = boundary['next_calls'][0]['arguments']
    measured = invoke(s, 'measure_spatial_area', args | {'task_ids': ['q1']})
    assert 'error' not in measured, measured
    return s, oid, args, measured


def test_native_public_tool_chain_persists_receipts_and_citable_values(workspace):
    s, oid, _, result = measure_native(workspace)
    assert result['measurements']['area_m2'] == pytest.approx(24)
    assert result['measurements']['perimeter_m'] == pytest.approx(20)
    assert result['spatial_fact_scope']['engineering_confirmed'] is False
    saved = TaskLedger.read(workspace, s.task.data['task_id'])
    assert saved['spatial_objects'][oid]['measurement_evidence']
    assert saved['spatial_results'][-1]['measurements']['area_m2'] == pytest.approx(24)
    assert result['observation_id'] in saved['observations']
    assert {'area', 'perimeter'} <= {
        r['operation'] for r in saved['subtasks'][0]['operation_receipts'] if r['fulfilled']}
    claim = dict(text='选定原生闭合路径面积为24平方米、周长20米，工程身份和净边界未确认。',
                 state='derived', evidence_basis='tool_observation',
                 question_part_ids=['q1'], observation_ids=[result['observation_id']])
    outcomes = [dict(requirement_id=p['id'], status='partial', gap_kind='ambiguous_scope',
                     reason='仅测得所选源闭合路径，工程身份和净边界尚未确认。')
                for p in s.task.parts]
    s.validate_answer(json.dumps({'claims': [claim], 'requirement_outcomes': outcomes}, ensure_ascii=False))
    for unit in ('m2', 'M2', 'm²', '㎡'):
        ascii_claim = claim | {'text': f'选定原生闭合路径面积为24{unit}，工程身份未确认。'}
        s.validate_answer(json.dumps({'claims': [ascii_claim], 'requirement_outcomes': outcomes}, ensure_ascii=False))
    with pytest.raises(AnswerValidationError):
        s.validate_answer(json.dumps({'claims': [claim | {'text': '已确认房间净面积为24平方米。', 'state': 'observed'}],
                                     'requirement_outcomes': outcomes}, ensure_ascii=False))
    # The limited, explicitly requested source-path facts can be delivered;
    # engineering identity is not a prerequisite invented by the scheduler.
    outcome = dict(requirement_id='q1', status='fulfilled', gap_kind='none',
                   reason='已计算指定原生闭合路径的面积与周长，未确认工程身份。')
    finished = s.finish(json.dumps({'claims': [claim], 'requirement_outcomes': [outcome]}, ensure_ascii=False))
    assert finished['task']['status'] == 'completed'


def test_same_parameters_revalidate_policy_instead_of_reusing_old_measurement(workspace):
    s, oid, args, result = measure_native(workspace)
    count = len(s.task.data['spatial_objects'][oid]['measurement_evidence'])
    rows = deepcopy(s.task.data['spatial_results'])
    s.task.data['unit_policy'] = {'unit': 'm', 'condition': 'explicit'}
    again = invoke(s, 'measure_spatial_area', args | {'task_ids': ['q1']})
    assert 'error' in again and 'SPATIAL_STALE' in json.dumps(again)
    assert len(s.task.data['spatial_objects'][oid]['measurement_evidence']) == count
    assert s.task.data['spatial_results'] == rows
    assert result['observation_id'] in s.observations


def test_area_prerequisites_remain_executable_after_expansion_cap(workspace):
    s, _ = setup_object(workspace)
    s.task.data['subtasks'][0]['operations'] = ['area']
    s.focus.expansions['q1'] = 2
    work = [item for item in schedule(s) if item['operation'] == 'area']
    assert work and work[0]['executable']
    needed = {'bind_spatial_object', 'inspect_spatial_boundary', 'measure_spatial_area'}
    assert needed <= set(work[0]['next_action']['tools'])
    assert needed <= s.focus.names()
    assert SPATIAL_METHODS <= set(TOOL_SPECS) & MUTATING_TOOLS


def test_spatial_delivery_keeps_units_and_conditions(workspace):
    from cad_agent.analysis.spatial_delivery import spatial_measurement_issues
    from cad_agent.analysis.tool_registry import Claim
    def claim(text):
        return Claim(text=text, state='derived')
    data = dict(kind='spatial_method', spatial_fact_scope={'identity_confirmed': False},
                unit={'unit': 'unknown'}, measurements={'area_drawing_units_squared': 24})
    assert spatial_measurement_issues(claim('选定路径面积24平方米，身份未确认。'), [data])
    assert spatial_measurement_issues(claim('选定路径面积24M2，身份未确认。'), [data])
    assert not spatial_measurement_issues(claim('选定路径面积24原图单位平方，身份未确认。'), [data])
    data.update(unit={'unit': 'mm'}, measurements={'area_m2': 24, 'conditional': True})
    assert spatial_measurement_issues(claim('选定路径面积24平方米，身份未确认。'), [data])
    assert not spatial_measurement_issues(claim('按用户单位约定，选定路径面积24平方米，身份未确认。'), [data])


def test_section_scope_cannot_enter_room_pipeline(workspace):
    from cad_agent.analysis.scope_workflow import selected_for_call
    s, scope = setup_object(workspace)
    scope['view_kind'] = 'section'
    with pytest.raises(ValueError, match='SCOPE_VIEW'):
        selected_for_call(s, 'analyze_room_batch')
    assert selected_for_call(s, 'inspect_spatial_boundary')['view_kind'] == 'section'
