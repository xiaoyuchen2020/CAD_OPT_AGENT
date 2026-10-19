"""Explicit notes adjacent to a split index, retaining the original circle text.

An atlas page is a different namespace from a drawing sheet. Association remains
a geometric candidate: competing notes are exposed and never silently selected.
"""
import re

from shapely.geometry import Point

from .annotations import Nearby


def reference_scope(index, ref):
    raw = ref['sheet_number']
    result = dict(raw_sheet_number=raw, effective_sheet_number=raw,
                  document_kind='drawing_sheet', state='circle_text', notes=[],
                  evidence=[], diagnostics=[])
    if ref.get('method') != 'divided_circle_text':
        return result
    x, y = ref['center']; r = ref['radius']
    texts = [t for t in index.scene['texts'] if t['layout_id'] == ref['layout_id']]
    nearby = Nearby(texts, lambda t: Point(t['origin'][:2]))
    for t in nearby.query([x-18*r, y-2*r, x+6*r, y+2*r]):
        a, b, c, d = t['bbox']
        # Same annotation band, horizontally adjacent, similar lettering size.
        if abs((t.get('rotation', 0)+180)%360-180) > .1 or not .3*r <= t['height'] <= 1.5*r:
            continue
        if not -1.8*r <= t['origin'][1]-y <= 1.8*r:
            continue
        if min(abs(c-(x-r)), abs(a-(x+r))) > 5*r:
            continue
        text = re.sub(r'\s+', '', t['text'])
        atlas = re.search(r'(?:图集|参)(\d{2}[A-Za-z]{1,3}\d{2,4}(?:-\d+)?)$', text)
        sheet = re.fullmatch(r'(?:详见|参见|见)(?:图号[:：]?)?([A-Za-z][A-Za-z0-9.-]{1,20})', text)
        if atlas and raw.isdigit():
            kind, value = 'standard_atlas', atlas[1]
        elif sheet:
            kind, value = 'drawing_sheet', sheet[1]
        else:
            continue
        result['notes'].append(dict(document_kind=kind, value=value,
                                    entity_id=t['entity_id'], bbox=t['bbox'], quote=t['text']))
    choices = {(n['document_kind'], n['value'].upper()) for n in result['notes']}
    result['evidence'] = [e for n in result['notes'] for e in index.entity_evidence(n['entity_id'])]
    if len(choices) > 1:
        result.update(state='conflict', effective_sheet_number=None)
        result['diagnostics'].append('COMPETING_REFERENCE_SCOPE_NOTES')
    elif choices:
        kind, value = next(iter(choices))
        if kind == 'standard_atlas':
            result.update(document_kind=kind, atlas_number=value, page_number=raw,
                          effective_sheet_number=None, state='adjacent_explicit_note')
            result['diagnostics'].append('ATLAS_PAGE_IS_NOT_DRAWING_SHEET')
        elif raw in ('-', '—', '本图') or raw.casefold() == value.casefold():
            result.update(effective_sheet_number=value, state='adjacent_explicit_note')
            if raw in ('-', '—', '本图'):
                result['diagnostics'].append('PLACEHOLDER_SHEET_RESOLVED_FROM_NOTE')
        else:
            result.update(state='conflict', effective_sheet_number=None)
            result['diagnostics'].append('CIRCLE_AND_NOTE_SHEET_CONFLICT')
    return result
