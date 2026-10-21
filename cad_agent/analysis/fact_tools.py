"""Bounded factual workflows. No rule engine, applicability or compliance verdicts."""
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR, localcontext
import re
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.grounding import contains_number, contains_numeric_phrase, numbers, numeric_tokens


class UserInputRef(Record):
    quote: str = Field(min_length=1, max_length=300,description='本轮问题中的逐字原文片段，保留单位、不等号和材料型号；不可改写或替换显示精度。')
    value: str = Field(min_length=1,max_length=300,description='必须含数字：quote 中的数值或含数字的单位/材料短语。独立数值允许3与3.0等精度等价；单位、不等号、材料型号不可改写。纯单位约定（如按毫米）不填 user_inputs；引用工具 applied_unit。')


class UserInputValidationError(ValueError):
    """A field-local repair hint without relaxing any source binding."""
    def __init__(self, path, code, message):
        self.path, self.code = path, code
        super().__init__(f'{path} [{code}]：{message}')


def user_values(refs, question, state, text):
    if not refs:return set()
    if state not in ('derived', 'unknown'):
        raise UserInputValidationError('state', 'USER_INPUT_REQUIRES_DERIVED', '用户输入须作为 derived 条件或 unknown 缺口说明，不能冒充图纸事实；保留有效的 user_inputs 引用。')
    if not re.search('用户|题设|给定|假设|参考|条件', text):
        raise UserInputValidationError('text', 'USER_CONDITION_NOT_LABELED', '正文须明确标注用户给定或参考条件；保留有效的 user_inputs 引用。')
    for index, ref in enumerate(refs):
        path = f'user_inputs[{index}]'
        if ref.quote not in question:
            raise UserInputValidationError(path + '.quote', 'QUOTE_NOT_IN_QUESTION', 'quote 不是本轮问题的逐字片段；仅将此字段改为包含目标值的实际原句，不要改动其他有效引用。')
        values = numbers(ref.value)
        if not values:
            raise UserInputValidationError(path + '.value', 'VALUE_HAS_NO_NUMBER', 'value 缺少数字；数值引用应填写 quote 中的数值。若此项仅表达单位约定（如按毫米），只移除此项并引用工具 applied_unit，不要改写有效 quote。')
        missing = [n for n in values if not contains_number(ref.quote, n)]
        if missing:
            raise UserInputValidationError(path + '.value', 'NUMBER_NOT_IN_QUOTE', f'数值 {missing!r} 不在此项 quote 中（仅独立数值允许精度等价）；仅修正此 value 或绑定正确原句，不能从其他引用借用数字。')
        if not contains_numeric_phrase(ref.quote, ref.value):
            raise UserInputValidationError(path + '.value', 'VALUE_NOT_IN_QUOTE', '数字存在，但 value 的完整短语不匹配 quote；保留单位、不等号、材料型号和词序，仅修正此 value，不能拼接不同位置的数字。')
    return {n for r in refs for n in numbers(r.value)}


class Quantity(Record):
    origin: Literal['source','user_input','measurement','calculation']
    label: str = Field(default='',max_length=120,description='明确对象与操作数角色，例如某分区面积；不要从第一个数字猜值。')
    source_quote: str = Field(default='',max_length=500,description='可选的已读原文逐字片段，须含本操作数与单位；保留约数和适用范围。')
    value: str = Field(pattern=r'^-?\d{1,12}(?:\.\d{1,8})?$')
    unit: Literal['','mm','cm','m','mm2','cm2','m2','count','ratio','percent','m2/count'] = ''
    evidence_id: str = ''
    unit_evidence_id: str = Field(default='', description='原文数值省略单位时引用已读默认线性尺寸单位条款。同快照/布局；跨图框须有本工程施工图或单体建筑设计的明确适用声明。仅适用厚度/长度/宽度/高度，不用于坐标、标高、面积。')
    question_quote: str = Field(default='',max_length=300)
    observation_id: str = ''
    measurement_ref: str = ''
    object_id: str = Field(default='', max_length=160, description='可选核验约束，须逐字对应来源/测量记录的 object_id；不能自造对象名。')
    scope_id: str = Field(default='', max_length=160, description='可选核验约束，须对应来源/测量记录的 scope_id；不能自造范围。')
    field: Literal['value','area_m2','candidate_area_m2','basis_area_m2','perimeter_m','annotation_area_m2'] = 'value'


