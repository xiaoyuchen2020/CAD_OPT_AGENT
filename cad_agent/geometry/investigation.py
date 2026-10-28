"""Evidence-bearing investigation tools. Source facts and interpretations stay separate."""
import json
import math
import re
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record, FiniteFloat, stable_id
from cad_agent.geometry.investigation_topology import network, VERSION
from cad_agent.geometry.local_measurement import measure_pair
from cad_agent.geometry.source import finite_box, UNIT_TO_M


def identity(args): return json.dumps(args.model_dump(), sort_keys=True, ensure_ascii=False)


class AnalyzeContext(Record):
    bundle_id: str
    entity_ids: list[str] = Field(default_factory=list, max_length=128)
    label_region_ids: list[str] = Field(default_factory=list, max_length=32)
    object_bbox: list[FiniteFloat] | None = Field(default=None, min_length=4, max_length=4)


class TraceNetwork(Record):
    bundle_id: str
    entity_ids: list[str] = Field(min_length=1, max_length=1000)
    seed_entity_ids: list[str] = Field(default_factory=list, max_length=128)
    census_entity_ids: list[str] = Field(default_factory=list, max_length=1000,
        description='Explicit independent source universe, not just the successfully traced subset. This is not automatically a complete engineering route.')
    tolerance_drawing_units: FiniteFloat = Field(gt=0)


class MeasurePrimitives(Record):
    bundle_id: str
    network_id: str
    primitive_ids: list[str] = Field(min_length=2, max_length=2)


class EvidenceQuote(Record):
    region_id: str
    quote: str = Field(min_length=1, max_length=500)
    role: Literal['unit', 'surface', 'height', 'floor', 'object_identity']


class ResolveBasis(Record):
    bundle_id: str
    evidence: list[EvidenceQuote] = Field(default_factory=list, max_length=32)
    measurement_id: str = ''
    target_unit: Literal['', 'mm', 'cm', 'm', 'in', 'ft'] = ''


class ReadArtifact(Record):
    bundle_id: str
    collection: Literal['geometry_network','geometry_context_analysis','geometry_basis','geometry_investigation_decision',
                        'geometry_route','geometry_validation','local_measurement']
    reference_id: str
    field: str = ''
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=20,ge=1,le=80)


class InvestigationDecision(Record):
    bundle_id: str
    hypothesis_id: str = ''
    entity_ids: list[str] = Field(default_factory=list, max_length=128)
    reference_ids: list[str] = Field(min_length=1, max_length=32)
    decision: Literal['exclude_candidate','retain_candidate','revise_interpretation']
    reason: str = Field(min_length=1,max_length=1000)
    supersedes: str = ''


class PlanInvestigation(Record):
    bundle_id: str
    hypothesis_id: str = ''


def save(service,b,collection,key,args,result):
    rid=stable_id(key,service.sid,b['source_sha256'],VERSION,identity(args))
    result.update({key+'_id':rid,'bundle_id':b['bundle_id'],'snapshot_id':service.sid,
        'source_sha256':b['source_sha256'],'request':args.model_dump(),
        'spatial_fact_scope':dict(identity=dict(engineering_confirmed=False),extent='investigation_only')})
    service.w.store.put(collection,rid,result)
    return result


def selected(b,ids):
    rows={r['entity_id']:r for r in b['entities']}
    if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_SOURCE_ID')
    if any(i not in rows for i in ids):raise ValueError('SOURCE_NOT_IN_BUNDLE')
    return [rows[i] for i in ids]


