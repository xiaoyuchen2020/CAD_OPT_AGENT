"""Explicit arithmetic obligations; never discover operands by numeric proximity."""
from copy import deepcopy
import re


def required_operations(text):
    # Negated alternatives are not requests (e.g. never average zone ratios).
    text = re.sub(r'(?:不能|不要|不得|不应|无需|不必)[^，,。；;]*', '', text)
    patterns = {
        'sum': r'求和|合计|总和|总厚度|汇总',
        'subtract': r'差值|厚度差|面积差|之差|相差|数量.{0,5}差异',
        'divide': r'比值|占比|比例|加权指标|面积\s*[/／]\s*(?:车位|停车)|除以|总面积\s*[/／]',
        'mean': r'平均', 'ceil': r'ceil\s*\(|向上取整',
        'floor': r'floor\s*\(|向下取整', 'multiply': r'乘以|[×*]\s*\d',
    }
    return sorted(op for op, pattern in patterns.items() if re.search(pattern, text, re.I))


def distributed(text):
    return bool(re.search(r'逐区|逐项计算|每区|各分区|每个.{0,12}分区|两种.{0,15}做法|分别.{0,12}(求|计算)', text))


def explicit_targets(text):
    """Only an explicit user-owned list, never a source snippet or a numeric guess.

    Unknown natural-language enumeration stays unknown. A title, one computed
    target, or a model-authored label cannot establish a complete target set.
    """
    for match in re.finditer(r'(?:计算|求|统计|列出)([^，,。；;]{1,160}?)的',text):
        raw=match.group(1).strip()
        if re.search(r'每个|所有|各个|等|每区|各分区',raw):continue
        targets=[v.strip() for v in re.split(r'、|以及|和|与|及',raw)]
        if len(targets)>1 and all(targets) and len(set(targets))==len(targets):
            return targets
    return None



def operand_binding(record, origin, reference, *, field='', basis='', quote=''):
    """Copy tool/source-owned identities, never labels supplied by a model."""
    keys = ('snapshot_id', 'source_sha256', 'region_id', 'layout_id', 'frame_id',
            'scope_id', 'object_id', 'entity_ids', 'text_span', 'source_parts',
            'verification_status', 'measurement_basis', 'applied_unit', 'status',
            'measurement_id', 'measurement_ref', 'source_entity_ids', 'evidence_ids', 'coordinate_space', 'unit',
            'batch_id', 'basis_comparability', 'association', 'annotation_basis', 'unit_basis', 'identity_status')
    binding = {key: deepcopy(record[key]) for key in keys if record.get(key) is not None}
    binding.update(origin=origin, reference=reference, field=field, basis=basis, quote=quote)
    return binding


def source_target_matches(quote, target, question):
    """Narrow literal subject path; adjacent table rows/numbers are not joins.

    Supports a one-line source sentence such as 东库面积100㎡，停车数5辆.
    A source region containing multiple requested subjects stays unbound.
    """
    if not target or re.search(r'[，,。；;\n]',target) or not quote.strip().startswith(target):
        return False
    if '\n' in quote or re.search(r'[。；;]', quote.strip().rstrip('。；;')):
        return False
    predicates=re.split(r'[，,]',quote.strip()[len(target):])
    # Only complete property/value clauses establish the literal subject path.
    # Competing subjects, alternative counts and parenthetical subsets need
    # a structured source binding; they cannot be guessed from a first label.
    property_value=(r'(?:的)?(?:面积|停车数|车位数|数量|厚度|长度|宽度|净宽|砂浆总厚度|总厚度)'
                    r'\s*(?:为|[:：=])?\s*(?:约)?\s*-?\d+(?:\.\d+)?\s*'
                    r'(?:mm[²2]?|cm[²2]?|m[²2]?|㎡|平方米|毫米|厘米|米|辆|个|车位)\s*[。.]?')
    if not all(re.fullmatch(property_value, p.strip(), re.I) for p in predicates):
        return False
    targets = explicit_targets(question) or []
    return not any(other != target and other in quote for other in targets)


