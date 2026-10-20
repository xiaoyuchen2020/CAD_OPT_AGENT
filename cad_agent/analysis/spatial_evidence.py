"""Persistent, exact source relationships for pluggable spatial methods.

Source binding is a useful verified fact. It does not certify the building role,
net boundary, census coverage or engineer acceptance. Those axes stay separate.
"""
from copy import deepcopy
from hashlib import file_digest
import json
from pydantic import Field
from shapely.geometry import Point, Polygon, LineString
from cad_agent.contracts import Record, stable_id

VERSION = 'spatial-object-v1'


class BindSpatialObject(Record):
    scope_id: str
    entity_ids: list[str] = Field(min_length=1, max_length=64)
    label_region_ids: list[str] = Field(default_factory=list, max_length=16)
    role: str = Field(default='source_geometry', min_length=1, max_length=80)
    exclusion_entity_ids: list[str] = Field(default_factory=list, max_length=64)
    floor: str = Field(default='', max_length=120)


class SpatialObjectRead(Record):
    object_id: str


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _scope(session, scope_id):
    from cad_agent.analysis.scope_workflow import selected_for_call
    selected = selected_for_call(session, 'inspect_scope_objects')
    if not selected or selected['scope_id'] != scope_id:
        raise ValueError('SPATIAL_SCOPE: object must belong to the active task scope.')
    path = session.w.store.artifact(selected['snapshot_id'], 'source.dxf')
    with path.open('rb') as source:
        if file_digest(source, 'sha256').hexdigest() != selected['source_sha256']:
            raise ValueError('SPATIAL_STALE: actual DXF hash differs from bound source.')
    return selected


def _records(session, scope, entity_ids):
    from cad_agent.geometry.service import GeometryService
    from cad_agent.analysis.scope_workflow import contains
    ids = sorted(set(entity_ids))
    records = GeometryService(session.w, scope['snapshot_id']).records(ids)
    forbidden = set(scope.get('title_entity_ids', [])) | set(scope.get('border_entity_ids', []))
    if {r['entity_id'] for r in records} != set(ids):
        raise ValueError('SPATIAL_SOURCE: missing exact source entities.')
    for rec in records:
        if (rec['entity_id'] in forbidden or rec['layout_id'] != scope['layout_id'] or
                not contains(scope['bbox'], rec.get('bbox')) or rec.get('status') != 'ready' or
                not rec.get('paths') or rec.get('kind') in ('DIMENSION', 'HATCH')):
            raise ValueError('SPATIAL_SOURCE: non-geometric, incomplete, foreign-layout or out-of-frame entity.')
    return records


def _polygons(records):
    result = []
    for r in records:
        if not r.get('linear'):
            continue
        for path in r['paths']:
            if len(path) >= 4 and path[0] == path[-1]:
                p = Polygon(path)
                if p.is_valid and p.area > 0:
                    result.append((r['entity_id'], p))
    return result


def _labels(session, scope, region_ids, records):
    texts = {t['entity_id']: t for t in session.index.scene['texts']}
    polygons = _polygons(records)
    labels = []
    for rid in sorted(set(region_ids)):
        region = session.index.by_id.get(rid)
        if not region or region.layout_id != scope['layout_id'] or region.frame_id != scope['frame_id']:
            raise ValueError('SPATIAL_LABEL: label belongs to a different layout/frame or is absent.')
        native = [texts[e] for e in region.entity_ids if e in texts]
        if not native:
            raise ValueError('SPATIAL_LABEL: no native text source.')
        points = [t['origin'][:2] for t in native]
        enclosing = [eid for eid, p in polygons if all(p.contains(Point(pt)) for pt in points)]
        labels.append(dict(region_id=rid, text=region.text, entity_ids=[t['entity_id'] for t in native],
            points=points, relation='strict_native_label_containment' if len(enclosing) == 1 else 'unverified',
            enclosing_entity_ids=enclosing,
            semantic_identity='unconfirmed'))
    return labels