def analyze(service,args):
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.geometry.workbench_routes import contains, views, InspectViews
    b=service.bundle(args.bundle_id); records=selected(b,args.entity_ids)
    scope=args.object_bbox or b['request']['bbox'];finite_box(scope)
    if not contains(b['request']['bbox'],scope):raise ValueError('OBJECT_SCOPE_OUTSIDE_BUNDLE')
    index=DrawingIndex(service.w,service.sid)
    if args.label_region_ids:
        labels=[]
        for rid in args.label_region_ids:
            r=index.by_id.get(rid)
            if r is None or r.layout_id!=b['layout_id'] or not contains(b['request']['bbox'],r.bbox):
                raise ValueError('LABEL_OUTSIDE_BUNDLE')
            labels.append(dict(region_id=rid,text=r.text,bbox=r.bbox,source_entity_ids=r.entity_ids))
    else:labels=views(service,InspectViews(bundle_id=args.bundle_id))['items']
    scale=max(math.hypot(scope[2]-scope[0],scope[3]-scope[1]),1e-12)
    associations=[]
    for label in labels:
        box=label['bbox'];cx,cy=(box[0]+box[2])/2,(box[1]+box[3])/2
        distance=math.hypot(max(scope[0]-cx,0,cx-scope[2]),max(scope[1]-cy,0,cy-scope[3]))
        associations.append(label|dict(title_within_scope=contains(scope,box),distance_to_scope_normalized=distance/scale,
            floor_mentions=re.findall(r'(?:地下\s*\d+\s*层|[Bb]\d+|\d+\s*[Ff层])',label['text']),
            elevation_mentions=re.findall(r'[±+\-]?\d+\.\d+',label['text']),
            binding_state='candidate',counterevidence=['spatial_proximity_alone_does_not_establish_ownership']))
    associations.sort(key=lambda row:(row['distance_to_scope_normalized'],row['region_id']))
    features=[]
    from shapely.geometry import LineString, Point
    dims=[r for r in b['entities'] if r['kind']=='DIMENSION' and 'defpoint2' in r['native'] and 'defpoint3' in r['native']]
    for r in records:
        n=r['native'];box=r.get('bbox'); paths=r.get('paths',[])
        closed=bool(n.get('closed')) or r['kind'] in ('CIRCLE','HATCH')
        anchors=[]
        if paths:
            shapes=[LineString(p) for p in paths if len(p)>1]
            for d in dims:
                if int(d['native'].get('dimtype',0)) & 7 not in (0,1):continue
                residuals=[min((shape.distance(Point(d['native'][k][:2])) for shape in shapes),default=float('inf')) for k in ('defpoint2','defpoint3')]
                if min(residuals)<=scale*1e-6:
                    anchors.append(dict(dimension_entity_id=d['entity_id'],endpoint_residuals=residuals,
                        interpretation='endpoint_incidence_only_not_target_identity'))
        features.append(dict(entity_id=r['entity_id'],source_path=r['entity_path'],native_kind=r['kind'],layer=r['layer'],
            fully_within_object_scope=bool(box and contains(scope,box)),closed_geometry=closed,
            dimension_anchors=anchors,
            role_candidates=['annotation'] if r['kind']=='DIMENSION' else ['closed_outline_or_symbol'] if closed else ['boundary_or_axis_or_symbol'],
            role_status='unconfirmed',required_checks=['check_measurement_crosses_target_space','exclude_same_wall_faces','check_adjacent_object_and_axis']))
    pair_checks=[]
    if len(records)==2:
        from shapely.geometry import Polygon
        pair=measure_pair(*records,'parallel_normal' if records[0]['kind']=='LINE' else 'concentric_radial')
        if pair.get('measurement_line'):
            cut=LineString(pair['measurement_line']);mid=cut.interpolate(.5,normalized=True)
            for obstacle in b['entities']:
                paths=obstacle.get('paths',[])
                if obstacle['entity_id'] in args.entity_ids or len(paths)!=1 or len(paths[0])<4 or paths[0][0]!=paths[0][-1]:continue
                box=obstacle.get('bbox')
                if box and not (box[0]<=mid.x<=box[2] and box[1]<=mid.y<=box[3]):continue
                polygon=Polygon(paths[0])
                if polygon.is_valid and polygon.covers(mid):pair_checks.append(dict(check='measurement_midpoint_inside_closed_outline',entity_id=obstacle['entity_id'],height_relation='unknown',impact='inspect_target_space_or_obstacle'))
            for d in dims:
                p,q=[Point(d['native'][k][:2]) for k in ('defpoint2','defpoint3')]
                if p.distance(q)==0:continue
                alignment=abs((q.x-p.x)*(pair['measurement_line'][1][0]-pair['measurement_line'][0][0])+(q.y-p.y)*(pair['measurement_line'][1][1]-pair['measurement_line'][0][1]))/(p.distance(q)*max(cut.length,1e-12))
                if p.distance(cut)<=scale*1e-6 or q.distance(cut)<=scale*1e-6:
                    pair_checks.append(dict(check='dimension_direction_comparison',dimension_entity_id=d['entity_id'],parallel_to_measurement_cosine=alignment,impact='verify_cross_target_dimension_not_along_boundary'))
        roots=[r['entity_path'][:-1] for r in records]
        if roots[0] and roots[0]==roots[1]:pair_checks.append(dict(check='shared_insert_parent',source_parent=roots[0],impact='possible_symbol_or_shared_wall_block_not_automatic_rejection'))
    result=save(service,b,'geometry_context_analysis','context_analysis',args,dict(scope=scope,
        label_candidates=associations,entity_features=features,pair_counterevidence=pair_checks,
        ownership=dict(state='competing_candidates' if len(associations)>1 else 'unconfirmed',
            alternatives_retained=True,geometry_label_bindings_confirmed=False),
        next_calls=[dict(tool='render_geometry_workbench',arguments=dict(bundle_id=args.bundle_id,bbox=scope))],
        note='Original titles, numerical elevation mentions and native geometry features are source facts; engineering roles and title ownership require interpretation.'))
    large_fields=('label_candidates','entity_features','pair_counterevidence')
    return {k:v for k,v in result.items() if k not in large_fields}|{
        **{field:result[field][:12] for field in large_fields},
        'field_counts':{field:len(result[field]) for field in large_fields},
        'continuations':{field:dict(tool='read_geometry_artifact',arguments=dict(bundle_id=args.bundle_id,
            collection='geometry_context_analysis',reference_id=result['context_analysis_id'],field=field,offset=12))
            for field in large_fields if len(result[field])>12}}


