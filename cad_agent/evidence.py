"""Evidence is an exact span in an immutable region, never model coordinates."""

from hashlib import sha256
import re
from cad_agent.contracts import Evidence, SourceRegion, stable_id
from cad_agent.cad.reader import union_box
from cad_agent.cad.numbers import VERSION as NUMBER_VERSION, region_readings, readings


def bind_evidence(snapshot_id, source: SourceRegion, start: int, end: int, unit):
    if not 0 <= start < end <= len(source.text):
        raise ValueError("Invalid source span")
    parts = [p for p in source.parts if p.text_span[0] < end and p.text_span[1] > start]
    ids = list(dict.fromkeys(p.entity_id for p in parts)) or source.entity_ids
    boxes = [p.bbox for p in parts]
    shapes = []
    # A whole-region outline must not leak into a narrower quote's highlight.
    if start == 0 and end == len(source.text) and source.highlight_regions:
        shapes = source.highlight_regions
    elif any(p.highlight_regions for p in parts):
        for part in parts:
            x, y, xx, yy = part.bbox
            shapes.extend(part.highlight_regions or [dict(boundary=[[x,y],[xx,y],[xx,yy],[x,yy]])])
    atoms = []
    source_tokens = region_readings(source, snapshot_id)
    # A quote covering one complete native entity has its own lexical boundary.
    # Adjacent CAD cells can have been concatenated by the legacy region index;
    # never tokenize a user-selected substring as though it were a whole entity.
    boundary_tokens=[t for t in source_tokens if t['text_span'][0]<start<t['text_span'][1]
                     or t['text_span'][0]<end<t['text_span'][1]]
    # Separate entities may split an established list delimiter from its digits.
    # Preserve that source role; only ambiguous concatenated cell numbers need
    # complete-entity retokenization (e.g. ordinal 1 adjacent to area 933).
    contextual=any(t['kind']=='ordinal' or t.get('interpretation')=='thickness_list_marker' for t in boundary_tokens)
    if boundary_tokens and not contextual and len(parts)==1 and list(parts[0].text_span)==[start,end]:
        source_tokens = [dict(t,text_span=[t['text_span'][0]+start,t['text_span'][1]+start])
                         for t in readings(source.text[start:end],snapshot_id+':'+source.region_id+':'+parts[0].entity_id)]
    for token in source_tokens:
        a,b = token["text_span"]
        if start <= a < b <= end:
            atoms.append({**token, "source_span":[a,b], "text_span":[a-start,b-start]})
        elif (token.get('interpretation') == 'thickness_list_marker'
              and start == a + 1 and b <= end):
            # A CAD cell may contain the complete quantity but not its separate
            # leading list hyphen. Preserve the parent reading and identity;
            # never reparse an arbitrary substring or strip a genuine minus.
            atoms.append({**token, "source_span":[a,b], "source_raw":token['raw'],
                          "raw":source.text[start:b], "text_span":[0,b-start]})
        elif token['kind'] == 'ordinal':
            # The digits and their list delimiter may be separate CAD text
            # entities. Keep the established ordinal only if ALL digits survive.
            digits = re.search(r'\d+', token['raw'])
            if digits and start <= a + digits.start() < a + digits.end() <= end:
                lo, hi = max(a, start), min(b, end)
                atoms.append({**token, "source_span":[a,b], "source_raw":token['raw'],
                              "raw":source.text[lo:hi], "text_span":[lo-start,hi-start]})
    return Evidence(
        evidence_id=stable_id(
            "ev", snapshot_id, source.region_id, str(start), str(end)
        ),
        snapshot_id=snapshot_id,
        region_id=source.region_id,
        layout_id=source.layout_id,
        frame_id=source.frame_id,
        quote=source.text[start:end],
        numeric_readings={"version":NUMBER_VERSION,"quote_sha256":sha256(source.text[start:end].encode()).hexdigest(),"items":atoms},
        text_span=[start, end],
        entity_ids=ids,
        bbox=union_box(boxes) if boxes else source.bbox,
        component_boxes=boxes,
        highlight_regions=shapes,
        source_parts=parts,
        unit=unit,
        precision="entity" if len(ids) == 1 else "region",
    )


def validate_proposal(proposal, regions):
    if not isinstance(proposal, dict):
        raise ValueError("Invalid proposal")
    r = regions.get(proposal.get("region_id"))
    quote = proposal.get("quote")
    if r is None or not isinstance(quote, str) or not quote.strip():
        raise ValueError("Unknown source")
    if r.text.count(quote) != 1:
        raise ValueError("Quote absent or ambiguous")
    start = r.text.index(quote)
    return r, start, start + len(quote)


def verify_source_evidence(evidence, *, snapshot_id, region, drawing_unit):
    """Verify a snapshot-local anchor; offsets are Unicode code points [start,end).

    Never normalize source text, relocate an anchor, infer a quantity's unit, or
    trust supplied geometry. Old numeric catalogs are rebuilt from the source.
    The drawing unit describes CAD coordinates, not every number in the quote.
    """
    value = evidence.model_dump() if isinstance(evidence, Evidence) else dict(evidence)
    span = value.get('text_span')
    if (region is None or value.get('snapshot_id') != snapshot_id
            or value.get('region_id') != region.region_id
            or not isinstance(span, (list, tuple)) or len(span) != 2
            or not all(type(n) is int for n in span)
            or not 0 <= span[0] < span[1] <= len(region.text)
            or region.text[span[0]:span[1]] != value.get('quote')):
        raise ValueError('SOURCE_RECORD_SPAN_MISMATCH')
    expected = bind_evidence(snapshot_id, region, *span, drawing_unit).model_dump()
    if value.get('evidence_id') != expected['evidence_id']:
        raise ValueError('SOURCE_RECORD_SPAN_MISMATCH: evidence identity')
    for key in ('layout_id', 'frame_id', 'entity_ids', 'bbox', 'component_boxes',
                'highlight_regions', 'source_parts', 'coordinate_space', 'unit'):
        if key in value and value[key] != expected.get(key, []):
            raise ValueError('SOURCE_RECORD_LOCATION_MISMATCH: ' + key)
    return expected
