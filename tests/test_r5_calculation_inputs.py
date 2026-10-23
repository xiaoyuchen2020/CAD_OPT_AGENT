"""Already-read operand inventory and actionable, source-bound calculation plans."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from cad_agent.analysis.calculation_workflow import refresh_calculation_inputs, pending_calculations, record_calculation
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts, calculation_tokens


def ledger(question='逐区计算东库和西库的面积/车位数。'):
    return dict(question=question,subtasks=[dict(id='q1',requirement=question,operations=['arithmetic'],
        status='pending',evidence_ids=['east','west'],observation_ids=[],operation_receipts=[])],
        evidence={'east':dict(quote='东库面积100㎡，停车数5辆',snapshot_id='s',frame_id='f'),
                  'west':dict(quote='西库面积180㎡，停车数6辆',snapshot_id='s',frame_id='f')},observations={})


def session(data):
    return SimpleNamespace(request=SimpleNamespace(question=data['question']),evidence=data['evidence'],observations=data['observations'])


def test_ready_calls_execute_and_advance_without_implicit_receipts():
    data=ledger();before=deepcopy(data['subtasks'])
    context=refresh_calculation_inputs(data)['q1']
    assert context['status']=='ready_to_execute' and len(context['next_calls'])==2
    assert data['subtasks']==before and not data.get('calculation_workflows')
    assert pending_calculations(data)[0]['next_calls']
    results=[]
    for call in context['next_calls']:
        args=deepcopy(call['arguments']);owners=args.pop('task_ids')
        result=calculate_facts(session(data),CalculateFacts(**args));results.append(result['value'])
        record_calculation(data,owners,'calculate_facts',result)
        data['subtasks'][0]['operation_receipts'].extend(result['operation_receipts'])
    assert results==['20.000','30.000']
    assert not pending_calculations(data)


def test_unowned_values_alternatives_and_partial_layers_never_auto_join():
    examples=[('逐区计算东库和西库的面积/车位数。','东库面积100㎡，西库停车数5辆'),
              ('逐区计算东库和西库的面积/车位数。','东库面积100㎡，停车数5辆或6辆'),
              ('分别计算外墙一和外墙二的砂浆总厚度。','外墙一砂浆10mm，另一种做法砂浆20mm'),
              ('计算每个分区的面积/车位数。','面积100㎡，停车数5辆')]
    for question,quote in examples:
        data=ledger(question);data['evidence']={'east':dict(quote=quote)}
        context=refresh_calculation_inputs(data)['q1']
        assert context['next_calls']==[] and context['missing_operands']
        assert not data.get('calculation_workflows')


def test_competing_duplicate_subject_sources_remain_unresolved():
    data=ledger();data['evidence']['west']=deepcopy(data['evidence']['east'])
    assert refresh_calculation_inputs(data)['q1']['next_calls']==[]


def test_area_m_is_not_square_metres_and_ids_are_not_operands():
    tokens=list(calculation_tokens('面积154.05m；4#-3/4防火分区；5厚砂浆；FM1122；型号123'))
    area=next(t for t in tokens if t['value']=='154.05')
    assert area['unit']=='m' and area['status']=='unit_conflict'
    assert any(t['value']=='4' and t['status']=='identifier' for t in tokens)
    assert next(t for t in tokens if t['value']=='5')['status']=='unit_unconfirmed'
    # Shared readings retain model digits for auditing, never as operands.
    assert next(t for t in tokens if t['value']=='1122')['status']=='identifier'
    assert next(t for t in tokens if t['value']=='123')['status']=='identifier'


def test_user_conditions_measurements_and_prior_results_keep_provenance():
    data=ledger('用户给定参考35㎡/车。请计算各区面积/车位数与参考差值。')
    data['subtasks'][0]['observation_ids']=['measure','unknown']
    data['observations']={
        'measure':dict(tool='measure_room_area',data=dict(status='needs_review',candidate_area_m2=12,measurement_basis='candidate_polygon',object_id='polygon')),
        'unknown':dict(tool='measure_geometry',data=dict(status='unknown',value=5,unit='m'))}
    context=refresh_calculation_inputs(data)['q1']
    user=[r for r in context['operands'] if r['quantity']['origin']=='user_input']
    measurement=[r for r in context['operands'] if r['quantity']['origin']=='measurement']
    assert len(user)==1 and user[0]['conditional'] and user[0]['unit']=='m2/count'
    assert len(measurement)==1 and measurement[0]['status']=='candidate_numeric'
    assert measurement[0]['binding']['object_id']=='polygon'
    assert not context['next_calls']


def test_dependency_sources_are_visible_but_other_tasks_are_not_borrowed():
    data=ledger();data['subtasks'][0]['evidence_ids']=[]
    data['subtasks'][0]['requirement']='计算总厚度'
    data['subtasks'].append(dict(id='read',requirement='读取厚度',operations=[],status='pending',evidence_ids=['east'],observation_ids=[]))
    assert refresh_calculation_inputs(data)['q1']['operand_count']==0
    data['subtasks'][0]['depends_on']=['read']
    assert refresh_calculation_inputs(data)['q1']['operand_count']==2


def test_inventory_is_bounded_and_never_hides_truncation():
    data=ledger('计算总和');data['evidence']={'east':dict(quote='；'.join(f'长度{i}mm' for i in range(140)))}
    context=refresh_calculation_inputs(data)['q1']
    assert context['operand_count']==140 and len(context['operands'])==128 and context['inventory_truncated']
    wire=pending_calculations(data)[0]['input_context']
    assert len(wire['operands'])==12 and wire['omitted_operands'] and wire['inventory_truncated']


ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'artifacts/designer-facts-b-final-20261019'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_replay():
    """Validate immutable source spans, then refresh copied final-B ledgers."""
    hashes={str(p.relative_to(ROOT)):digest(p) for p in [FROZEN/'review.json',FROZEN/'arithmetic-checks.json']}
    sources={};cases=[]
    for path in sorted((FROZEN/'attempts').glob('*/0001/task.json')):
        data=json.loads(path.read_text());hashes[str(path.relative_to(ROOT))]=digest(path)
        for evidence in data['evidence'].values():
            sid=evidence['snapshot_id']
            if sid not in sources:
                folder=FROZEN/'inputs'/sid
                drawing=json.loads((folder/'drawing.json').read_text())
                regions={r['region_id']:r for r in json.loads((folder/'regions.json').read_text())}
                assert digest(folder/'source.dxf')==drawing['sha256']
                for name in ('source.dxf','drawing.json','regions.json'):
                    p=folder/name;hashes[str(p.relative_to(ROOT))]=digest(p)
                sources[sid]=regions
            region=sources[sid][evidence['region_id']]
            assert region['text'][slice(*evidence['text_span'])]==evidence['quote']
            assert set(evidence['entity_ids'])<=set(region['entity_ids'])
        before=deepcopy(data['subtasks']);inventory=refresh_calculation_inputs(data)
        assert data['subtasks']==before
        cases.append(dict(case=path.parents[1].name,source_spans_revalidated=len(data['evidence']),
            baseline_calculate_facts_calls=sum(o['tool']=='calculate_facts' for o in data['observations'].values()),
            calculation_inputs=inventory,executed_calls=0))
    assert all(digest(ROOT/path)==sha for path,sha in hashes.items())
    return dict(evidence_class='final_B_frozen_source_offline_inventory_replay',live_model_calls=0,
        frozen_inputs_unchanged=True,input_sha256=hashes,cases=cases,
        limitations=['离线输入组织与确定性执行路径验证，不证明模型会主动执行。',
                    '未猜测替代层、未归属数字、工程单位或同对象工程口径。',
                    '候选几何面积与标注数值差不证明工程面积可比。'])


def test_final_b_frozen_replay():
    report=frozen_replay()
    assert len(report['cases'])==20 and report['live_model_calls']==0
    q17=next(c for c in report['cases'] if c['case']=='Q17-B')
    rows=[r for v in q17['calculation_inputs'].values() for r in v['operands']]
    assert any(r['value']=='154.05' and r['status']=='unit_conflict' for r in rows)
    assert all(not v['next_calls'] for v in q17['calculation_inputs'].values())


def test_frozen_q17_supported_superscript_and_candidate_area_are_numeric_only():
    data=json.loads((FROZEN/'attempts/Q17-B/0001/task.json').read_text())
    oid,obs=next((key,value) for key,value in data['observations'].items() if value['tool']=='select_room_boundaries')
    row=obs['data']['measurement']['items'][0]
    quantities=[dict(origin='measurement',observation_id=oid,measurement_ref=row['region_id'],field=field,
                     value=value,unit='m2') for field,value in [('candidate_area_m2','136.8504'),('annotation_area_m2','154.05')]]
    result=calculate_facts(session(data),CalculateFacts(operation='subtract',quantities=quantities,decimal_places=4))
    assert result['value']=='-17.1996' and result['candidate_inputs']
    assert result['relation_status']=='independent_numeric' and result['target_binding_status']=='unconfirmed'
    assert result['basis_comparability']=='unconfirmed' and result['semantic_difference'] is None
    unresolved=calculate_facts(session(data),CalculateFacts(operation='subtract',quantities=quantities,operand_relation='same_object'))
    assert unresolved['status']=='missing_operands' and 'value' not in unresolved
    rows=[r for c in refresh_calculation_inputs(data).values() for r in c['operands']]
    assert any(r['quantity'].get('field')=='annotation_area_m2' and r['unit']=='m2' for r in rows)
    bad=deepcopy(data);bad['observations'][oid]['data']['measurement']['items'][0]['area_annotation_checks'][0]['status']='candidate'
    import pytest
    with pytest.raises(ValueError,match='复合单位'):
        calculate_facts(session(bad),CalculateFacts(operation='subtract',quantities=quantities))
