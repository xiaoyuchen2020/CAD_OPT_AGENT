"""Candidate discovery, view hypotheses and conditional route sampling."""
import json,re
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record,FiniteFloat,stable_id
from cad_agent.geometry.candidate_routes import candidates,sample_pair

VERSION='geometry-route-hypothesis-v3'


class DiscoverPairs(Record):
    bundle_id:str
    kind:Literal['LINE','ARC']='LINE'
    anchor_dimension_id:str=''
    layer:str=''
    bbox:list[FiniteFloat]|None=Field(default=None,min_length=4,max_length=4)
    neighbors:int=Field(default=12,ge=2,le=80)
    offset:int=Field(default=0,ge=0)
    limit:int=Field(default=12,ge=1,le=40)


class InspectViews(Record):
    bundle_id:str
    query:str=''


class Hypothesis(Record):
    bundle_id:str
    label_region_ids:list[str]=Field(default_factory=list,max_length=16)
    entity_ids:list[str]=Field(default_factory=list,max_length=256)
    view_kind:Literal['plan','roof_plan','section','detail','unknown']='unknown'
    interpretation:str=Field(max_length=500)
    unresolved:list[str]=Field(default_factory=list,max_length=20)
    supersedes:str=Field(default='',description='Optional earlier hypothesis ID explicitly replaced by this revision. Other hypotheses remain independent.')


class RouteSegment(Record):
    entity_ids:list[str]=Field(min_length=2,max_length=2)
    requested_interval:list[FiniteFloat]|None=Field(default=None,min_length=2,max_length=2,description='LINE: signed distance from returned domain.origin along domain.direction; ARC: WCS XY degrees in [0,360], split wraparound into separate segments. Use measured domain, not a width or absolute coordinate.')


class MeasureRoute(Record):
    bundle_id:str
    hypothesis_id:str
    segments:list[RouteSegment]=Field(min_length=1,max_length=64)
    obstacle_entity_ids:list[str]=Field(default_factory=list,max_length=128)
    samples_per_interval:int=Field(default=9,ge=3,le=129)


class ExportResults(Record):
    bundle_id:str
    measurement_ids:list[str]=Field(default_factory=list,max_length=128)
    route_ids:list[str]=Field(default_factory=list,max_length=64)
    hypothesis_ids:list[str]=Field(default_factory=list,max_length=64)
    validation_ids:list[str]=Field(default_factory=list,max_length=64)
    network_ids:list[str]=Field(default_factory=list,max_length=64)
    context_analysis_ids:list[str]=Field(default_factory=list,max_length=64)
    basis_ids:list[str]=Field(default_factory=list,max_length=64)
    decision_ids:list[str]=Field(default_factory=list,max_length=64)


def key(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True)
def contains(a,b):return a[0]<=b[0]<=b[2]<=a[2] and a[1]<=b[1]<=b[3]<=a[3]


def discover(service,args):
    b=service.bundle(args.bundle_id);records=b['entities'];by_id={r['entity_id']:r for r in records}
    anchor=by_id.get(args.anchor_dimension_id) if args.anchor_dimension_id else None
    if args.anchor_dimension_id and anchor is None:raise ValueError('ANCHOR_NOT_IN_BUNDLE: retrieve the exact entity_id via read_geometry_bundle.source_paths; native handles are not entity_ids.')
    if args.bbox:
        from cad_agent.geometry.source import finite_box
        finite_box(args.bbox)
        if not contains(b['request']['bbox'],args.bbox):raise ValueError('CANDIDATE_SCOPE_OUTSIDE_BUNDLE')
        a,c,d,e=args.bbox
        records=[r for r in records if r.get('bbox') and not (r['bbox'][2]<a or r['bbox'][0]>d or r['bbox'][3]<c or r['bbox'][1]>e)]
    if args.layer:records=[r for r in records if r['layer']==args.layer]
    identity=args.model_dump(exclude={'offset','limit'})
    cid=stable_id('pairs',b['source_sha256'],VERSION,key(identity))
    try:result=service.w.store.get('geometry_pairs',cid)
    except KeyError:
        result=candidates(records,args.kind,anchor,args.neighbors)
        for item in result['items']:
            item['candidate_id']=stable_id('pair',cid,*item['entity_ids'])
            item['next_call']=dict(tool='measure_local_geometry',arguments=dict(bundle_id=args.bundle_id,entity_ids=item['entity_ids'],basis=item['basis']))
        result.update(candidate_set_id=cid,bundle_id=args.bundle_id,request=identity)
        service.w.store.put('geometry_pairs',cid,result)
    total=len(result['items']);end=min(total,args.offset+args.limit)
    return {k:v for k,v in result.items() if k!='items'}|dict(items=result['items'][args.offset:end],total=total,
        has_more=end<total,next_offset=end if end<total else None,
        spatial_selection='bbox_intersection_of_whole_sources_not_clipping',
        note='Pair score explains source endpoint correspondence; does not identify wall faces or select by rule width.')