class MissingOperand(Record):
    label: str = Field(min_length=1,max_length=120)
    reason: str = Field(min_length=1,max_length=300,description='具体缺少哪个对象的数值、单位或口径，已读范围内未确认不等于原图不存在。')
    required_source: str = Field(default='',max_length=200)
    unit: str = Field(default='',max_length=20)


class CalculateFacts(Record):
    operation: Literal['sum','subtract','multiply','divide','mean','ceil','floor','rank']
    quantities: list[Quantity] = Field(default_factory=list,max_length=20)
    calculation_key: str = Field(default='',max_length=120,description='同一目标算式的稳定标识；缺项补齐时沿用，不得用另一对象算式覆盖。')
    review_group_id: str = Field(default='', max_length=160, description='required_calculation_review 提供的候选组标识；显式计算或记录该组缺项时原样回传，仅表示检查范围，不证明对象归属。')
    target: str = Field(default='',max_length=160,description='本算式所针对对象/分区/做法；要求列明多个对象时逐字填写其中一个对象名，口径放在操作数label中。')
    coverage_scope: Literal['single','aggregate'] = 'single'
    operand_relation: Literal['independent','same_object','same_scope'] = Field(default='independent', description='独立数值计算保留语义未确认；要求同对象/范围时须核验来源身份。缺项只阻止本算式，不影响其他完整算式。')
    missing_operands: list[MissingOperand] = Field(default_factory=list,max_length=20,description='未知操作数逐项列缺口，允许 quantities 为空；返回未执行计划，不授予计算完成。')
    decimal_places: int = Field(default=3,ge=0,le=6)
    descending: bool = True


UNITS={'':('scalar',Decimal(1)), 'mm':('length',Decimal('.001')), 'cm':('length',Decimal('.01')), 'm':('length',Decimal(1)),
       'mm2':('area',Decimal('.000001')), 'cm2':('area',Decimal('.0001')), 'm2':('area',Decimal(1)),
       'm2/count':('area_per_count',Decimal(1)), 'count':('count',Decimal(1)), 'ratio':('scalar',Decimal(1)), 'percent':('scalar',Decimal('.01'))}
UNIT_TEXT={'mm':r'mm|毫米','cm':r'cm|厘米','m':r'(?<![a-z])m(?![a-z²2])|米(?![²2])',
           'mm2':r'mm[²2]|平方毫米','cm2':r'cm[²2]|平方厘米','m2':r'(?<![a-z])m[²2]|㎡|平方米',
           'm2/count':r'(?:m[²2]|㎡|平方米)\s*[/／]\s*(?:车位|车|辆|个|count)',
           'percent':r'%|％','count':r'个|处|辆|车位|樘|部|间|数量','ratio':r'比例|比值|系数'}


def calculation_tokens(quote):
    """Inventory literal numbers and adjacent units, never infer an operand join.

    Roles are lexical hints only. Identifier/model fragments and implicit units
    remain unavailable; an area label followed by m must not become m2.
    """
    order=('m2/count','mm2','cm2','m2','mm','cm','m','percent','count','ratio')
    for token in numeric_tokens(quote):
        start,end=token['text_span']
        if start and re.match(r'[0-9A-Za-z_.]',quote[start-1]):continue
        before,tail=quote[:start],quote[end:]
        identifier=bool(re.search(r'(?:编号|型号)\s*$',before) or re.match(r'[#＃]|\s*(?:号|防火分区)',tail))
        unit=next((u for u in order if re.match(r'\s*(?:'+UNIT_TEXT[u]+r')(?![A-Za-z²0-9])',tail,re.I)), '')
        identifier=identifier or bool(re.match(r'[A-Za-z_]',tail) and not unit)
        if not unit and re.search(r'(?:比例|比值|系数)\s*[:：=]?\s*$',before):unit='ratio'
        role_match=re.search(r'(面积|停车数|车位数|数量|厚度|长度|宽度|净宽|标高)\s*(?:为|[:：=])?\s*(?:约)?\s*$',before)
        role=role_match[1] if role_match else '厚度' if re.match(r'\s*厚(?!度)',tail) else ''
        identifier = identifier or bool(re.match(r'\s*[、．.]',tail) and not role)
        status='identifier' if identifier else 'unit_unconfirmed' if not unit else 'available_numeric'
        if role=='面积' and unit and UNITS[unit][0]!='area':status='unit_conflict'
        yield dict(value=token['value'],unit=unit,role=role,status=status,number_span=token['text_span'],
                   raw=token['raw'],interpretation=token['interpretation'])


