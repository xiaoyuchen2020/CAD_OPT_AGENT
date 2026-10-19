"""Source-checked reusable result sets and deterministic measurement operations."""
from copy import deepcopy
from decimal import Decimal
import re
from typing import Literal
from pydantic import Field
from shapely.geometry import Polygon
from cad_agent.contracts import Record, stable_id
from cad_agent.geometry.source import UNIT_TO_M
from cad_agent.geometry.room_batch import RoomBatchService
from cad_agent.analysis.tasks import TaskLedger

class PreviousResults(Record):
    pass

class AnalyzeMeasurements(Record):
    batch_id: str = Field(default='',description='名称批次；与discovery_id、review_id互斥，只填一项。')
    discovery_id: str = Field(default='',description='原始未筛选发现集合；与batch_id、review_id互斥。已筛选空间请只填review_id。')
    review_id: str = Field(default='',description='已逐项审查的空间集合；使用时batch_id及discovery_id均留空。')
    candidate_ids: list[str] = Field(default_factory=list,max_length=20)
    metric: Literal['area','perimeter'] = 'area'
    operation: Literal['list','rank','range','sum','mean'] = 'list'
    descending: bool = True
    limit: int | None = Field(default=None,ge=1,le=100)
    replace_highlights: bool = False


def restore_previous(session):
    parent=session.request.parent_run_id
    if not parent:raise ValueError('当前问题没有父会话。')
    run=session.w.store.get('chat_run',parent)
    if run['snapshot_id']!=session.request.snapshot_id or run['status']!='completed':
        raise ValueError('仅可复用同图纸已发布的父会话。')
    task=TaskLedger.read(session.w,run['task_id'])
    if task['source_sha256']!=session.drawing['sha256']:raise ValueError('来源已变化。')
    from cad_agent.analysis.object_registry import context, restore_registry
    registry = task.get('object_registry')
    if registry and registry.get('context') != context(session):
        raise ValueError('对象来源、算法版本或单位约定已变化，请重新读取和测量。')
    if not registry:
        from cad_agent.geometry.room_batch import VERSION as BATCH_VERSION
        for batch in task.get('batches', {}).values():
            if batch.get('version') != BATCH_VERSION:
                raise ValueError('历史批次算法版本无法核对，请重新读取。')
            policy = session.task.data.get('unit_policy')
            if policy and policy.get('condition') == 'explicit' and policy.get('unit') != batch.get('unit'):
                raise ValueError('历史批次单位与本轮约定不同，请重新读取和测量。')
    restore_registry(session, task)
    # Do not claim historical images were seen in the current model turn.
    # Historical interpretation remains attached to immutable batch provenance.
    batches=deepcopy(task['batches'])
    session.room_batches.update(batches)
    session.task.data['batches'].update(batches)
    retained={eid:deepcopy(e) for eid,e in task['evidence'].items() if e.get('snapshot_id') in session.allowed_snapshots}
    session.evidence.update(retained)
    session.task.data['evidence'].update(retained)
    session.task.data['inherited_from']=dict(run_id=parent,task_id=task['task_id'],source_sha256=task['source_sha256'])
    reusable={oid:deepcopy(o) for oid,o in task['observations'].items() if o.get('snapshot_id')==session.request.snapshot_id and o['tool'] in ('discover_rooms','get_room_contours','analyze_measurements','measure_room_area','check_dimension_chain','query_building_relations','read_dimensions','review_space_connections','review_discovered_spaces')}
    session.observations.update(reusable)
    session.task.data['observations'].update(reusable)
    session.task.data['spatial_results']=deepcopy(task.get('spatial_results',[]))
    session.task.save()
    reviewed=[o for o in task['observations'].values() if o['tool'] in ('review_space_connections','review_discovered_spaces','select_room_boundaries')]
    fresh_review=bool(re.search(r'重新.{0,4}(看图|核查|审查|检查)|再次.{0,4}(看图|核查|审查|检查)|重新复核',session.request.question))
    inherited_receipts=[dict(operation='vision',fulfilled=True,basis='inherited_same_source_review',parent_run_id=parent)] if reviewed and not fresh_review else []
    return dict(reusable_observations=list(reusable.values()),parent_run_id=parent,source_sha256=task['source_sha256'],
                operation_receipts=inherited_receipts,
                batches=[RoomBatchService.summary(b) for b in batches.values()],
                note='已核对同源版本并恢复可操作批次；保留历史候选身份，不视为本轮视觉复核。')


