"""Question-bound producer contracts, separate from delivery acceptance.

Types route work; they never confer evidence, execution receipts or completion.
The existing operation ledger remains authoritative for measurements/calculations.
Legacy plans get conservative clause-level migration without changing their text.
"""
import re
from typing import Literal

from cad_agent.analysis.operations import infer_operations
from cad_agent.analysis.unit_policy import user_unit_policy

RequirementType = Literal['SOURCE_FACT', 'SPATIAL_FACT', 'DERIVED_MEASURE',
                          'DERIVED_CALCULATION', 'USER_CONDITION', 'DELIVERY_REQUIREMENT']

CONTRACTS = {
    'SOURCE_FACT': ('source_read', 'registered_source_and_verified_attribution'),
    'SPATIAL_FACT': ('spatial_observation', 'source_bound_geometry_and_identity_review'),
    'DERIVED_MEASURE': ('measurement', 'fulfilled_operation_receipt_and_source_geometry'),
    'DERIVED_CALCULATION': ('calculation', 'fulfilled_arithmetic_receipt_and_verified_operands'),
    'USER_CONDITION': ('user_question', 'verbatim_question_binding_not_drawing_fact'),
    'DELIVERY_REQUIREMENT': ('delivery_validation', 'validated_delivery_against_producer_results'),
}
NON_PRODUCERS = {'USER_CONDITION', 'DELIVERY_REQUIREMENT'}
VERSION = 'typed-requirements-v1'


def _clause(cell, step):
    """Prefer the field's bound sentence, avoiding unrelated shared constraints."""
    text = step.get('requirement', '')
    field = cell.get('field', '')
    clauses = re.split(r'[。；;，,\n]', text)
    return next((part for part in clauses if field and field in part), text)


def _infer(cell, step):
    obj, field = cell.get('object', ''), cell.get('field', '')
    clause = _clause(cell, step)
    label = obj + ' ' + field if cell else clause
    if (re.search(r'按(?:原生)?位置|从左到右|同栏.{0,5}从上到下|跨栏位置排序', obj + ' ' + field)
            and re.search(r'排序|筛选|选择|按(?:原生)?位置|从左到右', clause)):
        return 'DELIVERY_REQUIREMENT'
    # Acceptance verbs about the answer or checked work describe delivery, not
    # a property printed in the drawing. Object names are never enumerated here.
    if ((re.search(r'用户(?:给定|提供|输入|指定)|输入(?:参考)?值|原图值|图纸值|图面读数', label)
                and re.search(r'分列|区分|分别列|分别标明|分别标注|分别注明|单列', field or clause))
            or re.search(r'(?:已检查|已核查|未覆盖|未检查|未核查).{0,5}(?:范围|部分|区域)', label)
            or re.fullmatch(r'已确认(?:几何|事实|结果)|候选归属|证据状态|核查状态', obj)
            or re.fullmatch(r'已确认(?:几何|事实|结果)|候选归属|证据状态|核查状态', field)
            or (re.search(r'尚缺|未确认|待核查', obj) and re.fullmatch(r'记录|说明|分别记录', field))
            or re.search(r'可定位证据|证据引用|引用编号|回答格式|输出格式', label)
            or re.fullmatch(r'回答|答复|输出|结果表', obj)):
        return 'DELIVERY_REQUIREMENT'
    from cad_agent.analysis.business_table_routing import literal_business_read
    if literal_business_read(clause):
        return 'SOURCE_FACT'
    # A requested printed value remains a source fact even if its label names
    # a sum/difference. A separate compute clause has its own producer.
    if (re.search(r'原文|标注|注记|文字|尺寸链|分尺寸|定位尺寸', field)
            or (re.search(r'读取|摘录|提取', clause) and re.search(r'原文|标注|注记', clause))):
        if not re.search(r'计算|求和|算出|减去|乘以|除以|之差', field + ' ' + clause):
            return 'SOURCE_FACT'
    # Arithmetic is still work when its operands include a user-supplied value.
    operations = set(infer_operations(field if cell else clause))
    if 'arithmetic' in operations or re.search(r'算术|数值关系|计算所得|计算结果|相差|差值', field or clause):
        return 'DERIVED_CALCULATION'
    if ('ranking' in operations or re.search(r'从大到小|从小到大', field or clause)):
        return 'DERIVED_CALCULATION'
    # An explicit read of annotations/wording is not a geometric computation.
    if re.search(r'原文|标注|注记|文字|尺寸链|分尺寸|定位尺寸', field):
        return 'SOURCE_FACT'
    # Legacy list parsing can turn relation operands into object/field pairs.
    # The enclosing relation clause still requires geometry, never a literal
    # document phrase containing both operand labels.
    if (field and field in clause and re.search(r'的(?:重叠|相交|交叠|连通|相邻)(?:关系)?[。？?]?$', clause)
            and not re.search(r'读取|摘录|标注|原文', clause)):
        return 'DERIVED_MEASURE'
    if re.search(r'重叠|净距|相交|交叠|连通|相邻关系|距离|净宽|周长', field):
        if re.search(r'读取|摘录|原文|标注|注记', clause) and not re.search(r'测量|计算|几何', clause):
            return 'SOURCE_FACT'
        return 'DERIVED_MEASURE'
    if re.search(r'测量|测距|量出|实测', field + ' ' + clause):
        # A unit instruction supplies a condition, not an executed measurement.
        if user_unit_policy(clause) and not cell and not re.search(r'面积|周长|距离|宽度|长度|厚度', clause):
            return 'USER_CONDITION'
        if not field or re.search(r'面积|周长|距离|宽度|长度|厚度|净宽|测量|测距', field):
            return 'DERIVED_MEASURE'
    if re.search(r'定位|位置|开启范围|开门范围|轮廓|边界|高亮', field):
        # Original textual/title/table locations continue through source reads.
        if re.search(r'原文|文字|说明|表格|标题|注记', obj + field) or obj.endswith('表'):
            return 'SOURCE_FACT'
        return 'SPATIAL_FACT'
    if (re.search(r'用户(?:给定|提供|输入|指定)|内部(?:参考|比较|筛选|对比)', obj)
            or (not cell and re.search(r'用户(?:给定|提供|输入|指定)|仅作为.{0,8}(?:参考|清单|标签)', clause))
            or (not cell and user_unit_policy(clause))):
        return 'USER_CONDITION'
    if not cell and re.search(r'^(?:本题|本次|请)?(?:只|仅).{0,12}(?:整理|回答)|不作.{0,20}(?:结论|方案)|(?:记录|说明).{0,15}(?:缺口|未覆盖)', clause):
        return 'DELIVERY_REQUIREMENT'
    return 'SOURCE_FACT'