def _references(session, scope, records, labels):
    """Reuse persisted native legend enclosures; also detect a new enclosure.

    An explicit legend title in a closed source polygon is a source fact. It
    excludes enclosed geometry from placed-object methods, independent of label
    distance or a guessed legend radius.
    """
    from cad_agent.analysis.scope_objects import legend_context
    anchors = [dict(region_id=l['region_id'], point=p) for l in labels for p in l['points']]
    titles = [dict(region_id=r.region_id, text=r.text,
        point=[(r.bbox[0]+r.bbox[2])/2, (r.bbox[1]+r.bbox[3])/2])
        for r in session.index.scope(scope['layout_id'], scope['frame_id'])
        if '图例' in r.text or 'legend' in r.text.lower()]
    # Include the geometry itself as a target, so callers cannot bypass legend
    # rejection by leaving label_region_ids empty.
    for r in records:
        for path in r['paths']:
            anchors.extend(dict(region_id=r['entity_id'], point=p) for p in path[:1])
    refs = deepcopy(scope.get('object_workflow', {}).get('reference_contexts', []))
    refs.extend(legend_context(records, anchors, titles, set()))
    from cad_agent.analysis.scope_workflow import contains
    hits = [r for r in refs if r.get('boundary') and any(
        all(Polygon(r['boundary']).covers(LineString(path)) for path in rec['paths'] if len(path) >= 2)
        for rec in records)]
    return list({_key(r): r for r in hits}.values())


def bind_spatial_object(session, args):
    scope = _scope(session, args.scope_id)
    records = _records(session, scope, args.entity_ids)
    exclusions = _records(session, scope, args.exclusion_entity_ids) if args.exclusion_entity_ids else []
    if set(args.entity_ids) & set(args.exclusion_entity_ids):
        raise ValueError('SPATIAL_EXCLUSIONS: a source entity cannot be both included and excluded.')
    labels = _labels(session, scope, args.label_region_ids, records)
    refs = _references(session, scope, records, labels)
    geometry_key = stable_id('source_shape', _key([{k:r[k] for k in ('entity_id','entity_path','paths')} for r in records]))
    identity_key = dict(source_sha256=scope['source_sha256'], snapshot_id=scope['snapshot_id'],
        layout_id=scope['layout_id'], frame_id=scope['frame_id'], scope_id=scope['scope_id'],
        geometry_key=geometry_key, labels=sorted(set(args.label_region_ids)), role=args.role,
        exclusions=sorted(set(args.exclusion_entity_ids)), floor=args.floor,
        unit_policy=deepcopy(session.task.data.get('unit_policy')))
    oid = stable_id('spatial_object', VERSION, _key(identity_key))
    bounds = [min(r['bbox'][0] for r in records), min(r['bbox'][1] for r in records),
              max(r['bbox'][2] for r in records), max(r['bbox'][3] for r in records)]
    status = 'label_containment_verified' if labels and all(l['relation']=='strict_native_label_containment' for l in labels) else 'source_bound'
    unit = session.drawing.get('unit', 'unknown')
    obj = dict(version=VERSION, object_id=oid, **{k:identity_key[k] for k in
        ('source_sha256','snapshot_id','layout_id','frame_id','scope_id')},
        floor=dict(value=args.floor or scope.get('name',''), status='asserted' if args.floor else 'scope_title_only',
                   title_evidence=deepcopy(scope.get('title_evidence',[]))),
        label=labels, geometry=dict(entity_ids=sorted(set(args.entity_ids)), geometry_key=geometry_key,
            records=deepcopy(records), bbox=bounds, complete_selected_entities=True),
        unit=dict(value=unit, status='dxf_header' if unit != 'unknown' else 'unknown', policy=deepcopy(session.task.data.get('unit_policy'))),
        role=dict(value=args.role, status='asserted', reference_only=bool(refs)),
        identity=dict(status=status, engineering_confirmed=False,
            verified_facts=['source_hash','layout','frame_containment','complete_selected_source_paths'] +
            (['strict_native_label_containment'] if status=='label_containment_verified' else [])),
        coverage=dict(selected_entities_complete=True, object_extent='unconfirmed', census='not_evaluated',
            net_boundary='unconfirmed', main_outline='unconfirmed'),
        exclusions=dict(entity_ids=sorted(set(args.exclusion_entity_ids)), records=deepcopy(exclusions),
            status='explicit_source_selection' if exclusions else 'none_selected_not_proof_none_exist'),
        reference_context=refs, measurement_evidence=[],
        next_calls=[dict(tool='render_region', arguments=dict(layout_id=scope['layout_id'], bbox=bounds,
                highlight_entity_ids=sorted(set(args.entity_ids))[:12]), purpose='Review semantic role and full extent of these exact paths.')]
            if bounds[0] < bounds[2] and bounds[1] < bounds[3] else
            [dict(tool='read_geometry', arguments=dict(entity_ids=sorted(set(args.entity_ids))[:12]), purpose='Review exact open source paths; request a local crop for semantic role.')])
    registry = session.task.data.setdefault('spatial_objects', {})
    old = registry.get(oid)
    if old:
        obj['measurement_evidence'] = deepcopy(old.get('measurement_evidence', []))
    registry[oid] = obj
    return public_object(obj)


