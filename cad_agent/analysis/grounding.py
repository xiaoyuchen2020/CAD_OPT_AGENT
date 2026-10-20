"""Source-bound arithmetic only; no expression evaluation or dimensional inference."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Literal

from pydantic import Field
from cad_agent.contracts import Record


NUMBER = r"-?\d+(?:\.\d+)?"
_SOURCE_NUMBER = r"[-−]?\d+(?:\.\d+)?"
_NUMBER_TOKEN = re.compile(r"(?<![\d.\-−])" + _SOURCE_NUMBER + r"(?![\d.])")
_QUANTITY_SUFFIXES = {'mm', 'cm', 'm', 'km', 'mm2', 'cm2', 'm2', 'mm3', 'cm3', 'm3',
                      'mm²', 'cm²', 'm²', 'mm³', 'cm³', 'm³', 'h', 'H', 's', 'min',
                      'kg', 'g', 't', 'N', 'kN', 'Pa', 'kPa', 'MPa', 'W', 'kW'}


def _thickness_list_marker(text, start, end):
    """Interpret a line-leading hyphen only in an explicit thickness layer.

    A line boundary and the Chinese thickness suffix are both necessary.
    Unicode minus, ranges, offsets, elevations and identifier separators never
    take this path. Keep the sign in the source span for later auditing.
    """
    return (text[start:start + 1] == '-'
            and not text[text.rfind('\n', 0, start) + 1:start].strip(' \t\r')
            and bool(re.match(r'(?:mm|cm|毫米|厘米)?\s*厚(?!度)(?=[\u3400-\u9fffA-Za-z（(])', text[end:])))


def numeric_tokens(text):
    """Yield source-preserving numeric readings; no unit inference."""
    for match in _NUMBER_TOKEN.finditer(text):
        marker = _thickness_list_marker(text, *match.span())
        yield dict(raw=match.group(), value=match.group()[1:] if marker else match.group(),
                   text_span=list(match.span()),
                   interpretation='thickness_list_marker' if marker else 'literal')


def numbers(text):
    return [token['value'] for token in numeric_tokens(text)]


def _decimal_quantity(text, start, end):
    """Only ordinary quantities may change precision; codes remain literal."""
    token = text[start:end]
    if token.startswith('−'):
        return False  # Preserve a native Unicode sign literally; do not normalize it away.
    integer = token.lstrip('-').split('.')[0]
    if len(integer) > 1 and integer.startswith('0'):
        return False
    if start and re.match(r'[A-Za-z_/.\-−]', text[start - 1]):
        return False
    suffix = re.match(r'[A-Za-z_][A-Za-z_0-9²³]*', text[end:])
    if suffix and suffix.group() not in _QUANTITY_SUFFIXES:
        return False
    return not (text[end:end + 1] in ('/', '_'))


def _equivalent_token(source, start, end, value, value_start, value_end):
    a, b = source[start:end], value[value_start:value_end]
    if _thickness_list_marker(source, start, end):
        a = a[1:]
    if _thickness_list_marker(value, value_start, value_end):
        b = b[1:]
    if a == b:
        return True
    return (a.startswith('-') == b.startswith('-')
            and _decimal_quantity(source, start, end)
            and _decimal_quantity(value, value_start, value_end)
            and Decimal(a) == Decimal(b))


def contains_number(text, value):
    """Check a bound reading, allowing precision and explicit layer markers.

    This is not a unit conversion or a claim-semantic check. Callers must retain
    the source quote, including its units, inequalities and material identifiers.
    Fractions and identifier digits retain the existing literal-only contract.
    """
    if not re.fullmatch(_SOURCE_NUMBER, value):
        # Retain literal fractions; they are not decimal conversions.
        return bool(re.search(r"(?<![\d.\-−])" + re.escape(value) + r"(?![\d.])", text))
    return any(_equivalent_token(text, m.start(), m.end(), value, 0, len(value))
               for m in _NUMBER_TOKEN.finditer(text))


def contains_numeric_phrase(text, value):
    """Match a literal phrase except for safely equivalent numeric precision.

    Compare every nonnumeric character verbatim, so units, inequality signs,
    material names and their order cannot be rewritten by numeric matching.
    """
    tokens = list(_NUMBER_TOKEN.finditer(value))
    if not tokens:
        return False
    parts, previous = [], 0
    for token in tokens:
        parts.extend((re.escape(value[previous:token.start()]), '(' + _NUMBER_TOKEN.pattern + ')'))
        previous = token.end()
    parts.append(re.escape(value[previous:]))
    pattern = ''.join(parts)
    if re.match(r'[A-Za-z_]', value):
        pattern = r'(?<![A-Za-z_])' + pattern
    if re.search(r'[A-Za-z_²³]$', value):
        pattern += r'(?![A-Za-z_\d²³])'
    for match in re.finditer(pattern, text):
        if all(_equivalent_token(text, *match.span(i), value, token.start(), token.end())
               for i, token in enumerate(tokens, 1)):
            return True
    return False


class Operand(Record):
    value: str = Field(pattern=r"^-?\d{1,12}(?:\.\d{1,6})?(?:/\d{1,12})?$", max_length=32)
    evidence_id: str = ""
    observation_id: str = ""
    measurement_ref: str = ""


class Calculation(Record):
    operation: Literal["add", "subtract", "multiply", "divide"]
    operands: list[Operand] = Field(min_length=2, max_length=2)
    result: str = Field(pattern=r"^-?\d{1,15}(?:\.\d{1,6})?$", max_length=24)
    decimal_places: int = Field(default=1, ge=0, le=6)


def verify_calculations(claim, evidence, observations=None):
    """Return allowed new numeric tokens; source and output checks remain mandatory."""
    if not claim.calculations:
        return set()
    if claim.state != "derived":
        raise ValueError("计算结论必须为 derived 且引用原文，不可冒充 observed 或工具测量。")
    allowed = set()
    for calc in claim.calculations:
        values = []
        measured = []
        for operand in calc.operands:
            if operand.observation_id:
                if claim.evidence_basis!="tool_observation" or operand.evidence_id or operand.observation_id not in claim.observation_ids:
                    raise ValueError("测量操作数必须引用该条的工具观测且不能混用原文。")
                obs=(observations or {}).get(operand.observation_id,{})
                if obs.get("tool")!="analyze_measurements":raise ValueError("不是已注册的测量结果集合。")
                refs={v["measurement_ref"]:v for v in obs["data"]["values"]}
                ref=refs.get(operand.measurement_ref)
                if not ref or Decimal(str(ref["value"]))!=Decimal(operand.value):raise ValueError("测量引用或操作数与保存值不符。")
                measured.append(ref);values.append(Decimal(operand.value));continue
            if claim.evidence_basis!="source" or operand.measurement_ref:raise ValueError("原文操作数不能混用测量观测。")
            if operand.evidence_id not in claim.evidence_ids or operand.evidence_id not in evidence:
                raise ValueError("计算操作数必须引用该条已读取的 evidence_ids。")
            if not contains_number(evidence[operand.evidence_id]["quote"], operand.value):
                raise ValueError("计算操作数未出现在所指原文中；不能使用问题中的数值代替来源。")
            try:
                parts = operand.value.split("/")
                with localcontext() as ctx:
                    ctx.prec = 40
                    values.append(Decimal(parts[0]) / Decimal(parts[1]) if len(parts) == 2 else Decimal(parts[0]))
            except (InvalidOperation, ZeroDivisionError):
                raise ValueError("计算操作数无效或分母为零。") from None
        if measured:
            if len(measured)!=2 or len({(v["unit"],v["basis"],v["field"]) for v in measured})!=1:
                raise ValueError("测量运算必须使用相同单位、字段与口径。")
            if calc.operation not in ("add","subtract"):
                raise ValueError("测量乘除需要显式量纲规则，当前不支持。")
        a, b = values
        try:
            with localcontext() as ctx:
                ctx.prec = 40
                if calc.operation == "add": value = a + b
                elif calc.operation == "subtract": value = a - b
                elif calc.operation == "multiply": value = a * b
                else: value = a / b
                rounded = value.quantize(Decimal(1).scaleb(-calc.decimal_places), rounding=ROUND_HALF_UP)
            if rounded != Decimal(calc.result):
                raise ValueError("计算结果与操作数、运算或声明的小数位数不符。")
        except (InvalidOperation, ZeroDivisionError):
            raise ValueError("计算无效、除数为零或超出数值范围。") from None
        if not contains_number(claim.text, calc.result):
            raise ValueError("计算结果未在该条结论中明确列出。")
        allowed.add(calc.result)
    return allowed