def requirement_type(cell, step=None):
    """Infer legacy types; never let a declared source hide a computed target."""
    if step is None and cell.get('type_contract_version') == VERSION:
        return cell['requirement_type']
    step = step or {}
    inferred = _infer(cell, step)
    declared = cell.get('declared_requirement_type')
    # Automatically attached types are recomputed on revision; explicit types
    # help with unknown grammar, but cannot weaken a recognized producer.
    if inferred != 'SOURCE_FACT':
        return inferred
    if declared in CONTRACTS:
        if declared in NON_PRODUCERS:
            # Model declarations cannot turn a literal source obligation into
            # acceptance-only prose or an unverified user value.
            return inferred
        return declared
    return inferred


def annotate_cell(cell, step=None):
    kind = requirement_type(cell, step)
    producer, acceptance = CONTRACTS[kind]
    cell.update(requirement_type=kind, producer=producer,
                acceptance_contract=acceptance, type_contract_version=VERSION)
    return cell


def is_source_fact(cell, step=None):
    return requirement_type(cell, step) == 'SOURCE_FACT'


def annotate_step(step):
    for cell in step.get('coverage', []):
        annotate_cell(cell, step)
    kinds = sorted({cell['requirement_type'] for cell in step.get('coverage', [])})
    if not kinds:
        kinds = [requirement_type({}, step)]
    step['requirement_types'] = kinds
    step['producer_requirements'] = [kind for kind in kinds if kind not in NON_PRODUCERS]
    step['acceptance_only'] = not step['producer_requirements']
    # Preserve all original operation lower bounds and require real tool
    # execution for a newly typed numeric/spatial obligation.
    operations = set(step.get('operations', []))
    for cell in step.get('coverage', []):
        kind, field = cell['requirement_type'], cell['field']
        if kind == 'SPATIAL_FACT':
            operations.add('geometry')
        elif kind == 'DERIVED_MEASURE':
            operations.add('area' if '面积' in field else 'perimeter' if '周长' in field else 'geometry')
            if re.search(r'重叠|相交|交叠|连通|相邻关系', field + ' ' + _clause(cell, step)):
                operations.add('relations')
        elif kind == 'DERIVED_CALCULATION':
            operations.add('ranking' if re.search(r'排序|从大到小|从小到大', field) else 'arithmetic')
    step['operations'] = sorted(operations)
    return step


def accepts_source_prerequisites(step):
    """Only actual producers may continue explicit source reads/pagination."""
    kinds = {requirement_type(cell, step) for cell in step.get('coverage', [])}
    if not kinds:
        kinds = {requirement_type({}, step)}
    return bool(kinds - NON_PRODUCERS)