def public_object(obj):
    result = deepcopy(obj)
    # Native paths persist once, while model packets retain exact source IDs.
    result['geometry'].pop('records', None)
    result['exclusions'].pop('records', None)
    result.update(kind='spatial_object', status=obj['identity']['status'],
        operation_receipts=[dict(operation='geometry', fulfilled=False, basis='source_identity_only')])
    return result


def resolve_object_input(session, object_id, required_roles=()):
    obj = session.task.data.get('spatial_objects', {}).get(object_id)
    if not obj:
        raise ValueError('SPATIAL_OBJECT: unknown task-owned object; bind exact source entities first.')
    scope = _scope(session, obj['scope_id'])
    if any(obj[k] != scope[k] for k in ('snapshot_id','source_sha256','layout_id','frame_id')):
        raise ValueError('SPATIAL_STALE: object context no longer matches active source scope.')
    if obj['unit']['policy'] != session.task.data.get('unit_policy'):
        raise ValueError('SPATIAL_STALE: unit policy changed; rebind the object before measurement.')
    records = _records(session, scope, obj['geometry']['entity_ids'])
    current = stable_id('source_shape', _key([{k:r[k] for k in ('entity_id','entity_path','paths')} for r in records]))
    if current != obj['geometry']['geometry_key']:
        raise ValueError('SPATIAL_STALE: native paths changed.')
    exclusion_ids = obj['exclusions']['entity_ids']
    exclusions = _records(session, scope, exclusion_ids) if exclusion_ids else []
    source_shape = lambda rows: _key([{k: r[k] for k in ('entity_id', 'entity_path', 'paths')} for r in rows])
    if source_shape(exclusions) != source_shape(obj['exclusions']['records']):
        raise ValueError('SPATIAL_STALE: exclusion source paths changed.')
    if required_roles and obj['role']['value'] not in required_roles:
        raise ValueError('SPATIAL_ROLE: declared role is incompatible with this method.')
    refs = _references(session, scope, records, obj['label'])
    if refs and not obj['role']['reference_only']:
        obj['role']['reference_only'] = True
        obj['reference_context'] = refs
    # Return a copy: method code cannot silently rewrite identity evidence.
    return deepcopy(obj)


def get_spatial_object(session, args):
    return public_object(resolve_object_input(session, args.object_id))


def record_measurement(session, object_id, measurement):
    """Methods attach their own limited conclusion; identity is never upgraded."""
    obj = resolve_object_input(session, object_id)
    if measurement.get('source_sha256') != obj['source_sha256']:
        raise ValueError('SPATIAL_MEASUREMENT: source hash missing or inconsistent.')
    evidence = session.task.data['spatial_objects'][object_id]['measurement_evidence']
    if measurement not in evidence:
        evidence.append(deepcopy(measurement))
