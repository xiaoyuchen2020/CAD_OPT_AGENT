"""Question-bound plan labels and actionable validation; no evidence authority.

Composition is explicit: ordered quotes from one requirement may be joined with
list separators. It is not an alias resolver or permission to add requirements.
"""
import re

from pydantic import Field, ValidationError
from cad_agent.contracts import Record
from cad_agent.analysis.typed_requirements import annotate_cell


class CoverageReference(Record):
    requirement_id: str = Field(description='引用 task.subtasks 中已有的原始分项 ID。')
    quote: str = Field(min_length=1, max_length=80, description='该分项内连续、逐字的原文片段。')


BINDING_GUIDANCE = (
    'object/field 优先逐字引用原问题；组合标签须提供同名 object_refs/field_refs，'
    '每项为 {requirement_id, quote}。片段须来自同一原始分项、按原文顺序且不重叠，'
    '标签只能以 、/和/及/与/以及 连接这些片段。字段枚举将拆成独立检查项；'
    '无法绑定的概括应改回原文，执行方法放 actions，不能新增检查要求。'
)
JOIN = r'\s*(?:、|和|及|与|以及)\s*'


class PlanValidationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        first = issues[0]
        super().__init__(f"{first['path']}: {first['message']}")


def issue(path, code, message, **detail):
    return dict(path=path, code=code, message=message, **detail)


def plan_error_feedback(error):
    """Do not hide semantic or Pydantic field errors behind a generic task hint."""
    if isinstance(error, PlanValidationError):
        issues = error.issues
    elif isinstance(error, ValidationError):
        issues = [issue('.'.join(map(str, e['loc'])), e['type'], e['msg'])
                  for e in error.errors(include_url=False, include_context=False, include_input=False)]
    else:
        issues = [issue('steps', 'invalid_plan', str(error)[:400])]
    # A malformed 10x24 plan can produce hundreds of issues. Return a bounded
    # page and an honest count so the feedback cannot exhaust the tool channel.
    shown = issues[:16]
    return dict(code='PLAN_VALIDATION_FAILED', message=f"{len(issues)} 处计划参数无效；" +
                '; '.join(f"{e['path']}: {e['message']}" for e in shown[:2]),
                issues=shown, issue_count=len(issues), has_more_issues=len(issues)>len(shown),
                next_action='修正列出的字段后重新调用 plan_task，保留全部原始 requirement_id；失败计划未写入。',
                binding_contract=BINDING_GUIDANCE)


def _locations(text, quote):
    return [m.start() for m in re.finditer(re.escape(quote), text)]


def bind_label(label, refs, nodes, question, path, requirement_id):
    """Return question-only provenance, distinct from CAD evidence bindings."""
    if not refs:
        for key, node in nodes.items():
            start = node['requirement'].find(label)
            if start >= 0:
                return [dict(requirement_id=key, quote=label, requirement_span=[start, start+len(label)])], []
        # Preserve the previous whole-question literal contract for a span that
        # crosses a lexical requirement boundary; do not fabricate an owner.
        start = question.find(label)
        if start >= 0:
            return [dict(quote=label, question_span=[start, start+len(label)])], []
        return [], [issue(path, 'unbound_question_label', '必须引用用户原问题；非连续组合需显式原文引用。',
                          value=label, requirement_id=requirement_id,
                          requirement_text=nodes[requirement_id]['requirement'][:500],
                          repair=f'将 {path} 改为原文连续片段，或补充 {path}_refs；不要原样重试。')]
    errors, bound = [], []
    owners = {r.requirement_id for r in refs}
    if len(owners) != 1:
        errors.append(issue(path+'_refs', 'mixed_requirement_quotes', '一个组合标签的片段须来自同一原始分项。'))
    cursor = 0
    for index, ref in enumerate(refs):
        rp = f'{path}_refs.{index}'
        if ref.requirement_id not in nodes:
            errors.append(issue(rp+'.requirement_id', 'unknown_requirement', '引用的原始分项不存在。', value=ref.requirement_id))
            continue
        text = nodes[ref.requirement_id]['requirement']
        positions = _locations(text, ref.quote)
        start = next((pos for pos in positions if pos >= cursor), -1)
        if not ref.quote.strip() or start < 0:
            errors.append(issue(rp+'.quote', 'invalid_question_quote', '引用须为该分项内有序、不重叠的连续原文。', value=ref.quote))
            continue
        cursor = start + len(ref.quote)
        bound.append(dict(requirement_id=ref.requirement_id, quote=ref.quote, requirement_span=[start,cursor]))
    pattern = JOIN.join(re.escape(r.quote) for r in refs)
    if not re.fullmatch(pattern, label):
        errors.append(issue(path, 'label_quote_mismatch', '标签必须由所列原文片段依序连接，不能增写或改写内容。', value=label))
    return bound, errors


def _literal_fields(label):
    """Split explicit enumeration, keeping prose, alternatives and formulas whole.

    A conjunction alone is ambiguous (e.g. 饱和密度), so only split a terminal
    conjunction inside an already explicit 、 list. Explicit refs remove this
    ambiguity and are handled separately by normalize_target.
    """
    depth, chunks, start = 0, [], 0
    for index, char in enumerate(label):
        if char in '（([【':depth += 1
        elif char in '）)]】':depth = max(0, depth-1)
        elif char == '、' and depth == 0:
            chunks.append(label[start:index].strip());start=index+1
    if not chunks:return [label]
    chunks.append(label[start:].strip())
    # Do not split alternatives or verbs/conditions into new requested fields.
    tail = re.fullmatch(r'([^，。；：()（）]{2,20}?)(?:以及|及)([^，。；：()（）]{2,20})', chunks[-1])
    if tail:chunks[-1:] = list(tail.groups())
    return chunks if all(chunks) else [label]


def _slice_binding(bindings, quote):
    for binding in bindings:
        start = binding['quote'].find(quote)
        if start < 0:continue
        key = 'requirement_span' if 'requirement_span' in binding else 'question_span'
        origin = binding[key][0]
        return [binding | {'quote':quote, key:[origin+start,origin+start+len(quote)]}]
    return bindings


def normalize_target(target, bindings, step=None):
    # Refs explicitly enumerate field fragments, so they are atomic obligations;
    # objects stay whole because a relation such as 井坑与车位 is a paired object.
    fields = [item for ref in target.field_refs for item in _literal_fields(ref.quote)] if target.field_refs else _literal_fields(target.field)
    result=[]
    for field in dict.fromkeys(fields):
        cell = dict(object=target.object,field=field,
            planning_label=dict(object=target.object,field=target.field),
            question_binding=dict(object=bindings['object'],field=_slice_binding(bindings['field'],field)))
        if getattr(target, 'requirement_type', None):
            cell['declared_requirement_type'] = target.requirement_type
        result.append(annotate_cell(cell, step))
    return result


def validate_coverage(args, nodes, question):
    errors, normalized = [], {}
    for si, step in enumerate(args.steps):
        cells=[]
        for ci, target in enumerate(step.coverage):
            bindings={}
            for key in ('object','field'):
                bindings[key], problems = bind_label(getattr(target,key), getattr(target,key+'_refs'),
                    nodes, question, f'steps.{si}.coverage.{ci}.{key}', step.requirement_id)
                errors.extend(problems)
            cells.extend(normalize_target(target,bindings,nodes[step.requirement_id]))
        normalized[step.requirement_id] = cells
    if errors:raise PlanValidationError(errors)
    return normalized