def views(service,args):
    from cad_agent.analysis.general import DrawingIndex
    b=service.bundle(args.bundle_id);index=DrawingIndex(service.w,service.sid);items=[]
    for region in index.regions:
        if region.layout_id!=b['layout_id'] or not contains(b['request']['bbox'],region.bbox):continue
        if args.query and args.query not in region.text:continue
        kinds=[name for name,pattern in [('plan',r'平面|plan'),('section',r'剖面|section'),('detail',r'详图|大样|detail'),('roof_plan',r'顶板|屋面')]
               if re.search(pattern,region.text,re.I)]
        if kinds:items.append(dict(region_id=region.region_id,text=region.text,bbox=region.bbox,
            view_candidates=kinds,identity='unconfirmed',source_entity_ids=region.entity_ids))
    return dict(bundle_id=args.bundle_id,items=items,interpretation='title candidates may be adjacent, duplicated in title block, or concatenated; not spatial partitions',target_complete=False)


def hypothesis(service,args):
    from cad_agent.analysis.general import DrawingIndex
    b=service.bundle(args.bundle_id);records={r['entity_id']:r for r in b['entities']}
    if any(e not in records for e in args.entity_ids):raise ValueError('HYPOTHESIS_SOURCE_NOT_IN_BUNDLE: entity_ids are geometry records; use label_region_ids for text labels, and source_paths to retrieve native geometry IDs.')
    if args.supersedes:
        previous=service.w.store.get('geometry_hypothesis',args.supersedes)
        if previous['bundle_id']!=args.bundle_id:raise ValueError('FOREIGN_SUPERSEDED_HYPOTHESIS')
    index=DrawingIndex(service.w,service.sid);labels=[]
    for rid in args.label_region_ids:
        r=index.by_id.get(rid)
        if r is None or r.layout_id!=b['layout_id'] or not contains(b['request']['bbox'],r.bbox):raise ValueError('HYPOTHESIS_LABEL_OUTSIDE_BUNDLE')
        labels.append(dict(region_id=rid,text=r.text,bbox=r.bbox,entity_ids=r.entity_ids))
    hid=stable_id('gh',service.sid,b['source_sha256'],VERSION,key(args.model_dump()))
    record=dict(hypothesis_id=hid,snapshot_id=service.sid,bundle_id=args.bundle_id,source_sha256=b['source_sha256'],
        labels=labels,request=args.model_dump(),semantic_identity='model_asserted_not_confirmed',
        source_binding=dict(state='geometry_sources_checked',engineering_applicability_state='unverified'),selected_by='model',unresolved=args.unresolved,
        supersedes=args.supersedes,
        note='Source existence is verified; label-to-geometry ownership is not inferred from sharing a bundle.')
    service.w.store.put('geometry_hypothesis',hid,record);return record