def trace(service,args):
    b=service.bundle(args.bundle_id);records=selected(b,args.entity_ids)
    if not set(args.seed_entity_ids+args.census_entity_ids)<=set(args.entity_ids):raise ValueError('SEED_OR_CENSUS_OUTSIDE_TRACE_INPUT')
    data=network(records,args.tolerance_drawing_units,args.seed_entity_ids,args.census_entity_ids)
    result=save(service,b,'geometry_network','network',args,data)
    return {k:v for k,v in result.items() if k not in ('primitives','connections','open_endpoints','branches','reachable_primitive_ids')}|dict(
        reachable_primitive_ids=data['reachable_primitive_ids'][:20],reachable_count=len(data['reachable_primitive_ids']),
        primitive_count=len(data['primitives']),connection_count=len(data['connections']),
        open_endpoint_count=len(data['open_endpoints']),open_endpoint_scope=data['traversal_scope'],
        total_input_open_endpoint_count=data['endpoint_counts']['open_input_endpoints'],branch_count=len(data['branches']),
        open_endpoints=data['open_endpoints'][:8],branches=data['branches'][:8],
        pages={field:dict(tool='read_geometry_artifact',arguments=dict(bundle_id=args.bundle_id,
            collection='geometry_network',reference_id=result['network_id'],field=field)) for field in ('primitives','connections','open_endpoints','branches','reachable_primitive_ids')})


def measure_primitives(service,args):
    b=service.bundle(args.bundle_id);net=owned(service,args.bundle_id,'geometry_network',args.network_id)
    by_id={p['entity_id']:p for p in net['primitives']}
    if any(p not in by_id for p in args.primitive_ids):raise ValueError('PRIMITIVE_NOT_IN_NETWORK')
    a,c=[by_id[p] for p in args.primitive_ids]
    result=measure_pair(a,c,'parallel_normal' if a['kind']=='LINE' else 'concentric_radial')
    result.update(source_primitive_ids=args.primitive_ids,source_entity_ids=list(dict.fromkeys([a['source_entity_id'],c['source_entity_id']])),
        source_segments=[dict(entity_id=p['source_entity_id'],segment_index=p['source_segment_index'],entity_path=p['entity_path']) for p in (a,c)],
        unit=dict(unit=b['unit'],basis='drawing_header'),source_unit=b['unit'])
    result=save(service,b,'local_measurement','measurement',args,result)
    result['spatial_fact_scope']['extent']='local_pair_only'
    service.w.store.put('local_measurement',result['measurement_id'],result)
    return result


