"""Shared delivery gates for source-bound spatial objects and method results."""
import re
import json


def spatial_measurement_issues(claim, observations):
    """A matching raw number cannot authorize inventing its physical unit."""
    from cad_agent.analysis.grounding import contains_number
    values = {}
    conditional = False
    def visit(node, source_unit):
        nonlocal conditional
        if isinstance(node, list):
            for item in node:
                visit(item, source_unit)
        elif isinstance(node, dict):
            conditional = conditional or node.get('conditional') is True
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit(value, source_unit)
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    unit = ('m2' if key.endswith('_m2') else 'm' if key.endswith('_m') else
                            source_unit + '2' if key.endswith('_drawing_units_squared') and source_unit else
                            source_unit if key.endswith('_drawing_units') else None)
                    if unit:
                        values.setdefault(unit, []).append(value)
    for data in observations:
        unit = data.get('unit', {})
        source_unit = unit.get('unit', unit.get('value', ''))
        visit(data.get('measurements', {}), source_unit)
        for record in data.get('measurement_evidence', []):
            inner_unit = record.get('unit', {})
            visit(record.get('measurements', {}), inner_unit.get('unit', inner_unit.get('value', '')))
    aliases = {'平方米': 'm2', '㎡': 'm2', 'm²': 'm2', 'm2': 'm2',
               '平方毫米': 'mm2', 'mm²': 'mm2', 'mm2': 'mm2',
               '平方厘米': 'cm2', 'cm²': 'cm2', 'cm2': 'cm2',
               '毫米': 'mm', 'mm': 'mm', '厘米': 'cm', 'cm': 'cm', '米': 'm', 'm': 'm'}
    pattern = r'(-?\d+(?:\.\d+)?)\s*(' + '|'.join(sorted(aliases, key=len, reverse=True)) + r')(?![A-Za-z0-9²])'
    issues = []
    for match in re.finditer(pattern, claim.text, re.I):
        number, label = match.groups()
        if not contains_number(json.dumps(values.get(aliases[label.lower()], [])), number):
            issues.append(f'空间数值 {number}{label} 没有对应单位的工具测量支持；原图单位量不能直接改写成物理量。')
    if conditional and re.search(pattern, claim.text, re.I) and not re.search(r'假设|约定|暂按|条件|用户|题设|给定', claim.text):
        issues.append('空间测量使用条件性单位或基准，结论须明确该条件，不能写成已确认的图纸实测。')
    return issues


def spatial_numeric_claim_text(text):
    # Unit exponents are checked by the typed-unit gate, not additional
    # measured values. Keep identifier digits (for example M20) intact.
    return re.sub(r'(?<![A-Za-z])((?:mm|cm|m))2(?![A-Za-z0-9])', r'\1', text, flags=re.I)


def is_spatial_fact(data):
    return data.get('kind') == 'spatial_object' or bool(data.get('spatial_fact_scope'))


def spatial_claim_issues(claim, data):
    if not is_spatial_fact(data):
        return []
    issues = []
    scope = data.get('spatial_fact_scope')
    scope = scope if isinstance(scope, dict) else data
    role = scope.get('role', data.get('role', {}))
    identity = scope.get('identity', data.get('identity', {}))
    unconfirmed = (role.get('status') == 'asserted'
                   or identity.get('engineering_confirmed') is not True)
    if not unconfirmed:
        return issues
    if claim.state not in ('derived', 'unknown', 'conflict'):
        issues.append('空间对象仅绑定原生来源，工程身份/范围未确认；须保留 derived/unknown/conflict，不能标 observed。')
    for match in re.finditer(r'(?:已确认|已核实|确定为|确认为|就是).{0,16}(?:建筑|房间|主体|轮廓|净边界|净面积|净宽|工程对象)', claim.text):
        if not re.search(r'不能|无法|尚未|未能|并非|不代表', claim.text[max(0,match.start()-8):match.start()]):
            issues.append('空间来源绑定未确认工程对象身份或净边界；不能将候选角色写成已确认工程对象。')
            break
    return issues


def spatial_numeric_evidence(data):
    """Only explicit method results can justify a numerical spatial conclusion.

    Source coordinates, labels and identifiers remain locators, not dimensions.
    The same contract applies to future methods without a tool-name allowlist.
    """
    blocked = {'bbox', 'points', 'coordinates', 'paths', 'vertices', 'endpoints', 'geometry', 'source',
               'identity', 'role', 'labels', 'label', 'rationale', 'note',
               'source_sha256', 'measurement_basis', 'diagnostics', 'next_calls',
               'unit', 'units', 'applied_unit', 'unit_basis', 'version', 'algorithm_version'}
    def values(value):
        if isinstance(value, list):
            return [number for item in value for number in values(item)]
        if isinstance(value, dict):
            return [number for k,v in value.items()
                    if k not in blocked and not k.endswith(('_id','_ids','_hash'))
                    and k not in ('entity_id','record_id') for number in values(v)]
        # Unit names (m2), result keys (area_m2), text, and method versions do
        # not supply operand digits. Measurement producers expose typed scalars.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return [value]
        if isinstance(value, str) and re.fullmatch(r'-?\d+(?:\.\d+)?', value):
            return [value]
        return []
    return [number for key in ('measurements','result','measurement_evidence')
            if key in data for number in values(data[key])]