def analyze(session,args):
    batch=collection(session,args)
    if batch.get('source_sha256',session.drawing['sha256'])!=session.drawing['sha256']:raise ValueError('批次来源已变化。')
    policy=getattr(session.task,'data',{}).get('unit_policy')
    if policy and policy['condition']=='explicit' and policy['unit']!=batch['unit']:
        raise ValueError('本轮单位与历史结果不同，请先resolve_room_units按本轮约定重新测量。')
    factor=UNIT_TO_M.get(batch['unit'])
    if not factor:raise ValueError('没有已解决的测量单位。')
    values=[];missing=[];polygons=[]
    for item in batch['items']:
        h=item.get('highlight',{})
        if h.get('kind')!='candidate_boundary':missing.append(item['number']);continue
        p=Polygon(h['boundary'],h.get('holes',[]))
        if not p.is_valid or p.is_empty:raise ValueError('无效候选多边形。')
        basis='selected_enclosure' # Never promote to net area, including legacy status.
        value=round(p.area*factor**2 if args.metric=='area' else p.exterior.length*factor,6)
        unit='m2' if args.metric=='area' else 'm'
        ref=stable_id('value',session.request.snapshot_id,batch['batch_id'],item['region_id'],args.metric,basis)
        values.append(dict(measurement_ref=ref,object_id=item['region_id'],number=item['number'],
                           field=args.metric,value=value,unit=unit,basis=basis,batch_id=batch['batch_id']))
        polygons.append(p)
    if not values:raise ValueError('集合中尚无可测量的已选择轮廓。')
    # Operate at stored six-decimal precision; tie by original number, not input order.
    ordered=sorted(values,key=lambda v:((-1 if args.descending else 1)*Decimal(str(v['value'])),v['number']))
    selected=ordered[:args.limit] if args.limit else ordered
    calc=None
    if args.operation in ('range','sum','mean'):
        if missing:raise ValueError('集合有未测量项，不能将局部聚合伪装为完整集合。')
        if args.operation in ('sum','mean') and any(p.intersection(q).area>min(p.area,q.area)*1e-6 for j,p in enumerate(polygons) for q in polygons[j+1:]):
            raise ValueError('候选轮廓重叠，不能聚合。')
        nums=[Decimal(str(v['value'])) for v in values]
        total=max(nums)-min(nums) if args.operation=='range' else sum(nums)
        if args.operation=='mean':total/=len(nums)
        calc=dict(operation=args.operation,value=float(round(total,6)),unit=values[0]['unit'],basis=values[0]['basis'],
                  operand_refs=[v['measurement_ref'] for v in values])
    receipts=[dict(operation=args.metric,fulfilled=not missing)]
    if args.operation in ('rank','range') or args.limit:receipts.append(dict(operation='ranking',fulfilled=not missing))
    if calc:receipts.append(dict(operation='arithmetic',fulfilled=True))
    action=None
    if args.replace_highlights:
        # Subset replaces current display, immutable source batch stays complete.
        by_id={i['region_id']:i for i in batch['items']}
        action=dict(mode='replace',snapshot_id=session.request.snapshot_id,batch_id=args.batch_id,
                    scope=batch['scope'],items=[deepcopy(by_id[v['object_id']]) for v in selected])
        if args.discovery_id or args.review_id:
            action=dict(mode='replace',kind='geometry',snapshot_id=session.request.snapshot_id,scope=batch['scope'],
                        regions=[deepcopy(by_id[v['object_id']]['highlight']) for v in selected],paths=[],object_ids=[v['object_id'] for v in selected])
        session.highlight_action=action
        session.task.data['highlight_action']=action
        receipts.extend([dict(operation='selection',fulfilled=True),dict(operation='highlight',fulfilled=True)])
    return dict(batch_id=batch['batch_id'],metric=args.metric,operation=args.operation,values=values,
                ordered=selected,calculation=calc,missing_numbers=missing,checked_count=len(values),
                total_candidates=batch['total'],coverage=batch.get('coverage_note','current_batch_only'),highlight_action=action,
                operation_receipts=receipts,note='数值是所选围合几何；周长仅计外环，未自动扣除内部对象；不是确认净面积。')