def basis(service,args):
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.geometry.workbench_routes import contains
    b=service.bundle(args.bundle_id);index=DrawingIndex(service.w,service.sid);items=[]
    for evidence in args.evidence:
        r=index.by_id.get(evidence.region_id)
        if r is None or r.layout_id!=b['layout_id'] or not contains(b['request']['bbox'],r.bbox):raise ValueError('BASIS_SOURCE_OUTSIDE_BUNDLE')
        if evidence.quote not in r.text:raise ValueError('BASIS_QUOTE_NOT_IN_SOURCE')
        items.append(evidence.model_dump()|dict(full_source_text=r.text,bbox=r.bbox,
            source_entity_ids=r.entity_ids,state='quote_verified_applicability_unconfirmed'))
    unit=b['unit'];known=unit in UNIT_TO_M
    unit_candidates=[]
    for e in items:
        if e['role']=='unit':
            for label,u in [('毫米','mm'),('厘米','cm'),('mm','mm'),('cm','cm'),('米','m')]:
                if label in e['quote'] and not (label=='米' and ('毫米' in e['quote'] or '厘米' in e['quote'])):
                    unit_candidates.append(dict(unit=u,region_id=e['region_id'],basis='explicit_quote_candidate'))
    conflicts=[e for e in unit_candidates if known and e['unit']!=unit]
    result=dict(evidence=items,unit_resolution=dict(state='conflict' if conflicts else 'resolved' if known else 'unknown',
        unit=unit,header_basis=True,candidates=unit_candidates,conflicts=conflicts),
        surface_resolution=dict(state='unconfirmed',evidence=[e for e in items if e['role']=='surface']),
        height_resolution=dict(state='unconfirmed',evidence=[e for e in items if e['role']=='height']),
        note='Verified text is not a verified mapping to measured faces or obstacle heights. Header units may be converted only absent conflicting cited evidence.')
    if args.measurement_id:
        m=owned(service,args.bundle_id,'local_measurement',args.measurement_id)
        if m.get('status')!='local_pair_measured':raise ValueError('SUCCESSFUL_MEASUREMENT_REQUIRED')
        if args.target_unit and known and not conflicts:
            result['conversion']=dict(value=m['measurements']['width_drawing_units']*UNIT_TO_M[unit]/UNIT_TO_M[args.target_unit],
                unit=args.target_unit,factor=UNIT_TO_M[unit]/UNIT_TO_M[args.target_unit],
                measurement_id=args.measurement_id,basis='source_header_unit',extent='local_pair_only',surface='unconfirmed')
        elif args.target_unit:result['conversion']=dict(status='blocked',reason='unit_basis_unknown_or_conflicting')
    elif args.target_unit:raise ValueError('CONVERSION_MEASUREMENT_REQUIRED')
    if result.get('conversion',{}).get('value') is not None:
        result['measurements']={'converted_width':{'width_drawing_units':result['conversion']['value']}}
        result['unit']={'unit':args.target_unit,'basis':'explicit_source_header_conversion'}
    return save(service,b,'geometry_basis','basis',args,result)


def owned(service,bid,collection,rid):
    row=service.w.store.get(collection,rid)
    if row.get('bundle_id')!=bid or row.get('snapshot_id')!=service.sid:raise ValueError('FOREIGN_INVESTIGATION_REFERENCE')
    return row


def navigation():
    return dict(spatial_fact_scope=dict(identity=dict(engineering_confirmed=False),extent='navigation_only'),unit=dict(unit='unknown'))


def read_artifact(service,args):
    service.bundle(args.bundle_id);row=owned(service,args.bundle_id,args.collection,args.reference_id)
    if not args.field:
        return dict(**navigation(),bundle_id=args.bundle_id,reference_id=args.reference_id,
            metadata={k:v for k,v in row.items() if not isinstance(v,list)},
            list_fields={k:len(v) for k,v in row.items() if isinstance(v,list)},
            note='Read list fields with field, offset and limit; no evidence deleted.')
    if args.field not in row or not isinstance(row[args.field],list):raise ValueError('LIST_FIELD_REQUIRED')
    data=row[args.field];end=min(len(data),args.offset+args.limit)
    return dict(**navigation(),bundle_id=args.bundle_id,reference_id=args.reference_id,field=args.field,
        items=data[args.offset:end],total=len(data),has_more=end<len(data),next_offset=end if end<len(data) else None)


