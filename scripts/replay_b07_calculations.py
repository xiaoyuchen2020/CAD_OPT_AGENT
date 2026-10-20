"""Replay arithmetic against immutable repaired-evaluation source spans; no LLM."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts

FROZEN = ROOT / 'artifacts/designer-facts-repaired-20261019'


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def replay(case):
    review=FROZEN/'review-inputs'/f'{case}.json'
    data=json.loads(review.read_text())
    evidence={e['evidence_id']:e for e in data['citations']}
    snapshots=sorted({e['snapshot_id'] for e in evidence.values()})
    paths=[review,FROZEN/'review.json']
    source_sha={}
    for sid in snapshots:
        folder=FROZEN/'inputs'/sid
        drawing=json.loads((folder/'drawing.json').read_text())
        regions={r['region_id']:r for r in json.loads((folder/'regions.json').read_text())}
        source_sha[sid]=sha(folder/'source.dxf')
        assert source_sha[sid]==drawing['sha256']
        paths.extend(folder/name for name in ('source.dxf','drawing.json','regions.json'))
        for e in evidence.values():
            if e['snapshot_id']!=sid:continue
            r=regions[e['region_id']]
            assert r['text'][slice(*e['text_span'])]==e['quote']
            assert set(e['entity_ids'])<=set(r['entity_ids'])
    hashes={str(p.relative_to(ROOT)):sha(p) for p in paths}
    # Add the verified frozen source hash to the replay's copies, never to the
    # archived evidence files. This binds resulting lineage to immutable input.
    evidence={key:dict(e,source_sha256=source_sha[e['snapshot_id']]) for key,e in evidence.items()}
    session=SimpleNamespace(evidence=evidence,observations={},request=SimpleNamespace(question=data['question']))
    results=[];rejected=[]
    def calc(**args):
        r=calculate_facts(session,CalculateFacts(**args));results.append(dict(arguments=args,result=r));return r
    def reject(**args):
        try:calculate_facts(session,CalculateFacts(**args))
        except ValueError as error:rejected.append(dict(arguments=args,error=str(error)))
        else:raise AssertionError('Unsafe binding unexpectedly accepted')
    def source(e,value,unit=''):
        return dict(origin='source',evidence_id=e,value=value,unit=unit)
    if case=='Q06-A':
        eid=next(k for k,e in evidence.items() if e['quote'].startswith('-外墙涂料系统') and '10厚DWM15' in e['quote'])
        reject(operation='sum',quantities=[source(eid,'5','mm'),source(eid,'10','mm')],target='涂料外墙')
        result=calc(operation='sum',calculation_key='outer_wall_mortar',target='两种涂料外墙逐套砂浆总厚度',
            operand_relation='same_object',quantities=[],missing_operands=[
                dict(label='外墙做法一的完整砂浆层序、厚度单位及所属编号',reason='原文含5厚与10厚，但未确认完整层序、所属做法及毫米单位依据。'),
                dict(label='外墙做法二的完整砂浆层序、厚度单位及所属编号',reason='同名重复图示尚未完成逐套来源绑定，不能借用另一套或内墙层值。')])
        assert result['status']=='missing_operands' and 'value' not in result
        boundary='仅拒绝未绑定单位/做法的求和并逐套列缺项；不证明原图缺少厚度，也不证明已完成做法覆盖。'
    elif case=='Q12-B':
        eid=next(k for k,e in evidence.items() if e['quote'].strip()=='270辆。')
        result=calc(operation='divide',calculation_key='unbound_zone_ratio',target='待确认机动车库防火分区',
            operand_relation='same_object',quantities=[source(eid,'270','count')],missing_operands=[
                dict(label='同一机动车库分区面积',unit='m2',reason='已读分区编号未绑定对应面积。'),
                dict(label='该分区停车数量的统计归属',unit='count',reason='270辆的对象、表头和适用范围尚未确认；不能认作该区或全库分母。')])
        assert result['status']=='missing_operands' and result['inputs'][0]['binding']['quote']=='270辆。'
        # A source identifier with nearby numbers cannot be used as an area.
        identifier=next(k for k,e in evidence.items() if e['quote']=='3#-1/2防火分区')
        reject(operation='divide',quantities=[source(identifier,'3','m2'),source(eid,'270','count')])
        boundary='270辆仅保留为未归属原文操作数；各区配对、去重、面积与数量全集均未完成，不计算或猜测分母。'
    else:
        assert case=='Q16-B'
        claims=[c for c in data['claims'] if c.get('calculations')]
        pairs=[c for claim in claims for c in claim['calculations']]
        assert len(pairs)==3
        for index,pair in enumerate(pairs):
            operands=[source(v['evidence_id'],v['value']) for v in pair['operands']]
            r=calc(operation='subtract',calculation_key=f'annotation_numbers_{index}',target='独立标注数字差',quantities=operands)
            assert r['value']=='1.500' and r['unit']=='' and r['target_binding_status']=='unconfirmed'
            reject(operation='subtract',quantities=[dict(q,unit='m') for q in operands],target='覆土深度')
        # The completed numeric difference does not close the separately requested
        # same-position calculation. The missing identity is explicit and local.
        r=calc(operation='subtract',calculation_key='same_position_soil_depth',target='室外地坪至顶板覆土深度',
            quantities=[source(v['evidence_id'],v['value']) for v in pairs[0]['operands']],operand_relation='same_object')
        assert r['status']=='missing_operands'
        session.observations['difference']=dict(tool='calculate_facts',data=results[0]['result'])
        reject(operation='subtract',quantities=[dict(origin='calculation',observation_id='difference',value='1.500',unit=''),
            dict(origin='user_input',question_quote='覆土1.5m',value='1.5',unit='m')])
        boundary='三组数字差1.500可算；标高单位、基准、表面、同部位关系未确认，不能提升为覆土深度或与1.5m工程量比较。'
    assert all(sha(ROOT/path)==digest for path,digest in hashes.items())
    return dict(case=case,evidence_class='frozen_repaired_source_span_arithmetic_replay',
                live_model_calls=0,source_extraction_rerun=False,input_sha256=hashes,
                source_spans_revalidated=len(evidence),inputs_unchanged=True,results=results,
                rejected_calls=rejected,boundary=boundary,engineering_semantics_confirmed=False,
                e2e_quality_evaluated=False,budget_changed=False)


def main():
    out=ROOT/'artifacts/bottleneck-repairs-20261019/b07-replay.json'
    out.parent.mkdir(parents=True,exist_ok=True)
    report=dict(cases=[replay(case) for case in ('Q06-A','Q12-B','Q16-B')],
                limitations=['离线确定性工具回放；不调用模型、不重跑E2E，不证明模型主动选对操作数。',
                             'Q17候选多边形仍可算术，但候选边界和工程量身份不因此得到确认。'])
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(out)

if __name__=='__main__':main()
