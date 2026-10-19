"""Bounded factual workflows. No rule engine, applicability or compliance verdicts."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR, localcontext
import re
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.grounding import contains_number, numbers


class UserInputRef(Record):
    quote: str = Field(min_length=1, max_length=300)
    value: str = Field(min_length=1,max_length=300,description='必须含数字：原句中的数值或含数字的单位/材料短语，不可改写。纯单位约定（如按毫米）不填 user_inputs；引用工具 applied_unit。')


def user_values(refs, question, state, text):
    if not refs:return set()
    if state != 'derived' or not re.search('用户|题设|给定|假设|参考|条件', text):
        raise ValueError('用户输入须作为 derived 条件明确标注，不能冒充图纸事实。')
    for ref in refs:
        if ref.quote not in question or ref.value not in ref.quote or not numbers(ref.value) or any(not contains_number(ref.quote,n) for n in numbers(ref.value)):
            raise ValueError('用户数值必须含数字并绑定本轮问题中的完整原文片段。纯单位约定（如毫米）请删除该 user_inputs 项，引用工具 applied_unit，不要反复改写 quote。')
    return {n for r in refs for n in numbers(r.value)}


class Quantity(Record):
    origin: Literal['source','user_input','measurement','calculation']
    value: str = Field(pattern=r'^-?\d{1,12}(?:\.\d{1,8})?$')
    unit: Literal['','mm','cm','m','mm2','cm2','m2','count','ratio','percent'] = ''
    evidence_id: str = ''
    question_quote: str = Field(default='',max_length=300)
    observation_id: str = ''
    measurement_ref: str = ''
    field: Literal['value','area_m2','candidate_area_m2','basis_area_m2','perimeter_m'] = 'value'


class CalculateFacts(Record):
    operation: Literal['sum','subtract','multiply','divide','mean','ceil','floor','rank']
    quantities: list[Quantity] = Field(min_length=1,max_length=20)
    decimal_places: int = Field(default=3,ge=0,le=6)
    descending: bool = True


UNITS={'':('scalar',Decimal(1)), 'mm':('length',Decimal('.001')), 'cm':('length',Decimal('.01')), 'm':('length',Decimal(1)),
       'mm2':('area',Decimal('.000001')), 'cm2':('area',Decimal('.0001')), 'm2':('area',Decimal(1)),
       'count':('count',Decimal(1)), 'ratio':('scalar',Decimal(1)), 'percent':('scalar',Decimal('.01'))}
UNIT_TEXT={'mm':r'mm|毫米','cm':r'cm|厘米','m':r'(?<![a-z])m(?![a-z²2])|米(?![²2])',
           'mm2':r'mm[²2]|平方毫米','cm2':r'cm[²2]|平方厘米','m2':r'(?<![a-z])m[²2]|㎡|平方米',
           'percent':r'%|％','count':r'个|处|辆|车位|樘|部|间|数量','ratio':r'比例|比值|系数'}


def calculate_facts(s,args):
    resolved=[]
    conditional=False
    for q in args.quantities:
        value=Decimal(q.value); basis='source_text'; quote=''
        if q.origin=='source':
            if q.question_quote or q.observation_id or q.measurement_ref:raise ValueError('原文操作数不能混用其他来源。')
            e=s.evidence.get(q.evidence_id)
            if not e or not contains_number(e['quote'],q.value):raise ValueError('操作数不在已读原文中。')
            quote=e['quote']
        elif q.origin=='user_input':
            if q.evidence_id or q.observation_id or q.measurement_ref:raise ValueError('用户操作数不能冒充原文或测量。')
            user_values([UserInputRef(quote=q.question_quote,value=q.value)],s.request.question,'derived','用户给定条件')
            quote=q.question_quote;basis='user_assumption'
        elif q.origin=='calculation':
            if q.evidence_id or q.question_quote or q.measurement_ref:raise ValueError('前序计算只用计算观测ID引用。')
            o=s.observations.get(q.observation_id,{})
            d=o.get('data',{})
            if o.get('tool')!='calculate_facts' or d.get('value') is None or Decimal(str(d['value']))!=value or d.get('unit')!=q.unit:
                raise ValueError('前序计算引用、值或单位不符。')
            conditional=conditional or d.get('conditional',False)
            basis='prior_calculation'
        else:
            if q.evidence_id or q.question_quote:raise ValueError('测量操作数只能引用测量记录。')
            o=s.observations.get(q.observation_id,{})
            if o.get('tool')=='analyze_measurements':
                row=next((v for v in o['data'].get('values',[]) if v['measurement_ref']==q.measurement_ref),None)
                if not row or Decimal(str(row['value']))!=value or row['unit'].replace('²','2')!=q.unit:raise ValueError('测量引用、单位或值不符。')
                basis=row['basis']
            elif o.get('tool') in ('measure_geometry','measure_room','measure_room_area'):
                d=o['data']; actual=d.get(q.field)
                if actual is None or Decimal(str(actual))!=value:raise ValueError('测量字段未取得有效数值。')
                if q.field=='value':
                    if d.get('status')!='measured' or d.get('unit','').replace('²','2')!=q.unit:raise ValueError('测量状态或单位未确认。')
                elif q.unit!=('m' if q.field=='perimeter_m' else 'm2'):raise ValueError('测量字段单位不匹配。')
                basis=d.get('measurement_basis',q.field)
            else:raise ValueError('只允许引用已读的确定性测量工具。')
        if quote and q.unit and not re.search(UNIT_TEXT[q.unit],quote,re.I):
            raise ValueError('原文未明确该单位；先读取单位依据，不能默认为毫米或平方米。')
        dim,factor=UNITS[q.unit]
        resolved.append(dict(**q.model_dump(),basis=basis,normalized_value=str(value*factor),dimension=dim))
    vals=[Decimal(v['normalized_value']) for v in resolved];dims=[v['dimension'] for v in resolved]
    op=args.operation
    if op=='rank':
        if len(set(dims))!=1:raise ValueError('排序需要相同量纲。')
        ordering=sorted(range(len(vals)),key=lambda i:((-vals[i] if args.descending else vals[i]),i))
        return dict(operation=op,inputs=resolved,ordered=[dict(input_index=i,**resolved[i]) for i in ordering],
            state='derived',conditional=conditional or any(q.origin=='user_input' for q in args.quantities),
            note='仅排序给定对象集合，不代表全图已穷尽或对象口径已确认。',
            operation_receipts=[dict(operation='ranking',fulfilled=True)])
    if op in ('sum','mean','subtract'):
        if len(set(dims))!=1:raise ValueError('加减平均需要相同量纲。')
        if len({v['basis'] for v in resolved if v['origin']=='measurement'})>1:raise ValueError('测量口径不同，不能合并。')
        if op=='subtract' and len(vals)!=2:raise ValueError('差值须有两个操作数。')
        result=sum(vals)/len(vals) if op=='mean' else vals[0]-vals[1] if op=='subtract' else sum(vals)
        unit={'scalar':'ratio','length':'m','area':'m2','count':'count'}[dims[0]]
    elif op in ('ceil','floor'):
        if len(vals)!=1 or dims[0] not in ('scalar','count'):raise ValueError('取整只支持一个无量纲值或数量。')
        result=vals[0].to_integral_value(rounding=ROUND_CEILING if op=='ceil' else ROUND_FLOOR);unit='count' if dims[0]=='count' else 'ratio'
    else:
        if len(vals)!=2:raise ValueError('乘除须有两个操作数。')
        a,b=dims
        if op=='divide':
            if not vals[1]:raise ValueError('分母不能为零。')
            if a==b:unit='ratio'
            elif b=='scalar':unit={'length':'m','area':'m2','count':'count','scalar':'ratio'}[a]
            elif a=='area' and b=='count':unit='m2/count'
            else:raise ValueError('不支持该量纲组合。')
            result=vals[0]/vals[1]
        else:
            if a=='length' and b=='length':unit='m2'
            elif 'scalar' in dims:unit={'length':'m','area':'m2','count':'count','scalar':'ratio'}[b if a=='scalar' else a]
            else:raise ValueError('不支持该量纲组合。')
            result=vals[0]*vals[1]
    if op in ('sum','mean','subtract') and len({q.unit for q in args.quantities})==1:
        unit=args.quantities[0].unit
        result=result/UNITS[unit][1]
    with localcontext() as ctx:
        ctx.prec=48
        result=result.quantize(Decimal(1).scaleb(-args.decimal_places),rounding=ROUND_HALF_UP)
    return dict(operation=op,inputs=resolved,value=str(result),unit=unit,state='derived',
                conditional=conditional or any(q.origin=='user_input' for q in args.quantities),
                applicability='unverified',note='仅计算已绑定数值；对象一致性及输入适用关系待核对，不作优化或合规判断。',
                operation_receipts=[dict(operation='arithmetic',fulfilled=True)])


class ReadAssembly(Record):
    region_id: str
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=8,ge=1,le=12)


def read_assembly(s,args):
    """Read explicit parser descendants and table rows; proximity never confers scope."""
    idx=s.index;r=idx.by_id[args.region_id]
    rows=idx.scope(r.layout_id,r.frame_id,r.region_id)
    if len(rows)==1:
        # A detached heading still gets a bounded next-reading menu, not a false
        # complete assembly or automatic table/header inheritance.
        rows=[x for x in idx.scope(r.layout_id,r.frame_id) if x.column_key==r.column_key]
        rows.sort(key=lambda x:(-x.bbox[3],x.bbox[0],x.region_id))
        at=next(i for i,x in enumerate(rows) if x.region_id==r.region_id)
        rows=rows[max(0,at-1):at+18];basis='same_column_neighbors'
    else:basis='parser_descendants'
    page=rows[args.offset:args.offset+args.limit]
    items=[dict(region_id=x.region_id,order=args.offset+i,kind=x.kind,
                relation_state='candidate',evidence=idx.evidence(x,end=min(len(x.text),1800)),
                truncated=len(x.text)>1800,next_start=1800 if len(x.text)>1800 else None) for i,x in enumerate(page)]
    tables=[]
    ids=set(r.entity_ids)
    for t in idx.tables:
        if t['layout_id']!=r.layout_id:continue
        matches=[row['row_index'] for row in t['rows'] if any(ids.intersection(c['entity_ids']) for c in row['cells'])]
        if matches:
            tables.append(dict(table_id=t['table_id'],matched_rows=matches,header_candidate_row=0,
                               next_tool='read_table_semantics'))
            if len(tables)>=3:break
    return dict(target_region_id=r.region_id,relation_basis=basis,items=items,table_candidates=tables,
                total=len(rows),has_more=args.offset+args.limit<len(rows),next_offset=args.offset+args.limit,
                completeness='candidate_sequence',note='逐层核对材料、厚度、顺序及适用条件；邻近条文不自动归入目标做法，括号替代与局部加强须区别。')


class ResolveScope(Record):
    floor_title: str = Field(default='',max_length=80)
    layout_id: str = ''
    names: list[str] = Field(default_factory=list,max_length=4)
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=12,ge=1,le=20)


def resolve_scope(s,args):
    from cad_agent.geometry.room_batch import RoomBatchService, normalized
    from cad_agent.cad.titles import same_plan_title
    svc=RoomBatchService(s.w,s.request.snapshot_id);svc.index.scope(args.layout_id)
    frames=[f for f in svc.drawing['frames'] if (not args.layout_id or f['layout_id']==args.layout_id)
            and (not args.floor_title or same_plan_title(f['name'],args.floor_title))]
    choices=[];items=[];seen=set()
    for f in frames:
        diagnostic,rows,titles=svc.diagnose_frame(f)
        choices.append({k:f[k] for k in ('frame_id','layout_id','name','bbox','method') if k in f}|diagnostic)
        for r in rows:
            if r.region_id in seen or not args.names or len(r.text)>60:continue
            if any(normalized(n) in normalized(r.text) for n in args.names):
                seen.add(r.region_id);items.append(dict(region_id=r.region_id,frame_id=f['frame_id'],layout_id=r.layout_id,
                    bbox=r.bbox,identity='label_candidate',evidence=svc.index.evidence(r)))
    return dict(scope_status='single_candidate' if sum(c['eligible'] for c in choices)==1 else 'ambiguous' if choices else 'not_found',
        choices=choices,items=items[args.offset:args.offset+args.limit],total=len(items),has_more=args.offset+args.limit<len(items),
        note='名称候选可先交付；未确定楼层范围前不能测量或跨层叠合。重复标题不自动当成不同楼层，不自动选冲突图框。')


class ProjectRead(Record):
    action: Literal['catalog','search','read'] = 'catalog'
    snapshot_id: str = ''
    query: str = Field(default='',max_length=150)
    region_id: str = ''
    start: int = Field(default=0,ge=0)
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=5,ge=1,le=8)


def project_read(s,args):
    allowed=[s.request.snapshot_id]+[d.snapshot_id for d in s.request.related_drawings]
    if args.action=='catalog':
        metadata={d.snapshot_id:d.model_dump() for d in s.request.related_drawings}
        return dict(items=[dict(snapshot_id=sid,name=s.w.store.get('drawing',sid)['name'],
            sha256=s.w.store.get('drawing',sid)['sha256'],metadata=metadata.get(sid,{})) for sid in allowed],
            note='仅用户选定资料；标签是用户提供，项目、轴网、楼层和版本关系仍需原图核对。未选资料不可访问。')
    if args.snapshot_id not in allowed:raise ValueError('目标图纸未被用户加入本轮关联资料。')
    if args.action=='search':
        return dict(snapshot_id=args.snapshot_id,**DrawingIndex(s.w,args.snapshot_id).search(query=args.query,offset=args.offset,limit=args.limit))
    from cad_agent.analysis.evidence_ledger import source_context
    return source_context(s.w,args.snapshot_id,args.region_id,start=args.start,neighbors=1,bounded=True)