REFERENCE_COLLECTIONS=('geometry_context_analysis','geometry_network','geometry_basis','geometry_validation','local_measurement','geometry_route','geometry_hypothesis','geometry_investigation_decision')


def decision(service,args):
    b=service.bundle(args.bundle_id);selected(b,args.entity_ids)
    if args.hypothesis_id:owned(service,args.bundle_id,'geometry_hypothesis',args.hypothesis_id)
    for rid in args.reference_ids:
        found=False
        for collection in REFERENCE_COLLECTIONS:
            try:owned(service,args.bundle_id,collection,rid);found=True;break
            except KeyError:pass
        if not found:raise ValueError('DECISION_REFERENCE_NOT_FOUND')
    if args.supersedes:owned(service,args.bundle_id,'geometry_investigation_decision',args.supersedes)
    return save(service,b,'geometry_investigation_decision','decision',args,
        dict(decision=args.decision,reason=args.reason,entity_ids=args.entity_ids,
            hypothesis_id=args.hypothesis_id,reference_ids=args.reference_ids,supersedes=args.supersedes,
            interpretation_author='model',semantic_identity='model_asserted_not_confirmed',
            note='Recorded interpretation with source references; excluded candidates are preserved, not deleted.'))


def plan(service,args):
    # Session-specific progress is added in workbench.execute, never stored as immutable source evidence.
    b=service.bundle(args.bundle_id)
    if args.hypothesis_id:owned(service,args.bundle_id,'geometry_hypothesis',args.hypothesis_id)
    return dict(bundle_id=b['bundle_id'],hypothesis_id=args.hypothesis_id)


SPECS={
 'analyze_geometry_context':(AnalyzeContext,'分析指定对象范围的原生标题竞争关系、楼层/标高文字、几何角色候选、尺寸端点关联及反证检查。来源事实与工程解释分离，不按最近文字自动确认。'),
 'trace_geometry_network':(TraceNetwork,'把源LINE/ARC和二维多段线分解为带原始段索引的精确线弧，按显式原图单位容差追踪端点连通、分支和缺口；独立声明源全集才报告几何覆盖分母，不证明工程全路线。'),
 'measure_geometry_primitives':(MeasurePrimitives,'测量trace_geometry_network中的两段精确线/弧，支持多段线子段并保留原始实体路径与段索引；仍为局部候选。'),
 'resolve_geometry_basis':(ResolveBasis,'核对单位、做法、标高和身份引文确实来自当前范围。分别记录适用性；仅无冲突源头单位可换算已有局部测量，不根据常见数值猜单位。'),
 'read_geometry_artifact':(ReadArtifact,'先读已有结果的元信息与各列表长度，再按字段分页；保留完整不可变证据，不重放所有数组。'),
 'record_geometry_decision':(InvestigationDecision,'引用已有证据记录保留、排除或修订候选的理由；模型判断不升级为人工确认，修订必须显式supersedes。'),
 'plan_geometry_investigation':(PlanInvestigation,'根据本任务当前对象的证据、失败、冲突与未决分支返回可执行补证动作及停滞诊断；不自动消耗调用，不重复已取得证据。'),
}
HANDLERS=dict(analyze_geometry_context=analyze,trace_geometry_network=trace,measure_geometry_primitives=measure_primitives,
    resolve_geometry_basis=basis,read_geometry_artifact=read_artifact,record_geometry_decision=decision,plan_geometry_investigation=plan)
OWNED_IDS=dict(analyze_geometry_context='context_analysis',trace_geometry_network='network',resolve_geometry_basis='basis',
    record_geometry_decision='decision')


class ReadNativeField(Record):
    bundle_id: str
    entity_id: str
    field: str
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=64,ge=1,le=256)


def native_field(service,args):
    b=service.bundle(args.bundle_id);row=selected(b,[args.entity_id])[0]
    if args.field not in row['native']:raise ValueError('NATIVE_FIELD_NOT_PRESENT')
    value=row['native'][args.field]
    if not isinstance(value,list):return dict(**navigation(),bundle_id=args.bundle_id,entity_id=args.entity_id,field=args.field,value=value)
    end=min(len(value),args.offset+args.limit)
    return dict(**navigation(),bundle_id=args.bundle_id,entity_id=args.entity_id,field=args.field,
        items=value[args.offset:end],total=len(value),has_more=end<len(value),next_offset=end if end<len(value) else None)

