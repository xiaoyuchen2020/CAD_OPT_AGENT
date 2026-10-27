"""Host-owned kernel inputs, validation, and context-local version dispatch."""
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
import hashlib
from . import assembly_kernel
from ..table_recovery.bundle import digest

ACTIVE = ContextVar('cad_assembly_kernel', default=None)
VALIDATOR = 'assembly-kernel@1'


def source():
    return Path(assembly_kernel.__file__).read_text()


def host_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def base_hash():
    return hashlib.sha256(source().encode()).hexdigest()


def version():
    active = ACTIVE.get()
    return active['version'] if active else base_hash()


@contextmanager
def using(version_id, callback):
    token = ACTIVE.set(dict(version=version_id, callback=callback))
    try:
        yield
    finally:
        ACTIVE.reset(token)


def prepare(index, target, band):
    a,b,c,d = band['bbox']
    selected = {t['entity_id']: t for t in index.scene['texts'] if t['layout_id']==target.layout_id
                and a<=t['origin'][0]<c and b<=t['origin'][1]<d and abs(t.get('rotation',0))<.1}
    regions = ({r.region_id:r for eid in selected for r,_ in index.entity_sources.get(eid,[])}.values()
               if hasattr(index,'entity_sources') else index.scope(target.layout_id,target.frame_id))
    rows=[]
    for r in regions:
        if r.layout_id!=target.layout_id or (target.frame_id and r.frame_id!=target.frame_id):continue
        parts=[dict(entity_id=p.entity_id,text_span=list(p.text_span)) for p in r.parts if p.entity_id in selected]
        if parts:
            # Preserve offsets and whitespace boundaries without exporting
            # neighboring text outside the selected native parts.
            chars=[c if c.isspace() else '\ufffc' for c in r.text]
            for p in parts:
                lo,hi=p['text_span'];chars[lo:hi]=r.text[lo:hi]
            rows.append(dict(region_id=r.region_id,text=''.join(chars),parts=parts))
    body=dict(texts=[{k:t[k] for k in ('entity_id','origin','height')} for t in selected.values()],
              regions=rows,column_edges=band['column_edges'],bbox=band['bbox'])
    return dict(body,input_id=digest(body))


def validate(bundle, spans, *, strict_order=False):
    """Independently prove membership, exact part coverage, columns and order.

    Generated code can split/merge adjacent source parts, but cannot omit a
    part, duplicate a part, splice non-whitespace, or change column membership.
    """
    if not isinstance(spans,list) or len(spans)>5000:raise ValueError('KERNEL_OUTPUT_SCHEMA')
    regions={r['region_id']:r for r in bundle['regions']}
    texts={t['entity_id']:t for t in bundle['texts']}
    expected={(r['region_id'],i) for r in bundle['regions'] for i in range(len(r['parts']))}
    seen=set();order=[]
    for span in spans:
        if not isinstance(span,list) or len(span)!=4:raise ValueError('KERNEL_OUTPUT_SCHEMA')
        rid,lo,hi,col=span
        if rid not in regions or any(type(v)!=int for v in (lo,hi,col)):raise ValueError('KERNEL_OUTPUT_SCHEMA')
        r=regions[rid]
        if not 0<=lo<hi<=len(r['text']):raise ValueError('KERNEL_SPAN_BOUNDS')
        parts=[(i,p) for i,p in enumerate(r['parts']) if p['text_span'][0]<hi and p['text_span'][1]>lo]
        if not parts or min(p['text_span'][0] for _,p in parts)!=lo or max(p['text_span'][1] for _,p in parts)!=hi:
            raise ValueError('KERNEL_PART_TRUNCATED')
        cursor=lo;points=[]
        for i,p in sorted(parts,key=lambda v:v[1]['text_span']):
            a,b=p['text_span']; t=texts[p['entity_id']]
            native_col=sum(t['origin'][0]>=edge for edge in bundle['column_edges'][1:-1])
            if native_col!=col or a<lo or b>hi:raise ValueError('KERNEL_COLUMN_OR_SPAN_CHANGED')
            if r['text'][cursor:a].strip():raise ValueError('KERNEL_FOREIGN_TEXT')
            cursor=max(cursor,b)
            key=(rid,i)
            if key in seen:raise ValueError('KERNEL_DUPLICATE_PART')
            seen.add(key);points.append(t['origin'])
        order.append((col,-max(p[1] for p in points),min(p[0] for p in points),rid,lo))
    if seen!=expected:raise ValueError('KERNEL_MISSING_PART')
    if strict_order and order!=sorted(order):raise ValueError('KERNEL_ORDER_MISMATCH')
    return dict(validator=VALIDATOR,source_parts=len(seen),strict_order=strict_order,semantic_acceptance=False)


def read(index, target, band):
    bundle=prepare(index,target,band)
    active=ACTIVE.get()
    spans=active['callback'](bundle) if active else assembly_kernel.run(bundle)
    # The unchanged native kernel retains legacy behavior; revisions are
    # checked before a callback can return them to the native binding layer.
    return [(index.by_id[rid],a,b,col,'ruled_row_candidate') for rid,a,b,col in spans]
