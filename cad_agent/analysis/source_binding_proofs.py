"""Alternative proofs for already-read sources; no query or identity expansion."""
import re

from cad_agent.cad.assembly_headings import HEADING, source_titles
from cad_agent.cad.source_vocabulary import TERMS, record_type
from cad_agent.cad.numbers import ordinals


MISSING_HEADERS = {'HEADER_UNRESOLVED', 'ASSEMBLY_IDENTITY_FIELD_UNRESOLVED',
                   'ASSEMBLY_LAYER_FIELD_UNRESOLVED'}


def unread_dependency(text):
    """Alternative proofs establish local ownership, not referenced contents."""
    return bool(re.search(r'详|参见|参照|采用\s*[\u4e00-\u9fff]{1,4}\s*[0-9０-９]', text))


def _identity(text):
    value = text.strip()
    if not value or '\n' in value or len(value) > 160:
        return False
    return record_type(value) == 'layer_assembly' and (value in TERMS or bool(HEADING.match(value)))


def closed_title_and_layers(index, target, sources, band, binding, geometry_diagnostics, heading_start=None):
    """A unique title cell and a complete enumerated cell in one closed row.

    Do not invent header roles or identity columns. Query matching continues to
    use its existing title/identity fields. A second title, multiple enumerated
    columns, clipped source, ambiguous envelope or numbering gap blocks recovery.
    Other cells remain untyped original text, never inferred applicability.
    """
    if (binding.get('state') != 'candidate' or set(binding.get('diagnostics', [])) != MISSING_HEADERS
            or geometry_diagnostics or band['maximum_endpoint_gap'] > .11):
        return None
    columns = {}
    for region, start, end, col, _ in sources:
        if region.text[start:end].strip():
            columns.setdefault(col, []).append((region, start, end))
        parts = [p for p in region.parts if p.text_span[0] < end and p.text_span[1] > start]
        if not parts or any(p.bbox[1] < band['bbox'][1] - band['endpoint_tolerance'] or
                            p.bbox[3] > band['bbox'][3] + band['endpoint_tolerance'] for p in parts):
            return None
    if len(columns) < 2 or None in columns:
        return None
    texts = {c: '\n'.join(r.text[a:b] for r, a, b in spans).strip() for c, spans in columns.items()}
    if any(re.search(r'(?m)^\s*(?:[0-9０-９]+[A-Za-zＡ-Ｚａ-ｚ]|[A-Za-zＡ-Ｚａ-ｚ])[、.．)）]', text) for text in texts.values()):
        return None
    identity_cols = [c for c, text in texts.items() if _identity(text)]
    if len(identity_cols) != 1:
        return None
    identity_col = identity_cols[0]
    headings = source_titles(target.text)
    start = heading_start if heading_start is not None else headings[0].start() if len(headings) == 1 else 0
    end = target.text.find('\n', start) if headings else len(target.text)
    title = target.text[start:len(target.text) if end < 0 else end].strip()
    if title != texts[identity_col]:
        return None
    enumerated = {c: ordinals(text) for c, text in texts.items() if c != identity_col}
    layer_cols = [c for c, markers in enumerated.items() if markers]
    if len(layer_cols) != 1:
        return None
    layer_col = layer_cols[0]
    other_cols = [c for c in columns if c not in (identity_col, layer_col)]
    if any(HEADING.search(texts[c]) for c in other_cols):
        return None
    body = texts[layer_col]
    markers = ordinals(body)
    numbers = [int(t['value']) for t in markers]
    if (len(numbers) < 2 or numbers != list(range(1, len(numbers) + 1))
            or body[:markers[0]['text_span'][0]].strip()
            or any(source_titles(line) for line in body.splitlines())):
        return None
    # Empty list items are not evidence of a complete construction sequence.
    for i, marker in enumerate(markers):
        stop = markers[i + 1]['text_span'][0] if i + 1 < len(markers) else len(body)
        if not body[marker['text_span'][1]:stop].strip():
            return None
    return dict(basis='explicit_title_and_numbered_closed_row', title_column=identity_col,
                layer_column=layer_col, layer_numbers=numbers, row_bbox=band['bbox'],
                source_line_ids=[line['entity_id'] for line in band['source_lines']],
                title_evidence=[index.evidence(r, a, b) for r, a, b in columns[identity_col]],
                layer_evidence=[index.evidence(r, a, b) for r, a, b in columns[layer_col]],
                requires_reference_read=any(unread_dependency(text) for text in texts.values()),
                untyped_columns=other_cols,
                untyped_evidence=[index.evidence(r, a, b) for c in other_cols for r, a, b in columns[c]])


def unnumbered_practice_paragraph(text):
    """Recognize source prose, never pretend it is an enumerated layer list."""
    body = text.partition('\n')[2].strip()
    if not body or re.search(r'(?m)^\s*[（(]?\s*(?:[0-9０-９]+[A-Za-zＡ-Ｚａ-ｚ]?|[一二三四五六七八九十]+)[、.．)）]|[①-⑳]', body):
        return False
    # Local abbreviated codes and table headings can occur inside a parser's
    # reading column without being recognized by the family heading grammar.
    # Their presence is not proof that a single prose record owns that column.
    if re.search(r'(?m)^\s*(?!(?:详|见|同|参见))[^\W\d_]{1,4}\s*[—–－-]?\s*[0-9０-９]+[A-Za-zＡ-Ｚａ-ｚ]?(?:[:：（(—–－-]|\s*$)',body):
        return False
    if any(re.fullmatch(r'(?:类别|编号|构造做法|适用部位|建筑做法|精装做法)',re.sub(r'\s+','',line)) for line in body.splitlines()):
        return False
    prose = '\n'.join(line for line in body.splitlines()
                      if not re.fullmatch(r'\s*[（(].*[）)]\s*', line))
    return bool(re.search(r'涂|刷|铺设|铺贴|铺装|满铺|干铺|湿铺|粘贴|黏贴|贴砖|抹|罩面|参见|详见|详[^，。；\n]{1,40}(?:图|设计|图集)', prose))