def route(service,args):
    b=service.bundle(args.bundle_id);h=service.w.store.get('geometry_hypothesis',args.hypothesis_id)
    if h['bundle_id']!=args.bundle_id:raise ValueError('FOREIGN_HYPOTHESIS')
    records={r['entity_id']:r for r in b['entities']}
    ids=args.obstacle_entity_ids+[e for segment in args.segments for e in segment.entity_ids]
    if any(e not in records for e in ids):raise ValueError('ROUTE_SOURCE_NOT_IN_BUNDLE')
    declared=set(h['request']['entity_ids'])
    if declared and any(e not in declared for segment in args.segments for e in segment.entity_ids):raise ValueError('ROUTE_SOURCE_OUTSIDE_HYPOTHESIS')
    rid=stable_id('route',service.sid,VERSION,key(args.model_dump()))
    try:result=service.w.store.get('geometry_route',rid)
    except KeyError:
        segments=[sample_pair(*(records[e] for e in s.entity_ids),count=args.samples_per_interval,
            obstacles=[records[e] for e in args.obstacle_entity_ids],requested_interval=s.requested_interval) for s in args.segments]
        result=dict(route_id=rid,bundle_id=args.bundle_id,hypothesis_id=args.hypothesis_id,request=args.model_dump(),
            source_sha256=b['source_sha256'],snapshot_id=service.sid,segments=segments,global_minimum_proven=False,
            semantic_identity=h['semantic_identity'],obstacle_census='selected_only',
            whole_route_enumerated=False,unit=dict(unit=b['unit']),
            spatial_fact_scope=dict(identity=dict(engineering_confirmed=False),extent='sampled_selected_sources_only'),
            unresolved=h['unresolved']+['route_continuity_and_transitions_unconfirmed','obstacle_height_relations_unconfirmed'])
        service.w.store.put('geometry_route',rid,result)
    summaries=[]
    for s in result['segments']:
        rows=s.get('samples',[]);hits=[r for r in rows if r['projected_obstacle_ids']]
        summaries.append({k:v for k,v in s.items() if k!='samples'}|dict(sample_count=len(rows),obstacle_hit_count=len(hits),
            sample_examples=(hits[:3] if hits else rows[:2]),samples_artifact=rid))
    return {k:v for k,v in result.items() if k!='segments'}|dict(segments=summaries)


def export(service,args):
    b=service.bundle(args.bundle_id);data={}
    for name,collection,ids in [('measurements','local_measurement',args.measurement_ids),('routes','geometry_route',args.route_ids),('hypotheses','geometry_hypothesis',args.hypothesis_ids),('validations','geometry_validation',args.validation_ids),('networks','geometry_network',args.network_ids),('context_analyses','geometry_context_analysis',args.context_analysis_ids),('bases','geometry_basis',args.basis_ids),('decisions','geometry_investigation_decision',args.decision_ids)]:
        rows=[service.w.store.get(collection,i) for i in ids]
        if any(r['bundle_id']!=args.bundle_id for r in rows):raise ValueError('FOREIGN_RESULT_EXPORT')
        data[name]=rows
    eid=stable_id('gr',service.sid,VERSION,key(args.model_dump()))
    result=dict(result_id=eid,snapshot_id=service.sid,source_sha256=b['source_sha256'],bundle_id=args.bundle_id,
        unit=b['unit'],global_minimum_proven=False,**data)
    service.w.store.put('geometry_resultset',eid,result)
    path=service.w.store.artifact_dir(service.sid)/'geometry-results'/f'{eid}.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(key(result))
    return dict(result_id=eid,bundle_id=args.bundle_id,counts={k:len(v) for k,v in data.items()},
        artifact_name=f'{eid}.json',download_url=f'/api/drawings/{service.sid}/geometry-results/{eid}',
        status='source_results_exported',global_minimum_proven=False)


SPECS={
 'discover_geometry_pairs':(DiscoverPairs,'枚举同心弧或有限平行线对候选。可用源DIMENSION端点作锚点，按端点残差和有效重叠排序，不用规则阈值选面；返回可直接调用的测量参数。'),
 'inspect_geometry_views':(InspectViews,'列出已加载范围内原生图示标题、坐标与来源，允许共框多标题；不自动把标题位置当图示边界。'),
 'register_geometry_hypothesis':(Hypothesis,'分别记录图示/对象候选、源标签与实体及未覆盖部分；来源核验和工程归属分开，不因共图框确认同对象。'),
 'measure_geometry_route':(MeasureRoute,'对显式对象假设的线/弧分段采样，给出原生边界宽度、声明区间缺口和所选障碍若同高度时的投影影响。自动加障碍顶点事件采样，仍不证明全程最小值。'),
 'export_geometry_results':(ExportResults,'直接导出本bundle已有测量、路线、假设回执，无需生成Python抄写数值；局部结果不升级为全程验收。'),
}
HANDLERS={'discover_geometry_pairs':discover,'inspect_geometry_views':views,'register_geometry_hypothesis':hypothesis,'measure_geometry_route':route,'export_geometry_results':export}