def calculate_facts(s,args):
    from copy import deepcopy
    from cad_agent.analysis.calculation_workflow import operand_binding, calculation_binding
    resolved=[]
    conditional=False
    approximate=False
    key=args.calculation_key or args.operation+':'+args.target
    task_data=getattr(getattr(s,'task',None),'data',{})
    trace=getattr(s,'trace',[])
    owners=trace[-1].get('task_ids',[]) if trace else []
    for plan in task_data.get('calculation_workflows',{}).values():
        if plan['calculation_key']==key and set(plan['task_ids'])&set(owners):
            if plan['target']!=args.target or plan['operation']!=args.operation or plan['coverage_scope']!=args.coverage_scope or plan.get('operand_relation','independent')!=args.operand_relation:
                raise ValueError('calculation_key 已绑定另一目标、运算或口径；不能用其他算式消除缺项。')
    if not args.quantities and not args.missing_operands:raise ValueError('至少提供已绑定操作数或明确 missing_operands；不能猜操作数。')
    for q in args.quantities:
        if q.unit_evidence_id and q.origin != 'source':
            raise ValueError('独立默认单位依据仅允许原文操作数使用。')
        value=Decimal(q.value); basis='source_text'; quote='';source_approximate=False
        record={};lineage=None;upstream={};candidate=False
        if q.source_quote and q.origin not in ('source','user_input'):raise ValueError('source_quote 仅用于原文或用户原句。')
        if q.origin=='source':
            if q.question_quote or q.observation_id or q.measurement_ref:raise ValueError('原文操作数不能混用其他来源。')
            e=s.evidence.get(q.evidence_id)
            if not e or not contains_number(e['quote'],q.value):raise ValueError('操作数不在已读原文中。')
            quote=e['quote'];record=e
        elif q.origin=='user_input':
            if q.evidence_id or q.observation_id or q.measurement_ref:raise ValueError('用户操作数不能冒充原文或测量。')
            user_values([UserInputRef(quote=q.question_quote,value=q.value)],s.request.question,'derived','用户给定条件')
            quote=q.question_quote;basis='user_assumption'
            record={'question_quote':q.question_quote}
        elif q.origin=='calculation':
            if q.evidence_id or q.question_quote or q.measurement_ref:raise ValueError('前序计算只用计算观测ID引用。')
            o=s.observations.get(q.observation_id,{})
            d=o.get('data',{})
            if o.get('tool')!='calculate_facts' or d.get('status')!='calculated' or d.get('value') is None or Decimal(str(d['value']))!=value or d.get('unit')!=q.unit:
                raise ValueError('前序计算引用、值或单位不符。')
            conditional=conditional or d.get('conditional',False)
            source_approximate=d.get('approximate',False)
            basis='prior_calculation';record=d
            lineage=deepcopy(d.get('lineage',[]))
            if not lineage:
                raise ValueError('前序计算缺少操作数来源链；重新调用 calculate_facts 绑定原始操作数。')
            value=Decimal(d.get('unrounded_value',d['value']))
            candidate=bool(d.get('candidate_inputs'))
            upstream=dict(upstream_target=d.get('target',''), upstream_target_binding_status=d.get('target_binding_status','unconfirmed'),
                          upstream_calculation_key=d.get('calculation_key',''), upstream_unit=d.get('unit',''))
        else:
            if q.evidence_id or q.question_quote:raise ValueError('测量操作数只能引用测量记录。')
            o=s.observations.get(q.observation_id,{})
            if o.get('tool')=='analyze_measurements':
                row=next((v for v in o['data'].get('values',[]) if v['measurement_ref']==q.measurement_ref),None)
                if not row or Decimal(str(row['value']))!=value or row['unit'].replace('²','2')!=q.unit:raise ValueError('测量引用、单位或值不符。')
                if row.get('status') in ('unresolved','failed','unknown','conflict'):raise ValueError('测量记录尚未确认。')
                basis=row['basis'];record=dict(o['data'],**row)
            elif o.get('tool') in ('analyze_room_batch','select_room_boundaries','resolve_room_units'):
                d=o['data'].get('measurement',o['data'])
                rows=[r for r in d.get('items',[]) if r.get('region_id')==q.measurement_ref]
                if len(rows)!=1:raise ValueError('房间批次操作数须引用唯一 region_id 作为 measurement_ref。')
                row=rows[0]
                if q.field=='annotation_area_m2':
                    annotations=[a for a in row.get('area_annotation_checks',[]) if a.get('value') is not None and
                                 a.get('unit')=='m2' and a.get('status')=='source_supported_compound_unit']
                    if len(annotations)!=1:raise ValueError('面积标注须有唯一且来源核验过的复合单位记录。')
                    annotation=annotations[0];actual=annotation['value']
                    record=dict(row,**annotation);basis='independent_source_annotation';candidate=True
                else:
                    if q.field not in ('area_m2','candidate_area_m2'):raise ValueError('房间批次仅支持已计算面积字段。')
                    actual=row.get(q.field);record=row;basis=row.get('area_basis',q.field)
                    if row.get('status') in ('unresolved','failed','unknown','conflict','needs_unit_confirmation'):
                        raise ValueError('房间批次面积尚未确认数值或单位。')
                    candidate=q.field=='candidate_area_m2' or row.get('status')!='measured'
                if actual is None or Decimal(str(actual))!=value or q.unit!='m2':raise ValueError('房间批次字段、数值或单位不匹配。')
                # The label anchor is a reference, not a proven room identity.
                record=dict(record,region_id=q.measurement_ref,batch_id=d.get('batch_id'),
                            snapshot_id=o.get('snapshot_id') or d.get('snapshot_id'))
            elif o.get('tool') in ('measure_geometry','measure_room','measure_room_area'):
                if q.measurement_ref:raise ValueError('此测量工具不提供 measurement_ref；请引用 observation_id 与 field。')
                d=o['data']; actual=d.get(q.field)
                if actual is None or Decimal(str(actual))!=value:raise ValueError('测量字段未取得有效数值。')
                if q.field=='value':
                    if d.get('status')!='measured' or d.get('unit','').replace('²','2')!=q.unit:raise ValueError('测量状态或单位未确认。')
                elif q.field=='annotation_area_m2' or q.unit!=('m' if q.field=='perimeter_m' else 'm2'):raise ValueError('测量字段单位不匹配。')
                if q.field not in ('value','candidate_area_m2') and d.get('status') in ('unresolved','failed','unknown','conflict'):
                    raise ValueError('测量状态未确认；候选面积仅可显式引用 candidate_area_m2。')
                basis=d.get('measurement_basis',q.field);record=d
                candidate=q.field=='candidate_area_m2' or d.get('status') in ('candidate','needs_review')
            else:raise ValueError('只允许引用已读的确定性测量工具。')
        binding=operand_binding(record,q.origin,q.evidence_id or q.observation_id or 'current_question',
                                field=q.field if q.origin=='measurement' else '',basis=basis,quote=quote)
        for constraint in ('object_id','scope_id'):
            if getattr(q,constraint) and record.get(constraint)!=getattr(q,constraint):
                raise ValueError(f'{constraint} 未与来源记录绑定；请读取该对象/范围记录，不可用模型标签替代。')
        if lineage is None:lineage=[deepcopy(binding)]
        original_quote=quote
        number_evidence=[]
        unit_binding=None
        if q.source_quote:
            if q.source_quote not in quote or not contains_number(q.source_quote,q.value):raise ValueError('source_quote 必须是含该数值的已读原文片段。')
            quote=q.source_quote
        if quote:
            source_approximate=bool(re.search(r'约|大约|近似|左右|测绘.{0,8}为准',original_quote))
        if quote:
            tokens=[t for t in calculation_tokens(quote)
                    if re.fullmatch(r'-?\d+(?:\.\d+)?',t['value']) and Decimal(t['value'])==Decimal(q.value)]
            if not tokens:
                raise ValueError('该数值仅见于编号或材料型号，不能作为独立数量操作数。')
            if all(t['status']=='identifier' for t in tokens):
                raise ValueError('编号不是数量操作数；须读取明确数值与单位，不能按编号或数字邻近配值。')
            tokens=[t for t in tokens if t['status']!='identifier']
            # Use longest unit first. mm cannot borrow the prefix of mm2, and
            # m2 cannot discard the denominator of m2/count.
            unit_order=('m2/count','mm2','cm2','m2','mm','cm','m','percent','count','ratio')
            bound_units=[]
            for token in tokens:
                tail=quote[token['number_span'][1]:]
                detected=next((u for u in unit_order if re.match(r'\s*(?:'+UNIT_TEXT[u]+r')(?![A-Za-z²0-9])',tail,re.I)), '')
                if not detected and re.search(r'(?:比例|比值|系数)\s*[:：=]?\s*$',quote[:token['number_span'][0]]):detected='ratio'
                bound_units.append(detected)
            if q.unit_evidence_id:
                from cad_agent.analysis.default_units import resolve
                unit_binding = resolve(s, q, record, quote, tokens)
            if q.unit and q.unit not in bound_units:
                if any(bound_units) or not unit_binding:
                    raise ValueError('该数值没有直接绑定所填单位；须读取并绑定明确默认单位条款，不能借用其他数字的单位或截断复合单位。')
            if not q.unit and any(bound_units):
                raise ValueError('原文数值带有单位；不能省略单位将工程量当作无量纲数字。')
            # Spans are always relative to the unchanged full evidence quote.
            offset=original_quote.index(quote)
            number_evidence=[dict(raw=t['raw'],value=t['value'],interpretation=t['interpretation'],
                                  text_span=t['number_span'])
                             for t in calculation_tokens(original_quote)
                             if t['status']!='identifier' and (t['unit']==q.unit or (unit_binding and not t['unit']))
                             and re.fullmatch(r'-?\d+(?:\.\d+)?',t['value'])
                             and Decimal(t['value'])==Decimal(q.value)
                             and t['number_span'][1]>offset and t['number_span'][0]<offset+len(quote)]
        approximate=approximate or source_approximate
        dim,factor=UNITS[q.unit]
        if unit_binding:
            binding['unit_basis'] = unit_binding
            # Keep the clause identity through chained calculations without
            # changing the immutable numerical source's quote or object identity.
            for leaf in lineage:
                if leaf.get('reference') == q.evidence_id:
                    leaf['unit_basis'] = unit_binding
        resolved.append(dict(**q.model_dump(),basis=basis,normalized_value=str(value*factor),dimension=dim,
                             quote=original_quote,approximate=source_approximate,binding=binding,lineage=lineage,
                             unit_status='explicit' if q.unit else 'numeric_only_unit_unconfirmed',candidate=candidate,
                             number_evidence=number_evidence,unit_binding=unit_binding,**upstream))
    for plan in task_data.get('calculation_workflows',{}).values():
        if plan['calculation_key']!=key or not set(plan['task_ids'])&set(owners):continue
        prior=plan.get('inputs',[])
        identity_fields=('origin','evidence_id','unit_evidence_id','observation_id','measurement_ref','field','value','unit','object_id','scope_id')
        identity_changed=any(any(old.get(k,'')!=new.get(k,'') for k in identity_fields)
                             for old,new in zip(prior,resolved))
        known_object_changed=any(any(old.get('binding',{}).get(k) and
                                    old['binding'][k]!=new.get('binding',{}).get(k)
                                    for k in ('snapshot_id','frame_id','scope_id','object_id'))
                                 for old,new in zip(prior,resolved))
        if len(resolved)<len(prior) or known_object_changed or (plan['status']=='calculated' and identity_changed):
            raise ValueError('calculation_key 已绑定操作数来源、顺序和对象；补齐未知单位/来源可沿用，不能换成另一已知对象。已执行算式修改须用新 calculation_key。')
    if args.review_group_id:
        from cad_agent.analysis.calculation_workflow import operand_groups
        group = next((g for owner in owners for g in operand_groups(task_data, task_data.get('calculation_inputs', {}).get(owner, {}))
                      if g['review_group_id'] == args.review_group_id), None)
        if group is None:
            raise ValueError('review_group_id 不属于当前分项已读的候选组。')
        physical = [r for r in resolved if r['origin'] != 'user_input']
        if group['kind'] == 'calculated_subtotal':
            matches = all(r['origin'] == 'calculation' and r.get('observation_id') in group['observation_ids'] for r in physical)
        else:
            leaves = [leaf for r in physical for leaf in r['lineage'] if leaf['origin'] != 'user_input']
            matches = all(leaf['origin'] == 'source' and leaf.get('reference') in group['evidence_ids'] for leaf in leaves)
        if not matches:
            raise ValueError('操作数不属于该 review_group_id，不能用另一组计算消除当前检查。')
    relation,gaps=calculation_binding(args,resolved,s.request.question)
    metadata=dict(calculation_key=args.calculation_key or args.operation+':'+args.target,
                  target=args.target,coverage_scope=args.coverage_scope,review_group_id=args.review_group_id,
                  missing_operands=[v.model_dump() for v in args.missing_operands]+gaps,approximate=approximate,
                  candidate_inputs=any(r['candidate'] for r in resolved),**relation)
    if metadata['missing_operands']:
        return dict(**metadata,operation=args.operation,inputs=resolved,status='missing_operands',state='unknown',
                    conditional=conditional or any(q.origin=='user_input' for q in args.quantities),
                    note='尚未执行算术。补齐明确对象的操作数、单位和口径后沿用 calculation_key 再调用；不能以已读原文代替计算完成。',
                    operation_receipts=[dict(operation='ranking' if args.operation=='rank' else 'arithmetic',fulfilled=False,
                                             calculation_operation=args.operation)])
    vals=[Decimal(v['normalized_value']) for v in resolved];dims=[v['dimension'] for v in resolved]
    op=args.operation
    if op=='rank':
        if len(set(dims))!=1:raise ValueError('排序需要相同量纲。')
        ordering=sorted(range(len(vals)),key=lambda i:((-vals[i] if args.descending else vals[i]),i))
        return dict(**metadata,status='calculated',operation=op,inputs=resolved,ordered=[dict(input_index=i,**resolved[i]) for i in ordering],
            state='derived',conditional=conditional or any(q.origin=='user_input' for q in args.quantities),
            note='仅排序给定对象集合，不代表全图已穷尽或对象口径已确认。',
            operation_receipts=[dict(operation='ranking',fulfilled=True,calculation_operation=op)])
    if op in ('sum','mean','subtract'):
        if len(set(dims))!=1:raise ValueError('加减平均需要相同量纲。')
        if len({v['basis'] for v in resolved if v['origin']=='measurement'})>1:
            if op!='subtract' or args.operand_relation!='independent':raise ValueError('测量口径不同，不能合并。')
            metadata.update(basis_comparability='unconfirmed',semantic_difference=None)
        if op=='subtract' and len(vals)!=2:raise ValueError('差值须有两个操作数。')
        result=sum(vals)/len(vals) if op=='mean' else vals[0]-vals[1] if op=='subtract' else sum(vals)
        unit={'scalar':'ratio','length':'m','area':'m2','area_per_count':'m2/count','count':'count'}[dims[0]]
    elif op in ('ceil','floor'):
        if len(vals)!=1 or dims[0] not in ('scalar','count'):raise ValueError('取整只支持一个无量纲值或数量。')
        result=vals[0].to_integral_value(rounding=ROUND_CEILING if op=='ceil' else ROUND_FLOOR);unit='count' if dims[0]=='count' else 'ratio'
    else:
        if len(vals)!=2:raise ValueError('乘除须有两个操作数。')
        a,b=dims
        if op=='divide':
            if not vals[1]:raise ValueError('分母不能为零。')
            if a==b:unit='ratio'
            elif b=='scalar':unit={'length':'m','area':'m2','area_per_count':'m2/count','count':'count','scalar':'ratio'}[a]
            elif a=='area' and b=='count':unit='m2/count'
            else:raise ValueError('不支持该量纲组合。')
            result=vals[0]/vals[1]
        else:
            if a=='length' and b=='length':unit='m2'
            elif 'scalar' in dims:unit={'length':'m','area':'m2','area_per_count':'m2/count','count':'count','scalar':'ratio'}[b if a=='scalar' else a]
            else:raise ValueError('不支持该量纲组合。')
            result=vals[0]*vals[1]
    if op in ('sum','mean','subtract') and len({q.unit for q in args.quantities})==1:
        unit=args.quantities[0].unit
        result=result/UNITS[unit][1]
    unrounded_value=str(result)
    with localcontext() as ctx:
        ctx.prec=48
        result=result.quantize(Decimal(1).scaleb(-args.decimal_places),rounding=ROUND_HALF_UP)
    return dict(**metadata,status='calculated',operation=op,inputs=resolved,value=str(result),unrounded_value=unrounded_value,unit=unit,state='derived',
                conditional=conditional or any(q.origin=='user_input' for q in args.quantities),
                applicability='unverified',note=('输入含约数，输出小数位仅为计算显示精度；' if approximate else '')+'仅计算已绑定数值；对象一致性、集合覆盖及输入适用关系待核对，不作优化或合规判断。',
                operation_receipts=[dict(operation='arithmetic',fulfilled=True,calculation_operation=op)] +
                    ([dict(operation='units', fulfilled=True, basis='validated_calculation_operands')]
                     if resolved and all(r['unit_status']=='explicit' for r in resolved) else []))


