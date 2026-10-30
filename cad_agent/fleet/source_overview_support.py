"""Source-overview-only readers for the archived evidence wire format.

These pure projections deliberately do not import the optional results panel or
rule workflow engine. They neither execute rules nor create their databases.
"""
import math
import os
import re
from pydantic import BaseModel, Field, StrictInt
from cad_agent.exploration.contracts import copied, digest

MEASUREMENT_TOOLS = {'measure_local_geometry', 'measure_geometry_primitives'}


def identity(e):
    return {k:e.get(k) for k in ('evidence_id','snapshot_id','region_id','quote','text_span','entity_ids')}

def package_leaves(package, load=None, seen=None):
    """Resolve legacy merged packages without rewriting any immutable object."""
    seen=set() if seen is None else seen
    if package['package_id'] in seen:return []
    seen.add(package['package_id'])
    ids=package.get('constituent_packages',[])
    if ids and load:
        return [leaf for ident in ids for leaf in package_leaves(load(ident),load,seen)]
    return [package]

def mapping(value):
    return value if isinstance(value, dict) else {}

def archived_location(source, archive):
    """Legacy fact atoms omit geometry; hydrate only an exact archived citation."""
    result = mapping(archive.get('result')) or mapping(mapping(archive.get('artifacts')).get('run')).get('result') or {}
    matches = [c for c in mapping(result).get('citations', [])
               if isinstance(c, dict) and identity(c) == identity(source)]
    keys = ('layout_id', 'frame_id', 'bbox', 'component_boxes', 'highlight_regions',
            'source_parts', 'coordinate_space', 'precision', 'unit')
    if not matches:
        return dict(source)
    locations = [{k:c[k] for k in keys if c.get(k) is not None} for c in matches]
    if any(item != locations[0] for item in locations[1:]):
        return dict(source)
    return dict(source, **{k:v for k,v in locations[0].items() if source.get(k) is None})

def anchor(source, archive):
    """Never infer a drawing/layout or route an old attempt to the current node."""
    drawings = mapping(archive.get('artifacts')).get('drawings', [])
    drawing = next((d for d in drawings if d['id'] == source.get('snapshot_id')), None)
    bbox = source.get('bbox')
    reason = None
    if not drawing or not drawing.get('sha256'):
        reason = '缺少归档图纸版本'
    elif source.get('source_sha256') and source['source_sha256'] != drawing['sha256']:
        reason = '来源与归档图纸版本不一致'
    elif not archive.get('node_id'):
        reason = '缺少原执行节点'
    elif not source.get('layout_id') or not (isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in bbox)
            and bbox[0] <= bbox[2] and bbox[1] <= bbox[3]):
        reason = '缺少有效布局或坐标，保留原文待定位'
    # Node-local URLs in a centre archive must not escape the selected node route.
    source = {k:v for k,v in source.items() if k not in ('preview_url', 'evidence_url')}
    return dict(source, source_sha256=drawing.get('sha256') if drawing else None,
                unit=source.get('unit') or (drawing or {}).get('unit', 'unknown'),
                drawing_name=drawing.get('name') if drawing else source.get('snapshot_id'),
                node_id=archive.get('node_id'), job_id=archive['job_id'], attempt_id=archive['attempt_id'],
                locatable=reason is None, location_note=reason)

def published(result):
    citations = [c for c in result.get('citations') or [] if isinstance(c, dict)]
    manifest = result.get('delivery_manifest')
    if manifest is not None:
        selected = set(manifest.get('selected_evidence_ids') or [])
        citations = [c for c in citations if c.get('evidence_id') in selected]
    return citations

class ReadEvidence(BaseModel):
    # Archives use compact Evidence records; layout/bbox are optional there.
    evidence_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    region_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    text_span: list[StrictInt] = Field(min_length=2,max_length=2)
    entity_ids: list[str] = Field(min_length=1)

def source_read_atoms(payload,record):
    if not isinstance(payload,dict) or not record.get('ref',{}).get('complete_record'):return []
    # Explicit tool exception/arguments, prompts, summaries and search results
    # are not a source read, even if they embed Evidence-shaped dictionaries.
    if any(k in payload for k in ('tool','arguments','exception_message','messages','claims')):return []
    if not payload.get('target_region_id') or not isinstance(payload.get('items'),list):return []
    receipt=payload.get('source_coverage_receipt',{})
    ids=receipt.get('read_evidence_ids',[])
    if not isinstance(ids,list) or not ids:return []
    sid=payload.get('snapshot_id')
    if sid not in record.get('sources',{}):raise ValueError('FACT_RECOVERED_READ_SOURCE_MISMATCH')
    found=[]
    for item in payload['items']:
        if not isinstance(item,dict) or not isinstance(item.get('evidence'),dict):continue
        raw=item['evidence']
        if raw.get('evidence_id') not in ids:continue
        # Metadata outside Evidence is not promoted. Exact text and span are
        # confirmed twice by the host read item and its registered evidence.
        ReadEvidence.model_validate(raw)
        evidence=raw
        span=evidence['text_span'];quote=evidence['quote']
        if (evidence['snapshot_id']!=sid or item.get('region_id')!=evidence['region_id']
                or item.get('text')!=quote or item.get('text_span')!=span
                or len(span)!=2 or span[0]<0 or span[1]-span[0]!=len(quote)
                or not quote or not evidence['entity_ids']):
            raise ValueError('FACT_RECOVERED_READ_BINDING_INVALID')
        found.append(evidence)
    return found