def calculation_binding(args, resolved, question):
    """Arithmetic execution and semantic identity are separate, inspectable states."""
    physical = [r for r in resolved if r['origin'] != 'user_input']
    leaves = [b for r in physical for b in r['lineage'] if b['origin'] != 'user_input']
    objects = {b.get('object_id') for b in leaves if b.get('object_id')}
    scopes = {(b.get('snapshot_id'), b.get('layout_id'), b.get('frame_id'), b.get('scope_id'))
              for b in leaves if b.get('snapshot_id') and (b.get('frame_id') or b.get('scope_id'))}
    object_complete = bool(leaves) and all(b.get('object_id') for b in leaves)
    scope_complete = bool(leaves) and all(b.get('snapshot_id') and (b.get('frame_id') or b.get('scope_id')) for b in leaves)
    # Identity is namespace-bound: an object id repeated across drawings is not
    # a proof that the same physical object was measured in both drawings.
    snapshots = {b.get('snapshot_id') for b in leaves if b.get('snapshot_id')}
    same_object = object_complete and len(objects) == 1 and len(snapshots) <= 1 and len(scopes) <= 1
    same_scope = scope_complete and len(scopes) == 1
    target_bound = bool(physical) and bool(args.target) and all(
        (r['origin'] == 'source' and source_target_matches(r['quote'], args.target, question)) or
        (r['origin'] == 'calculation' and r.get('upstream_target') == args.target and
         r.get('upstream_target_binding_status') == 'source_bound') or
        (r.get('binding', {}).get('object_id') == args.target and bool(r.get('binding', {}).get('object_id')))
        for r in physical)
    if len(scopes) > 1 or len(snapshots) > 1 or any(r['unit_status']!='explicit' for r in physical):
        target_bound = False
    gaps = []
    if args.operand_relation == 'same_object' and not same_object:
        # Literal one-subject source records provide a reachable binding path
        # for annotations without a separate object registry.
        if not target_bound:
            gaps.append(dict(label='同一对象的操作数对应关系', reason='来源对象缺失或冲突；模型标签及数字邻近不能证明同一对象。',
                             required_source='读取同一对象的结构化 object_id，或逐字包含目标名称及数值的单主体原文。', unit=''))
    if args.operand_relation == 'same_scope' and not same_scope:
        gaps.append(dict(label='同一范围的操作数对应关系', reason='来源范围缺失或冲突。',
                         required_source='读取一致 snapshot_id 与 frame_id/scope_id 的来源记录。', unit=''))
    if args.operand_relation != 'independent':
        for i,r in enumerate(resolved):
            if r['origin']!='user_input' and r['unit_status']!='explicit':
                gaps.append(dict(label=f'操作数{i+1}的单位',reason='原文仅提供数字，尚未确认工程量单位。',
                    required_source='读取该操作数直接绑定的单位依据；独立数值差可另用 independent 计算。',unit=''))
    return dict(operand_relation=args.operand_relation,
                relation_status=('source_bound' if not gaps and ((args.operand_relation == 'same_object' and (same_object or target_bound)) or
                                 (args.operand_relation == 'same_scope' and same_scope)) else
                                 'unresolved' if gaps else 'independent_numeric'),
                target_binding_status='source_bound' if target_bound else 'unconfirmed',
                source_object_ids=sorted(objects), source_scopes=[list(s) for s in sorted(scopes, key=str)],
                lineage=deepcopy([b for r in resolved for b in r['lineage']])), gaps

def record_calculation(data, ids, name, result):
    if name != 'calculate_facts' or result.get('error'):
        return
    plans = data.setdefault('calculation_workflows', {})
    for owner in ids:
        key = owner + ':' + result['calculation_key']
        plans[key] = dict(task_ids=[owner], **{k: deepcopy(result[k]) for k in
            ('calculation_key', 'operation', 'target', 'coverage_scope', 'status', 'inputs',
             'missing_operands', 'value', 'unit', 'observation_id', 'operand_relation',
             'relation_status', 'target_binding_status', 'lineage') if k in result})