def collection(session,args):
    if sum(bool(v) for v in (args.batch_id,args.discovery_id,args.review_id))!=1:raise ValueError('指定batch_id、discovery_id或review_id其中一项。')
    if args.review_id:
        from cad_agent.geometry.discovery_review import DiscoveryReviewService
        if not any(o['tool']=='review_discovered_spaces' and o['data'].get('review_id')==args.review_id for o in session.observations.values()):raise ValueError('先读取本轮或同源父会话的空间审查结果。')
        record=DiscoveryReviewService(session.w,session.request.snapshot_id).get(args.review_id)
        selected=record.get('selected',[]);known={i['item_id'] for i in selected}
        chosen=set(args.candidate_ids) if args.candidate_ids else known
        if not chosen or not chosen<=known or len(args.candidate_ids)!=len(set(args.candidate_ids)):raise ValueError('只能筛选已通过审查的空间集合。')
        items=[dict(number=i['number'],region_id=i['item_id'],highlight=dict(kind='candidate_boundary',boundary=i['boundary'],holes=[])) for i in selected if i['item_id'] in chosen]
        return dict(batch_id=args.review_id,source_sha256=record['source_sha256'],unit=record['applied_unit'],items=items,total=len(selected),
                    scope=dict(layout_id=record['layout_id'],bbox=record['scope_bbox']),coverage_note='reviewed_selected_spaces_only')
    if args.batch_id:
        if args.candidate_ids:raise ValueError('名称批次不接受空间发现ID。')
        if args.batch_id not in session.room_batches:raise ValueError('先读取当前或父会话结果集合。')
        return session.room_batches[args.batch_id]
    from cad_agent.geometry.service import GeometryService
    record=GeometryService(session.w,session.request.snapshot_id).get(args.discovery_id)
    if record['kind']!='room_discovery':raise ValueError('不是空间发现结果。')
    seen={i['candidate_id'] for o in session.observations.values() if o['tool']=='discover_rooms' and o['data'].get('measurement_id')==args.discovery_id for i in o['data'].get('items',[])}
    chosen=args.candidate_ids or sorted(seen)
    if not chosen or len(set(chosen))!=len(chosen) or not set(chosen)<=seen:raise ValueError('只能选择已读取且不重复的空间候选。')
    items=[]
    for n,c in enumerate(record['candidates'],1):
        if c['candidate_id'] not in chosen:continue
        items.append(dict(number=n,region_id=c['candidate_id'],highlight=dict(kind='candidate_boundary',boundary=c['boundary'],holes=[])))
    return dict(batch_id=args.discovery_id,source_sha256=record['source_sha256'],unit=record['applied_unit'],items=items,
        total=len(record['candidates']),scope=dict(layout_id=record['layout_id'],bbox=record['scope_bbox']),
        coverage_note=dict(selected_count=len(items),read_count=len(seen),discovery_count=len(record['candidates']),
                           geometry_complete=record['complete'],note='仅比较所选已读候选；未确认房间身份，未读页面不在排名中。'))
