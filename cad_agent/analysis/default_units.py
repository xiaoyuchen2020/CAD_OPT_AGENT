"""Explicit text-unit clauses with an immutable, checked source scope.

This resolves printed linear dimensions, never CAD coordinates, areas, identifiers,
or engineering object identity. Source quotes remain unchanged.
"""
import re


def declaration(text):
    text = re.sub(r'\s+|[“”"「」]', '', text)
    # A plan-only convention must not leak into detail/assembly dimensions.
    if '总平面图尺寸' in text:
        return None
    if not (
        re.search(r'(?:本说明|本图|本工程|施工图|单体建筑).*(?:尺寸|长度|宽度|厚度|高度)', text)
        and re.search(r'未(?:标明|注明|标注)单位|其余(?:尺寸)?(?:均)?以', text)
    ):
        return None
    matches = re.findall(r'(?:均)?以(?:毫米[（(]mm[）)]|厘米[（(]cm[）)]|毫米|厘米|mm|cm)为单位', text, re.I)
    units = {'mm' if '毫米' in m or 'mm' in m.lower() else 'cm' for m in matches}
    if len(units) != 1:
        return None
    return next(iter(units))


def same_frame(a, b):
    keys = ('snapshot_id', 'layout_id', 'frame_id')
    return all(a.get(k) and a.get(k) == b.get(k) for k in keys)


def applicable(a, b):
    if not all(a.get(k) and a.get(k) == b.get(k) for k in ('snapshot_id', 'layout_id')):
        return False
    if a.get('source_sha256') and b.get('source_sha256') and a['source_sha256'] != b['source_sha256']:
        return False
    global_clause = re.search(r'本工程施工图|本工程所注|单体建筑设计中', re.sub(r'\s+', '', b.get('quote', '')))
    return bool(global_clause) or same_frame(a, b)


def candidates(index, region):
    source = dict(snapshot_id=index.snapshot, layout_id=region.layout_id, frame_id=region.frame_id)
    return [r for r in getattr(index, "regions", list(index.by_id.values()))
            if declaration(r.text) and applicable(source, dict(snapshot_id=index.snapshot,
                layout_id=r.layout_id, frame_id=r.frame_id, quote=r.text))]


def resolve(session, quantity, record, quote, tokens):
    """Resolve only a named, already-read clause; never borrow an adjacent unit."""
    eid = quantity.unit_evidence_id
    if not eid:
        return None
    if quantity.origin != 'source' or quantity.unit not in ('mm', 'cm'):
        raise ValueError('独立单位条款只用于原文线性尺寸，不用于几何坐标或其他量纲。')
    unit_source = session.evidence.get(eid, {})
    if not applicable(record, unit_source):
        raise ValueError('数值与默认单位条款须属于同一快照/布局及明确适用范围；局部条款不得跨图框。')
    if declaration(unit_source.get('quote', '')) != quantity.unit:
        raise ValueError('单位引用不是与所填单位一致的明确默认线性尺寸条款。')
    # Restrict applicability to a printed linear property, not material grades,
    # percentages, plan coordinates, elevations or bare numeric table cells.
    linear = [t for t in tokens if (
        re.match(r'\s*厚(?!度)', quote[t['number_span'][1]:])
        or re.search(r'(?:厚度|长度|宽度|高度|净宽)\s*[:：为=]?\s*$',
                     quote[:t['number_span'][0]]))]
    if not linear or re.search(r'标高|坐标|总平面', quote):
        raise ValueError('默认单位仅适用于已明确为厚度、长度、宽度或高度的原文数值。')
    known = [e for e in session.evidence.values() if applicable(record, e)]
    index = getattr(session, 'index', None)
    if index is not None:
        region = index.by_id.get(record.get('region_id'))
        if region is not None:
            known += [{'quote': r.text} for r in candidates(index, region)]
    if any(declaration(e.get('quote', '')) not in (None, quantity.unit) for e in known):
        raise ValueError('同一来源范围存在冲突的默认单位条款，不能自动选择。')
    return dict(evidence_id=eid, quote=unit_source['quote'],
                snapshot_id=unit_source['snapshot_id'], layout_id=unit_source['layout_id'],
                frame_id=unit_source['frame_id'], region_id=unit_source.get('region_id'),
                text_span=unit_source.get('text_span'), unit=quantity.unit,
                basis='source_default_linear_unit',
                applicability='source_declared_building_dimensions' if not same_frame(record, unit_source)
                else 'same_source_frame_linear_property')
