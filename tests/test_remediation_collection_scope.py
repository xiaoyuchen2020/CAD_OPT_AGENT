"""A saved candidate collection cannot satisfy an unrelated bound plan."""
from copy import deepcopy

import pytest

from cad_agent.analysis.result_sets import AnalyzeMeasurements, analyze
from cad_agent.analysis.scope_workflow import bind_scope_call
from test_remediation_scope import invoke, session
from test_room_batch import room_fixture


def scoped_collection(workspace):
    _, _, drawing = room_fixture(workspace, units=4)
    s = session(workspace, drawing, '计算三层平面图中候选围合的面积。')
    scope = invoke(s, 'resolve_drawing_scope', dict(floor_title='三层平面图', task_ids=['q1']))['selected_scope']
    x, y, right, top = scope['bbox']
    width, height = (right-x)/4, (top-y)/4
    boundary = [[x+width,y+height], [x+2*width,y+height],
                [x+2*width,y+2*height], [x+width,y+2*height]]
    batch = dict(batch_id='bound-batch', snapshot_id=drawing['id'], source_sha256=drawing['sha256'],
                 unit='mm', scope=deepcopy(scope), total=1,
                 items=[dict(number=1, region_id='candidate', highlight=dict(kind='candidate_boundary', boundary=boundary))])
    s.room_batches[batch['batch_id']] = batch
    return s, batch


def test_matching_collection_has_bound_candidate_area(workspace):
    s, batch = scoped_collection(workspace)
    result = invoke(s, 'analyze_measurements', dict(batch_id=batch['batch_id'], task_ids=['q1']))
    assert 'error' not in result, result
    assert result['scope_binding']['frame_id'] == batch['scope']['frame_id']
    assert result['values'][0]['basis'] == 'selected_enclosure'
    assert any(r['operation']=='area' and r['fulfilled'] for r in result['operation_receipts'])


@pytest.mark.parametrize('change,code', [
    ('frame', 'SCOPE_COLLECTION'), ('layout', 'SCOPE_COLLECTION'),
    ('scope', 'SCOPE_COLLECTION'), ('geometry', 'SCOPE_COLLECTION_GEOMETRY'),
    ('hash', 'SCOPE_STALE'), ('snapshot', 'SCOPE_STALE'),
])
def test_collection_scope_checked_before_receipts_and_cache(workspace, change, code):
    s, batch = scoped_collection(workspace)
    args = AnalyzeMeasurements(batch_id=batch['batch_id'])
    # Populate the cache under a valid scope, then invalidate the saved collection.
    assert 'error' not in invoke(s, 'analyze_measurements', dict(batch_id=batch['batch_id'], task_ids=['q1']))
    if change == 'frame': batch['scope']['frame_id'] = 'different-floor-same-extents'
    elif change == 'layout': batch['scope']['layout_id'] = 'other-layout'
    elif change == 'scope': batch['scope'] = None
    elif change == 'geometry': batch['items'][0]['highlight']['boundary'][0] = [-1e12, -1e12]
    elif change == 'hash': batch['source_sha256'] = 'changed'
    elif change == 'snapshot': batch['snapshot_id'] = 'other-snapshot'
    with pytest.raises(ValueError, match=code): bind_scope_call(s, 'analyze_measurements', args)
    with pytest.raises(ValueError, match=code): analyze(s, args)
    result = invoke(s, 'analyze_measurements', dict(batch_id=batch['batch_id'], task_ids=['q1']))
    assert code in result['error']['message']
    assert not result.get('operation_receipts') and not result.get('already_returned')


def test_collection_waits_for_unresolved_owner_scope(workspace):
    s, batch = scoped_collection(workspace)
    invoke(s, 'resolve_drawing_scope', dict(floor_title='不存在的平面图', task_ids=['q1']))
    with pytest.raises(ValueError, match='SCOPE_UNRESOLVED'):
        analyze(s, AnalyzeMeasurements(batch_id=batch['batch_id']))