def outstanding(data, step):
    if 'arithmetic' not in step.get('operations', []):
        return []
    plans = [p for p in data.get('calculation_workflows', {}).values() if step['id'] in p['task_ids']]
    missing = [dict(operation='calculation_operands', task_ids=[step['id']], blocking=True,
                    calculation_key=p['calculation_key'], target=p['target'],
                    missing_operands=deepcopy(p.get('missing_operands', [])),
                    next_action='读取明确对象的缺项及单位；不得从首个数字、邻近文字或其他对象补值。')
               for p in plans if p['status'] != 'calculated']
    done = set()
    for receipt in step.get('operation_receipts', []):
        if not receipt.get('fulfilled'):
            continue
        op = receipt.get('calculation_operation')
        if op:
            done.add(op)
        done.update(receipt.get('calculation_operations',[]))
    required = set(required_operations(step['requirement']))
    remaining = sorted(required - done)
    if remaining:
        missing.append(dict(operation='calculation_execution', task_ids=[step['id']], blocking=True,
            required_operations=remaining, tool='calculate_facts',
            next_action='为各计算明确操作数、单位、来源及对象口径；齐全后实际调用。缺项时用 missing_operands 记录具体缺口后部分交付。'))
    if distributed(step['requirement']):
        targets=explicit_targets(step['requirement'])
        remaining_targets=[]
        for target in targets or []:
            executed={p['operation'] for p in plans if p['coverage_scope']=='single' and
                      p['target']==target and p['status']=='calculated' and p.get('target_binding_status')=='source_bound'}
            missing_ops=sorted(required-executed)
            if missing_ops or not executed:
                remaining_targets.append(dict(target=target,required_operations=missing_ops))
        if targets is None or remaining_targets:
            missing.append(dict(operation='calculation_target_coverage',task_ids=[step['id']],blocking=True,
                target_set_status='explicit_user_list' if targets else 'unconfirmed',
                required_targets=targets or [],missing_target_operations=remaining_targets,
                reason=('逐对象算式未全部执行；target 必须逐字对应要求中的对象，合计不能替代。' if targets else
                        '用户要求逐对象计算，但目标全集尚未明确；一个已计算对象或模型填写的目标名不证明覆盖完成。')))
    if re.search(r'同一(?:对象|部位|剖面|位置)|对应位置', step['requirement']):
        verified = {p['operation'] for p in plans if p['status']=='calculated' and
                    p.get('relation_status')=='source_bound' and p.get('operand_relation')=='same_object'}
        if required - verified:
            missing.append(dict(operation='calculation_identity', task_ids=[step['id']], blocking=True,
                required_operations=sorted(required-verified),
                next_action='以 operand_relation=same_object 调用，并读取同一对象的来源身份；独立数字差不能替代同部位工程量。'))
    return missing


def pending_calculations(data):
    refresh_calculation_inputs(data)
    pending=[]
    for step in data['subtasks']:
        if step['status'] in ('waiting_input', 'blocked_capability'):
            continue
        work=outstanding(data,step)
        context=data.get('calculation_inputs',{}).get(step['id'])
        if work and context:
            # Attach one bounded packet to the existing obligation, avoiding a
            # second parallel queue or any implicit arithmetic completion.
            work[0]['input_context']=calculation_input_context(context)
            work[0]['next_calls']=deepcopy(context['next_calls'][:3])
        pending.extend(work)
    return pending


def calculation_input_context(context):
    """Small model-facing view; persisted inventory retains exact source refs."""
    return {key:deepcopy(context[key]) for key in ('status','required_operations','missing_operands','operand_count','inventory_truncated')} | {
        'operands':[{key:deepcopy(row[key]) for key in ('value','unit','role','status','quantity','reference','target_binding_status') if key in row}
                    | {'quote':row.get('quote','')[:160]} for row in context['operands'][:12]],
        'omitted_operands':max(0,len(context['operands'])-12),
        'automatic_join':False,
    }