class ReadAssembly(Record):
    region_id: str
    view: Literal['record','catalog'] = 'record'
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=8,ge=1,le=12)


def read_assembly(s,args):
    from cad_agent.analysis.assembly import read
    result = read(s.index,args)
    from cad_agent.analysis.default_units import candidates
    result['default_unit_clauses'] = [
        dict(evidence=s.index.evidence(r), applicability='requires_explicit_unit_evidence_binding')
        for r in candidates(s.index, s.index.by_id[args.region_id])[:3]]
    return result


class ResolveScope(Record):
    floor_title: str = Field(default='',max_length=80)
    view: Literal['plan','section','detail'] = 'plan'
    layout_id: str = ''
    names: list[str] = Field(default_factory=list,max_length=4)
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=12,ge=1,le=20)


def resolve_scope(s,args):
    from cad_agent.geometry.room_batch import RoomBatchService, normalized
    from cad_agent.cad.titles import plan_title_relation, plan_title_key
    from cad_agent.contracts import stable_id
    from cad_agent.analysis.scope_workflow import remember_scope, compact_scope
    svc=RoomBatchService(s.w,s.request.snapshot_id);svc.index.scope(args.layout_id)
    frames=[f for f in svc.drawing['frames'] if (not args.layout_id or f['layout_id']==args.layout_id)
            and (args.view != 'plan' or plan_title_relation(f['name'],args.floor_title) != 'different')]
    choices=[];candidates=[];seen=set();title_sources={}
    view_scene=svc.index.scene if args.view != 'plan' else None
    for f in frames:
        if args.view == 'plan':
            diagnostic,rows,titles=svc.diagnose_frame(f)
            relation=plan_title_relation(f['name'],args.floor_title)
        else:
            from cad_agent.analysis.view_scope import diagnose_view_frame, title_key
            diagnostic,rows,titles=diagnose_view_frame(svc.index,f,args.view,args.floor_title,view_scene)
            if (args.floor_title and not diagnostic['requested_title_found']
                    and title_key(f['name']) != title_key(args.floor_title)):
                continue
            relation=diagnostic['title_match']
        # An omitted operating mode may reveal aliases, but cannot choose a
        # peacetime plan when another supported operating-mode identity exists.
        mode_conflict=(args.view=='plan' and relation=='unspecified_mode' and any(
            plan_title_key(other['name']) != plan_title_key(f['name']) and
            any(m in other['name'] for m in ('平时','战时')) for other in frames))
        if mode_conflict:
            diagnostic['reasons'].append('unspecified_operating_mode')
            diagnostic['eligible']=False
        choices.append({k:f[k] for k in ('frame_id','layout_id','name','bbox','method') if k in f}|diagnostic|
                       dict(title_match=relation))
        title_sources[f['frame_id']]=titles
        for r in rows:
            if r.region_id in seen or not args.names or len(r.text)>60:continue
            if any(re.search(r'(?<!非)'+re.escape(normalized(n)), normalized(r.text)) for n in args.names if normalized(n)):
                seen.add(r.region_id);candidates.append(dict(region_id=r.region_id,frame_id=f['frame_id'],layout_id=r.layout_id,
                    bbox=r.bbox,identity='label_candidate',evidence=svc.index.evidence(r)))
    eligible=[c for c in choices if c['eligible']]
    selected=None
    if len(eligible)==1:
        f=eligible[0];titles=title_sources[f['frame_id']]
        selected=dict(scope_id=stable_id('scope',s.request.snapshot_id,svc.drawing['sha256'],f['frame_id']),
            snapshot_id=s.request.snapshot_id,source_sha256=svc.drawing['sha256'],
            **{k:f[k] for k in ('frame_id','layout_id','name','bbox')},
            title_match=f['title_match'],title_evidence=[svc.index.evidence(t) for t in titles],
            title_entity_ids=sorted({eid for t in titles for eid in t.entity_ids}),
            boundary_role='search_scope_only',identity_state='title_and_closed_border_candidate')
        if args.view != 'plan':
            selected.update(view_kind=args.view,border_entity_ids=f['border_entity_ids'],
                datum_status='unconfirmed',role_status='unconfirmed',view_equivalence_status='unconfirmed')
    items=[i for i in candidates if selected and i['frame_id']==selected['frame_id']]
    excluded=[dict(region_id=i['region_id'],frame_id=i['frame_id'],reason='outside_selected_scope' if selected else 'unresolved_scope')
              for i in candidates if i not in items]
    next_calls=remember_scope(s,selected,items,args.names)
    visible_scope=compact_scope(selected)
    if visible_scope and args.view != 'plan':
        visible_scope.update({k:selected[k] for k in
            ('view_kind','datum_status','role_status','view_equivalence_status','border_entity_ids')})
    choice_page=[dict(c,title_region_ids=c['title_region_ids'][:12],
        title_region_ids_truncated=len(c['title_region_ids'])>12,
        conflicting_titles=c['conflicting_titles'][:12],conflicting_title_count=len(c['conflicting_titles']))
        for c in choices[args.offset:args.offset+args.limit]]
    return dict(scope_status='single_candidate' if selected else 'ambiguous' if choices else 'not_found',
        selected_scope=visible_scope, choices=choice_page,
        choice_total=len(choices),choices_has_more=args.offset+args.limit<len(choices),items=items[args.offset:args.offset+args.limit],
        excluded_candidates=excluded[args.offset:args.offset+args.limit],excluded_total=len(excluded),
        excluded_has_more=args.offset+args.limit<len(excluded),total=len(items),has_more=args.offset+args.limit<len(items),next_calls=next_calls,
        operation_receipts=[],
        note=('显式剖面或详图范围尚未确认；请核对原生标题、闭合图框和冲突视图，不能用自拟bbox解除。'
              if args.view != 'plan' and selected is None else
              '唯一合格范围已绑定本次要求；items仅含此范围名称候选，其他图框只保留诊断。按next_calls通过正常工具预算读取子图锚点及局部真实几何，核验边界后测量；范围及名称不是建筑轮廓，也不证明净宽、楼层全覆盖或对象身份。'))


class ProjectRead(Record):
    action: Literal['catalog','search','read'] = 'catalog'
    snapshot_id: str = ''
    query: str = Field(default='',max_length=150)
    reference_kind: Literal['unspecified','external_file','internal_section'] = 'unspecified'
    reference_evidence_id: str = ''
    reference_quote: str = Field(default='',max_length=500)
    region_id: str = ''
    start: int = Field(default=0,ge=0)
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=5,ge=1,le=8)


def project_read(s,args):
    from cad_agent.analysis.project_sources_workflow import run
    return run(s,args)