def export_measurements(records,sources):
    measurements={};images=[];bundles={}
    for record,payload in records:
        if not isinstance(payload,dict):continue
        bid=payload.get('bundle_id');sid=payload.get('snapshot_id')
        if bid and isinstance(payload.get('scope'),dict) and payload['scope'].get('layout_id') and sid in sources and payload.get('source_sha256')==sources[sid]['source_sha256'] and sid in record['sources']:
            bound=dict(snapshot_id=sid,source_sha256=payload['source_sha256'],scope=copied(payload['scope']),record_ref=copied(record['ref']))
            if bid in bundles and any(bundles[bid][k]!=bound[k] for k in ('snapshot_id','source_sha256','scope')):raise ValueError('GEOMETRY_BUNDLE_CONFLICT')
            bundles[bid]=bound
        mid=payload.get('measurement_id')
        if not mid:continue
        if payload.get('image_id'):
            images.append(payload);continue
        if not payload.get('measurements'):continue
        # Only top-level, host archived method results. Not model claims,
        # nested arbitrary JSON, source coordinates or unsuccessful requests.
        receipts=payload.get('operation_receipts',[])
        if not any(r.get('tool') in MEASUREMENT_TOOLS and r.get('reference')==mid
                   and r.get('local_result_available') is True for r in receipts):continue
        if payload.get('status')!='local_pair_measured':continue
        if (sid not in sources or payload.get('source_sha256')!=sources[sid]['source_sha256']
                or sid not in record['sources'] or not record['ref'].get('complete_record')):
            raise ValueError('GEOMETRY_SOURCE_NOT_BOUND')
        if not payload.get('source_entity_ids') or not payload.get('bundle_id') or not payload.get('observation_id'):
            raise ValueError('GEOMETRY_IDENTITY_INCOMPLETE')
        width=payload['measurements'].get('width_drawing_units');line=payload.get('measurement_line')
        if (isinstance(width,bool) or not isinstance(width,(int,float)) or not math.isfinite(width) or width<=0
            or not isinstance(line,list) or len(line)!=2 or any(not isinstance(p,list) or len(p)!=2 for p in line)
            or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for p in line for v in p)):
            raise ValueError('GEOMETRY_MEASUREMENT_INVALID')
        error=payload.get('error_bound',0)
        if isinstance(error,bool) or not isinstance(error,(int,float)) or not math.isfinite(error) or error<0:
            raise ValueError('GEOMETRY_ERROR_BOUND_INVALID')
        if abs(math.dist(*line)-width)>max(1e-6,abs(width)*1e-9):
            raise ValueError('GEOMETRY_LINE_VALUE_MISMATCH')
        value=dict(measurement_id=mid,snapshot_id=sid,source_sha256=payload['source_sha256'],
            observation_id=payload['observation_id'],record_ref=copied(record['ref']),
            verification='archive_method_record_hash_and_source_bound',engineering_applicability='unconfirmed',
            raw=copied(payload),images=[])
        if mid in measurements and measurements[mid]['raw']!=value['raw']:raise ValueError('GEOMETRY_ID_CONFLICT')
        measurements[mid]=value
    for m in measurements.values():
        b=bundles.get(m['raw']['bundle_id'])
        if b and (b['snapshot_id'],b['source_sha256'])==(m['snapshot_id'],m['source_sha256']):
            m['layout_id']=b['scope']['layout_id'];m['bundle_scope']=b
            line=m['raw']['measurement_line'];m['bbox']=[min(p[0] for p in line),min(p[1] for p in line),max(p[0] for p in line),max(p[1] for p in line)]
    for image in images:
        m=measurements.get(image['measurement_id'])
        if m and (image.get('snapshot_id'),image.get('source_sha256'))==(m['snapshot_id'],m['source_sha256']):
            m['images'].append({k:image[k] for k in ('image_id','image_sha256','bbox','image_url') if k in image})
    return measurements

def node_ids(env=None):
    env = os.environ if env is None else env
    values = tuple(part.strip().lower() for part in env.get('CAD_FLEET_NODE_IDS', 'a,b').split(','))
    if (not 1 <= len(values) <= 8 or len(set(values)) != len(values)
            or any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', value) for value in values)):
        raise ValueError('Invalid CAD_FLEET_NODE_IDS')
    return values