SPECS['read_geometry_native_field']=(ReadNativeField,'按字段完整分页读取源顶点/样条参数等大数组；summary/native读取中的截断有明确续读入口，不使用显示简化数据替代源参数。')
HANDLERS['read_geometry_native_field']=native_field


class PrimitivePair(Record):
    primitive_ids: list[str] = Field(min_length=2,max_length=2)


class MeasureNetworkRoute(Record):
    bundle_id: str
    network_id: str
    hypothesis_id: str
    pairs: list[PrimitivePair] = Field(min_length=1,max_length=64)
    obstacle_entity_ids: list[str] = Field(default_factory=list,max_length=128)
    samples_per_interval: int = Field(default=9,ge=3,le=129)


def network_route(service,args):
    from cad_agent.geometry.candidate_routes import sample_pair
    b=service.bundle(args.bundle_id);net=owned(service,args.bundle_id,'geometry_network',args.network_id)
    h=owned(service,args.bundle_id,'geometry_hypothesis',args.hypothesis_id)
    primitives={p['entity_id']:p for p in net['primitives']};declared=set(h['request']['entity_ids'])
    if not declared:raise ValueError('HYPOTHESIS_SOURCE_SELECTION_REQUIRED')
    obstacles=selected(b,args.obstacle_entity_ids);segments=[];used=set()
    for pair in args.pairs:
        if any(i not in primitives for i in pair.primitive_ids):raise ValueError('PRIMITIVE_NOT_IN_NETWORK')
        rows=[primitives[i] for i in pair.primitive_ids]
        if any(r['source_entity_id'] not in declared for r in rows):raise ValueError('ROUTE_SOURCE_OUTSIDE_HYPOTHESIS')
        segment=sample_pair(*rows,count=args.samples_per_interval,obstacles=obstacles)
        segment['source_segments']=[dict(entity_id=r['source_entity_id'],segment_index=r['source_segment_index'],primitive_id=r['entity_id']) for r in rows]
        if segment['status']=='sampled_candidate_profile':used.update(pair.primitive_ids)
        # Pair widths are analytic constants over their common domain. Endpoints
        # are retained as domain evidence, not asserted obstacle-clear transitions.
        segment['endpoint_review']=dict(domain=segment.get('domain'),status='boundary_pair_domain_known_transition_and_obstacle_clearance_unproven')
        segments.append(segment)
    required={p['entity_id'] for p in net['primitives'] if p['source_entity_id'] in declared}
    result=save(service,b,'geometry_route','route',args,dict(hypothesis_id=args.hypothesis_id,network_id=args.network_id,
        segments=segments,unpaired_declared_primitive_ids=sorted(required-used),
        branch_count=len(net['branches']),open_endpoint_count=len(net['open_endpoints']),
        topology_coverage=net['coverage'],measurement_coverage=dict(paired_primitives=len(used),declared_primitives=len(required),
            length_fraction=None,reason='pair overlap may cover only part of a primitive; no complete route length denominator'),
        global_minimum_proven=False,whole_route_enumerated=False,obstacle_census='selected_only',
        unit=dict(unit=b['unit']),unresolved=['branch_selection','transition_clearance','obstacle_height','whole_route_census'],
        semantic_identity='model_asserted_not_confirmed'))
    return {k:v for k,v in result.items() if k!='segments'}|dict(segments=[{k:v for k,v in s.items() if k!='samples'}|dict(sample_count=len(s.get('samples',[]))) for s in segments],
        read_action=dict(tool='read_geometry_artifact',arguments=dict(bundle_id=args.bundle_id,collection='geometry_route',reference_id=result['route_id'],field='segments')))

SPECS['measure_geometry_network_route']=(MeasureNetworkRoute,'沿已登记对象的线弧/多段线子段对生成分段宽度剖面，复用拓扑分支和缺口，保留未配对源段与条件障碍影响；配对成功不证明过渡段或全路线最小值。')
HANDLERS['measure_geometry_network_route']=network_route
OWNED_IDS['measure_geometry_network_route']='route'
