"""Editable pure kernel used by the native assembly reader.

Input contains only host-selected texts and region parts. No I/O or evidence
creation belongs here. Return [region_id, start, end, column] spans.
"""
from statistics import median


def run(bundle):
    texts = {t['entity_id']: t for t in bundle['texts']}
    edges = bundle['column_edges']
    result = []
    for region in bundle['regions']:
        spans = []
        for part in sorted(region['parts'], key=lambda p:p['text_span']):
            col = max(i for i, edge in enumerate(edges[:-1]) if edge <= texts[part['entity_id']]['origin'][0])
            start, end = part['text_span']
            if spans and col == spans[-1][2] and (start <= spans[-1][1] or not region['text'][spans[-1][1]:start].strip()):
                spans[-1][1] = max(end, spans[-1][1])
            else:
                spans.append([start, end, col])
        result.extend([[region['region_id'], a, b, col] for a, b, col in spans])
    height = median(t['height'] for t in texts.values()) if texts else 1
    regions = {r['region_id']: r for r in bundle['regions']}
    def order(span):
        rid, start, end, col = span
        source = [texts[p['entity_id']] for p in regions[rid]['parts']
                  if p['text_span'][0] < end and p['text_span'][1] > start]
        return (col, -round(max(t['origin'][1] for t in source)/max(height*.3,.01)),
                min(t['origin'][0] for t in source), rid, start)
    return sorted(result, key=order)
