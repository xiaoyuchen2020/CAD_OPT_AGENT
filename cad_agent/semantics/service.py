"""Shared bounded HTTP/agent interface for inspectable semantic candidates."""
import copy
import math
import re
from itertools import combinations

from shapely.geometry import MultiPoint

from cad_agent.analysis.general import DrawingIndex, intersects
from cad_agent.contracts import stable_id
from .annotations import AnnotationBuilder, VERSION
from .tables import read_semantic_table
from .details import detail_catalog, TRACE_VERSION
from .reference_scope import reference_scope


class SemanticService:
    def __init__(self, workspace, snapshot_id, index=None):
        self.w,self.sid=workspace,snapshot_id
        self.index=index or DrawingIndex(workspace,snapshot_id)

    def annotations(self,layout_id):
        self.index.scope(layout_id)
        # One immutable snapshot/layout cache, bounded like native geometry.
        key=(self.sid,layout_id,VERSION,self.index.drawing['sha256'])
        with self.w.lock:
            cache=getattr(self.w,'_annotation_semantics',None)
            if cache and cache[0]==key:return cache[1]
            result=AnnotationBuilder(self.index,layout_id).build()
            self.w._annotation_semantics=(key,result)
            return result

    def evidence(self,item):
        return [e for eid in item['text_entity_ids'] for e in self.index.entity_evidence(eid)]

    def query(self,args):
        self.index.scope(args.layout_id,args.frame_id)
        frame=next((f for f in self.index.drawing['frames'] if f['frame_id']==args.frame_id),None)
        if frame and frame['layout_id']!=args.layout_id:raise ValueError('图框与布局不匹配。')
        data=self.annotations(args.layout_id)
        items=[v for v in data[args.kind] if (not frame or intersects(v['bbox'],frame['bbox'])) and
               (args.bbox is None or intersects(v['bbox'],args.bbox)) and
               (not args.query or args.query.casefold() in v.get('label','').casefold())]
        page=[copy.deepcopy(v)|{'evidence':self.evidence(v)} for v in items[args.offset:args.offset+args.limit]]
        return dict(snapshot_id=self.sid,source_sha256=self.index.drawing['sha256'],layout_id=args.layout_id,
                    kind=args.kind,items=page,total=len(items),offset=args.offset,has_more=args.offset+args.limit<len(items),
                    version=VERSION,coverage=data['coverage'],diagnostics=data['diagnostics'],
                    note=data['note'])

    def locate(self,args):
        if len(set(args.axis_ids))!=len(args.axis_ids):raise ValueError('轴线 ID 不能重复。')
        byid={v['object_id']:v for v in self.annotations(args.layout_id)['axes']}
        if any(i not in byid for i in args.axis_ids):raise ValueError('轴线不属于当前快照和布局。')
        axes=[byid[i] for i in args.axis_ids]
        def intersection(u,v):
            a,b=u['line'];c,d=v['line']
            ux,uy=b[0]-a[0],b[1]-a[1];vx,vy=d[0]-c[0],d[1]-c[1]
            det=ux*vy-uy*vx
            if abs(det)<=1e-5*math.hypot(ux,uy)*math.hypot(vx,vy):return None
            t=((c[0]-a[0])*vy-(c[1]-a[1])*vx)/det
            return [a[0]+t*ux,a[1]+t*uy]
        pairs=[(u,v,intersection(u,v)) for u,v in combinations(axes,2)]
        points=[p for u,v,p in pairs if p is not None]
        status='candidate';diagnostics=[];boundary=None;point=None
        if len(axes)==2 and len(points)==1:point=points[0]
        elif len(axes)==4 and len(points)==4:
            poly=MultiPoint(points).convex_hull
            if poly.geom_type=='Polygon' and len(poly.exterior.coords)==5:boundary=[list(p) for p in poly.exterior.coords]
            else:status='unresolved';diagnostics.append('DEGENERATE_AXIS_REGION')
        else:status='unresolved';diagnostics.append('SELECT_TWO_CROSSING_OR_FOUR_PAIRED_PARALLEL_AXES')
        if any(a['alternative_line_ids'] for a in axes):diagnostics.append('MULTIPLE_AXIS_LINE_CANDIDATES')
        return dict(snapshot_id=self.sid,layout_id=args.layout_id,state=status,point=point,boundary=boundary,
                    coordinate_space='dxf_wcs',unit=self.index.drawing['unit'],axes=axes,
                    evidence=[e for a in axes for e in self.evidence(a)],diagnostics=diagnostics,
                    note='由候选轴线的无限延长线求交；不确认子图归属，也不以轴间距作为房间净尺寸。')

    def trace(self,args):
        refs=self.annotations(args.layout_id)['references']
        ref=next((r for r in refs if r['object_id']==args.reference_id),None)
        if ref is None:raise ValueError('索引不属于当前快照和布局。')
        targets=[];scopes=list(dict.fromkeys([self.sid]+args.target_snapshot_ids))
        scope=reference_scope(self.index,ref)
        number=ref['detail_number'];sheet=scope['effective_sheet_number'];excluded=0
        excluded_targets=[];sheet_inventory=[]
        def catalog(index):
            key=(index.snapshot,index.drawing['sha256'],TRACE_VERSION)
            with self.w.lock:
                cache=getattr(self.w,'_detail_catalogs',{})
                if key not in cache:
                    if len(cache)>=5:cache.pop(next(iter(cache)))
                    cache[key]=detail_catalog(index)
                    self.w._detail_catalogs=cache
                return cache[key]
        source_catalog=catalog(self.index)
        center=ref.get('center') or [(ref['bbox'][0]+ref['bbox'][2])/2,(ref['bbox'][1]+ref['bbox'][3])/2]
        source_borders=[b for b in source_catalog['borders'] if b['layout_id']==args.layout_id and
                        b['bbox'][0]<=center[0]<=b['bbox'][2] and b['bbox'][1]<=center[1]<=b['bbox'][3]]
        source_borders.sort(key=lambda b:(b['bbox'][2]-b['bbox'][0])*(b['bbox'][3]-b['bbox'][1]))
        source_border=source_borders[0]['id'] if source_borders else None
        def source_records(index, ids):
            return [{k:e[k] for k in ('snapshot_id','region_id','layout_id','frame_id','quote','text_span','entity_ids','bbox')}
                    for eid in dict.fromkeys(ids) for e in index.entity_evidence(eid)]
        for sid in scopes:
            index=self.index if sid==self.sid else DrawingIndex(self.w,sid)
            target_catalog=catalog(index)
            sheet_inventory.extend(dict(snapshot_id=sid,value=v['value'],bbox=v['bbox'],
                border_id=v['border']['id'] if v['border'] else None,
                source_records=source_records(index,v['text_entity_ids'])) for v in target_catalog['sheets'])
            # Atlas identity cannot be inferred from a page number or a matching
            # key in an unrelated drawing. No implicit external retrieval.
            if sheet is None:continue
            for t in target_catalog['targets']:
                if t['detail_number'].casefold()!=number.casefold():continue
                if sid==self.sid and set(t['text_entity_ids']).intersection(ref['text_entity_ids']):continue
                declared=t['sheet_candidates']
                sheet_matches=[v for v in declared if v['value'].casefold()==sheet.casefold()]
                same_sheet=sheet in ('-','—','本图') and sid==self.sid and t['layout_id']==args.layout_id and source_border in t['border_ids']
                mismatch=('different_closed_border' if sheet in ('-','—','本图') and source_border and t['border_ids'] and not same_sheet
                          else 'different_sheet_number' if sheet not in ('-','—','本图') and declared and not sheet_matches else None)
                if mismatch:
                    excluded+=1
                    if len(excluded_targets)<24:
                        excluded_targets.append(dict(snapshot_id=sid,source_sha256=index.drawing['sha256'],layout_id=t['layout_id'],
                            detail_number=t['detail_number'],title=t['title'],title_state=t.get('title_state','observed'),bbox=t['bbox'],
                            method=t['method'],reason=mismatch,declared_sheet_numbers=sorted({v['value'] for v in declared}),
                            support_entity_ids=t['support_entity_ids'],
                            source_records=source_records(index,t['text_entity_ids']+[eid for v in declared for eid in v['text_entity_ids']])))
                    continue
                state='candidate_key_match' if sheet_matches or same_sheet else 'candidate_sheet_unresolved'
                sheet_conflict=len({v['value'].casefold() for v in declared})>1
                if sheet_conflict:state='candidate_sheet_conflict'
                ids=list(dict.fromkeys(t['text_entity_ids']+[eid for v in sheet_matches for eid in v['text_entity_ids']]))
                sources=source_records(index,ids)
                targets.append(dict(target_id=stable_id('detailtarget',sid,TRACE_VERSION,t['entity_id'],t['detail_number'],*t['support_entity_ids'][:1]),snapshot_id=sid,
                    source_sha256=index.drawing['sha256'],layout_id=t['layout_id'],bbox=t['bbox'],
                    entity_id=t['entity_id'],title=t['title'],detail_number=t['detail_number'],state=state,source_records=sources,
                    method=t['method'],title_state=t.get('title_state','observed'),support_entity_ids=t['support_entity_ids'],
                    sheet_entity_ids=[eid for v in sheet_matches for eid in v['text_entity_ids']],
                    sheet_basis='explicit_titleblock_field' if sheet_matches else 'same_closed_border' if same_sheet else 'unresolved',
                    diagnostics=(['DETAIL_KEY_CASE_NORMALIZED'] if t['detail_number']!=number else [])+
                                (['DETAIL_TITLE_UNRESOLVED'] if t.get('title_state')=='unresolved' else [])+
                                (['TITLE_FROM_SHEET_CONTEXT_ONLY'] if t.get('title_state')=='sheet_context_only' else [])+
                                (['SHEET_FIELD_CONFLICT'] if sheet_conflict else [])))
        targets.sort(key=lambda t:(t['state'],t['snapshot_id'],t['target_id']))
        requested_sheet_observed=bool(sheet and any(v['value'].casefold()==sheet.casefold() for v in sheet_inventory))
        diagnosis=('reference_scope_conflict' if scope['state']=='conflict' else
                   'external_atlas_reference' if scope['document_kind']=='standard_atlas' else
                   'multiple_target_candidates' if len(targets)>1 else
                   'target_candidate_found' if targets else
                   'same_key_different_scope' if excluded else
                   'requested_sheet_not_observed' if sheet not in ('-','—','本图') and not requested_sheet_observed else
                   'target_not_parsed_in_requested_scope')
        return dict(snapshot_id=self.sid,reference=copy.deepcopy(ref),evidence=self.evidence(ref),
                    version=TRACE_VERSION,items=targets[args.offset:args.offset+args.limit],total=len(targets),offset=args.offset,
                    has_more=args.offset+args.limit<len(targets),searched_snapshot_ids=scopes,excluded_sheet_mismatches=excluded,
                    reference_scope=scope,diagnosis=dict(code=diagnosis,requested_sheet_observed=requested_sheet_observed,
                        source_border_id=source_border,missing_resource_confirmed=False,
                        note='未观测到图号不等于证明文件缺失；异图同号仅供核对，未替换原索引。图集来源尚未进行文档身份匹配。'),
                    excluded_targets=excluded_targets,excluded_targets_truncated=excluded>len(excluded_targets),
                    sheet_inventory=sheet_inventory[:64],sheet_inventory_total=len(sheet_inventory),
                    status='unresolved' if not targets else 'ambiguous' if len(targets)>1 else targets[0]['state'],
                    note='仅搜索当前图纸及显式指定快照；编号圆、标题下划线及图签字段保留来源。目标仍为候选，未知图号或多目标不自动确认；布局兜底范围不证明同一张图。')

    def table(self,args):
        return read_semantic_table(self.index,args)
