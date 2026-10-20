"""Bounded native-dimension chain checking, independent of question wording."""
import math
import re
from decimal import Decimal
from pydantic import Field
from typing import Literal
from cad_agent.components.contracts import DimensionQuery
from cad_agent.components.service import ComponentService

class DimensionChainQuery(DimensionQuery):
    orientation: Literal['horizontal','vertical']='horizontal'
    side: Literal['lower','upper','left','right']='lower'


def check_chain(w,sid,args):
    record=ComponentService(w,sid).build('dimensions',DimensionQuery.model_validate(
        args.model_dump(exclude={'orientation','side'})))
    axis=0 if args.orientation=='horizontal' else 1
    dims=[]
    for d in record['dimensions']:
        pts=d['definition_points']
        if len(pts)!=2 or d['dimension_type'] not in ('linear','aligned'):continue
        lo,hi=sorted([pts[0][axis],pts[1][axis]])
        length=hi-lo
        if length<=0 or d['geometry_recomputed'] is None or not math.isclose(length,d['geometry_recomputed'],rel_tol=1e-7,abs_tol=1e-6):continue
        line=d.get('dimension_line_point')
        if not line:continue
        dims.append((lo,hi,line[1-axis],d))
    tolerance=.01
    # Each path is a continuous series on ONE dimension line; no nearest-text sums.
    chains=[]
    for total in dims:
        lo,hi,pos,d=total
        parts=[x for x in dims if x[3]['object_id']!=d['object_id'] and x[0]>=lo-tolerance and x[1]<=hi+tolerance and x[1]-x[0]<hi-lo-tolerance]
        levels=[]
        for p in parts:
            if not any(abs(p[2]-v)<tolerance for v in levels):levels.append(p[2])
        for level in levels:
            candidates=[p for p in parts if abs(p[2]-level)<tolerance]
            paths=[(lo,[])];solutions=[]
            for _ in range(min(30,len(candidates))):
                nxt=[]
                for end,path in paths:
                    for p in candidates:
                        if abs(p[0]-end)>tolerance or p in path:continue
                        sequence=path+[p]
                        if abs(p[1]-hi)<tolerance:solutions.append(sequence)
                        else:nxt.append((p[1],sequence))
                paths=nxt[:100]
                if not paths:break
            # Ambiguous competing chains must not silently pick one.
            if len(solutions)==1 and len(solutions[0])>=2:chains.append((total,solutions[0]))
    chains.sort(key=lambda c: ((c[0][2] if args.side in ('lower','left') else -c[0][2]),-len(c[1])))
    receipts=[dict(operation='dimensions',fulfilled=bool(chains)),dict(operation='geometry',fulfilled=bool(chains))]
    if not chains:return dict(record_id=record['record_id'],status='unresolved',candidate_chain_count=0,
        checked_dimension_count=len(dims),candidate_dimensions=[x[3] for x in dims[:12]],
        candidate_dimensions_complete=len(dims)<=12,scope=record['request'],operation_receipts=receipts,
        diagnostics=['no_unique_continuous_native_dimension_chain'],note='未找到连续且唯一的原生尺寸链；爆炸尺寸或缺少总尺寸不能据邻近文字拼接。')
    total,parts=chains[0]
    def value(d):
        text=d['text_override'].strip()
        if d['text_state']=='suppressed':return None
        if text not in ('','<>'):
            return Decimal(text) if re.fullmatch(r'[+-]?\d+(?:\.\d+)?',text) else None
        v=d['expected_unformatted_text_value']
        if any(c['state']=='conflict' for c in d.get('measurement_comparisons',[])):
            return None
        return Decimal(str(v)) if v is not None else None
    readings=[p[3] for p in parts];all_dims=readings+[total[3]]
    nums=[value(d) for d in all_dims]
    delta=None
    if all(n is not None for n in nums):
        subtotal=sum(nums[:-1]);delta=subtotal-nums[-1]
        receipts.append(dict(operation='arithmetic',fulfilled=True,calculation_operations=['sum','subtract']))
    def displayed(d):
        labels=d.get('rendered_texts',[])
        if len(labels)!=1 or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?',labels[0].strip()):return None
        return Decimal(labels[0].strip())
    shown=[displayed(d) for d in all_dims]
    display_arithmetic=None
    if all(v is not None for v in shown):
        display_arithmetic=dict(parts=[str(v) for v in shown[:-1]],total=str(shown[-1]),
            sum=str(sum(shown[:-1])),difference=str(sum(shown[:-1])-shown[-1]),
            basis='native_rendered_dimension_text',note='原图显示文字算术，保留CAD未取整数值作另一口径。')
    selected=[{k:d[k] for k in ('object_id','entity_ids','bbox','text_override','rendered_texts','text_state','expected_unformatted_text_value','geometry_recomputed','geometry_length_m','definition_points','dimension_line_point','style','association_status','target_candidates','diagnostics')} for d in all_dims]
    for item,d in zip(selected,all_dims):
        item.update({k:d.get(k) for k in ('actual_measurement','stored_measurement','measurement_comparisons','definition_control_points','target_binding')})
    conflicts=any(d.get('status')=='conflict' for d in all_dims)
    return dict(record_id=record['record_id'],status='conflict' if conflicts else 'checked_candidate',candidate_chain_count=len(chains),
        arithmetic_basis='explicit_text_or_nonconflicting_native_measurement',
        target_binding_status='unconfirmed',unit_status=record.get('unit_status','unknown'),
        parts=selected[:-1],total=selected[-1],sum_value=float(round(sum(nums[:-1]),6)) if delta is not None else None,
        display_arithmetic=display_arithmetic,
        difference=float(round(delta,6)) if delta is not None else None,
        geometric_sum=sum(d['geometry_recomputed'] for d in readings),geometric_total=total[3]['geometry_recomputed'],
        unit=args.unit_override or record['unit'],scope=record['request'],operation_receipts=receipts,
        highlight_entity_ids=[d['object_id'] for d in all_dims],
        note='尺寸链按界点连续且同一尺寸线核验；自动尺寸值来自CAD及样式，原始显示文字单独保留；端点附近实体仍是候选。')