def refresh_calculation_inputs(data):
    """Organize already read inputs without calling tools or guessing operands.

    Inventory is visibility, not applicability. Only a complete literal single
    subject sentence can create an executable proposal; alternatives, layer
    collections, tables, and unowned values require an explicit tool call.
    """
    from types import SimpleNamespace
    from cad_agent.analysis.fact_tools import calculation_tokens, CalculateFacts, calculate_facts
    evidence=data.get('evidence',{})
    observations=data.get('observations',{})
    question=data.get('question','')
    session=SimpleNamespace(evidence=evidence,observations=observations,request=SimpleNamespace(question=question))
    inventories={}
    for step in data.get('subtasks',[]):
        if 'arithmetic' not in step.get('operations',[]):continue
        owner_ids={step['id'],*step.get('depends_on',[])}
        owners=[s for s in data.get('subtasks',[]) if s['id'] in owner_ids]
        eids={e for s in owners for e in s.get('evidence_ids',[]) if e in evidence}
        oids={o for s in owners for o in s.get('observation_ids',[])}
        targets=explicit_targets(step['requirement']) or []
        if not targets:
            single=re.search(r'(?:计算|求)([^，,。；;]{1,80}?)的',step['requirement'])
            if single and not re.search(r'每|各|所有|等|以及|和|与|及|、',single[1]):targets=[single[1].strip()]
        # Sources acquired by a sibling requirement can be reused only as an
        # explicit single-subject record for the requested target.
        eids.update(eid for eid,e in evidence.items() if any(
            source_target_matches(e.get('quote',''),target,question) for target in targets))
        rows=[]
        for eid in sorted(eids):
            e=evidence.get(eid,{})
            quote=e.get('quote','')
            for token in calculation_tokens(quote):
                quantity=dict(origin='source',evidence_id=eid,value=token['value'],unit=token['unit'])
                rows.append(dict(**token,quantity=quantity,reference=eid,
                    binding=operand_binding(e,'source',eid),quote=quote[:500],
                    target_binding_status='unconfirmed',automatic_join=False))
        # Only explicitly labelled user conditions become inventory inputs.
        # Preserve clause text including comparators/ranges; never choose an
        # endpoint or turn these conditions into drawing facts.
        for clause in re.split(r'[。；;\n]',question):
            if not re.search(r'用户(?:给定|提供|参考)|题设|假设|给定(?:值|数值|参考)',clause):continue
            if len(clause)>300:continue
            for token in calculation_tokens(clause):
                rows.append(dict(**token,quantity=dict(origin='user_input',question_quote=clause,value=token['value'],unit=token['unit']),
                    reference='current_question',quote=clause,conditional=True,
                    target_binding_status='user_condition_only',automatic_join=False))
        for oid in sorted(oids):
            obs=observations.get(oid,{})
            d=obs.get('data',{});tool=obs.get('tool')
            candidates=[]
            if tool=='analyze_measurements':
                candidates=[dict(origin='measurement',observation_id=oid,measurement_ref=r.get('measurement_ref',''),
                    value=str(r.get('value')),unit=r.get('unit','').replace('²','2')) for r in d.get('values',[]) if r.get('value') is not None]
            elif tool in ('measure_geometry','measure_room','measure_room_area'):
                candidates=[dict(origin='measurement',observation_id=oid,field=field,value=str(d[field]),
                    unit=d.get('unit','').replace('²','2') if field=='value' else 'm' if field=='perimeter_m' else 'm2')
                    for field in ('value','area_m2','candidate_area_m2','basis_area_m2','perimeter_m') if d.get(field) is not None]
            elif tool in ('analyze_room_batch','select_room_boundaries','resolve_room_units'):
                measurement=d.get('measurement',d)
                for row in measurement.get('items',[]):
                    for field in ('area_m2','candidate_area_m2'):
                        if row.get(field) is not None:
                            candidates.append(dict(origin='measurement',observation_id=oid,measurement_ref=row.get('region_id',''),
                                field=field,value=str(row[field]),unit='m2'))
                    annotations=row.get('area_annotation_checks',[])
                    if len(annotations)==1 and annotations[0].get('value') is not None:
                        candidates.append(dict(origin='measurement',observation_id=oid,measurement_ref=row.get('region_id',''),
                            field='annotation_area_m2',value=str(annotations[0]['value']),unit='m2'))
            elif tool=='calculate_facts' and d.get('status')=='calculated' and d.get('value') is not None:
                candidates=[dict(origin='calculation',observation_id=oid,value=str(d['value']),unit=d.get('unit',''))]
            for quantity in candidates:
                try:
                    # Validation only: a one-input identity sum is never saved,
                    # returned as a calculation, or awarded a receipt.
                    check=calculate_facts(session,CalculateFacts(operation='sum',quantities=[quantity]))
                except (ValueError,KeyError,TypeError):continue
                operand=check['inputs'][0]
                rows.append(dict(value=quantity['value'],unit=quantity['unit'],role=quantity.get('field','value'),
                    status='candidate_numeric' if operand['candidate'] else 'available_numeric',quantity=quantity,
                    reference=oid,binding=operand['binding'],target_binding_status='unconfirmed',automatic_join=False))
        required=required_operations(step['requirement'])
        calls=[]
        # Strict and reachable fast path: area/count or length/width in one
        # literal target sentence. Never sum a partly read assembly or pair
        # values merely because they share a frame, unit, or region.
        for target in targets:
            sources=[eid for eid in eids if source_target_matches(evidence[eid].get('quote',''),target,question)]
            # Repeated source records might be duplicate or competing values.
            # Leave deduplication to explicit source/object workflows.
            if len(sources)!=1:continue
            subject_rows=[r for r in rows if r['reference']==sources[0] and r['status']=='available_numeric']
            for op,roles in [('divide',('面积',('停车数','车位数'))),('multiply',('长度',('宽度',)))]:
                if op not in required:continue
                left=[r for r in subject_rows if r['role']==roles[0]]
                right=[r for r in subject_rows if r['role'] in roles[1]]
                if len(left)!=1 or len(right)!=1 or len(subject_rows)!=2:continue
                args=dict(operation=op,target=target,operand_relation='same_object',coverage_scope='single',
                    calculation_key=op+':'+target,quantities=[left[0]['quantity'],right[0]['quantity']])
                if any(p.get('status')=='calculated' and step['id'] in p.get('task_ids',[]) and
                       p.get('operation')==op and p.get('target')==target for p in data.get('calculation_workflows',{}).values()):continue
                try:preview=calculate_facts(session,CalculateFacts(**args))
                except (ValueError,KeyError,TypeError):continue
                if preview['status']=='calculated' and preview['target_binding_status']=='source_bound':
                    calls.append(dict(tool='calculate_facts',arguments=dict(task_ids=[step['id']],**args),
                        status='ready_to_execute',reason='单主体原文完整绑定两个操作数及单位；尚未执行。'))
        gaps=[]
        if not rows:gaps.append(dict(label='来源操作数',reason='已读范围尚无可列出的数值；需读取目标对象原文或确定性测量。'))
        unavailable={r['status'] for r in rows}-{'available_numeric','candidate_numeric'}
        if unavailable:gaps.append(dict(label='单位或数值性质',reason='存在编号、未确认单位或字段单位冲突；不能借用几何单位约定或相邻单位。',statuses=sorted(unavailable)))
        if not calls:gaps.append(dict(label='算式及对象对应关系',reason='尚无完整且唯一的同目标算式；按 quantity 引用核对对象、范围、单位和角色，缺项用 missing_operands 记录。'))
        plans=[p for p in data.get('calculation_workflows',{}).values() if step['id'] in p.get('task_ids',[])]
        for plan in plans:gaps.extend(deepcopy(plan.get('missing_operands',[])))
        inventories[step['id']]=dict(status='ready_to_execute' if calls else 'needs_binding',required_operations=required,
            operand_count=len(rows),operands=rows[:128],inventory_truncated=len(rows)>128,
            missing_operands=gaps[:12],next_calls=calls)
    data['calculation_inputs']=inventories
    return inventories
