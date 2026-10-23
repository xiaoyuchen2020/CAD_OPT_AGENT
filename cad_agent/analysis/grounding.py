"""Source-bound arithmetic only; no expression evaluation or dimensional inference."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Literal

from pydantic import Field
from cad_agent.contracts import Record


from cad_agent.cad.numbers import (
    numbers, numeric_tokens, contains_number, contains_numeric_phrase,
    without_claim_list_markers, matching_quantities, source_contains_number,
)


class Operand(Record):
    quantity_id: str = Field(default="", description="来源 numeric_readings 中的 number_id；重复数值必须指定，编号/型号/歧义项不可计算。")
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
            if not source_contains_number(evidence[operand.evidence_id], operand.value):
                raise ValueError("计算操作数未出现在所指原文中；不能使用问题中的数值代替来源。")
            source = evidence[operand.evidence_id]
            candidates = matching_quantities(source, operand.value, operand.quantity_id)
            if len(candidates) != 1:
                raise ValueError("操作数必须绑定唯一数量；编号、型号或歧义数值不可计算，重复值请指定 quantity_id。")
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
