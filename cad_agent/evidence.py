"""Evidence is an exact span in an immutable region, never model coordinates."""

from hashlib import sha256
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
    crosses_boundary=any(a<start<b or a<end<b for a,b in (t['text_span'] for t in source_tokens))
    if crosses_boundary and len(parts)==1 and list(parts[0].text_span)==[start,end]:
        source_tokens = [dict(t,text_span=[t['text_span'][0]+start,t['text_span'][1]+start])
                         for t in readings(source.text[start:end],snapshot_id+':'+source.region_id+':'+parts[0].entity_id)]
    for token in source_tokens:
        a,b = token["text_span"]
        if start <= a < b <= end:
            atoms.append({**token, "source_span":[a,b], "text_span":[a-start,b-start]})
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
