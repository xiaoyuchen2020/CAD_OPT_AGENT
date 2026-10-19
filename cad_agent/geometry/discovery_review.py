"""Visual screening of name-independent topology with an explicit ranking frontier."""
from copy import deepcopy
import json
import re
from typing import Literal
from pydantic import Field
from shapely.geometry import Polygon, Point
from cad_agent.analysis.general import DrawingIndex
from cad_agent.contracts import Record, stable_id
from cad_agent.geometry.source import UNIT_TO_M
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.vision import VisionService
from cad_agent.geometry.review_images import sheets

VERSION='discovery-screen-v4-annotation-crosscheck'

def area_annotation_checks(boundary, area_m2, regions, layout_id):
    """Contained native area text is a comparison candidate, never area truth."""
    polygon=Polygon(boundary);checks=[]
    for region in regions:
        if region.get('layout_id')!=layout_id:continue
        box=region.get('bbox')
        if not box or not polygon.contains(Point((box[0]+box[2])/2,(box[1]+box[3])/2)):continue
        for match in re.finditer(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(?:㎡|m²|m2|平方米)',region.get('text',''),re.I):
            value=float(match[1])
            if value<=0:continue
            delta=round(area_m2-value,6)
            checks.append(dict(region_id=region['region_id'],text=region['text'],annotation_area_m2=value,
                geometry_area_m2=area_m2,difference_m2=delta,association_status='contained_text_candidate',
                status='unresolved_area_difference' if abs(delta)>max(.05,value*.05) else 'numerically_close'))
    return checks
class PrepareDiscoveryReview(Record):
    discovery_id: str
    offset: int = Field(default=0,ge=0,le=2000)
    limit: int = Field(default=12,ge=1,le=12)
    prior_review_id: str = Field(default='',description='同一发现集合的已完成审查；会话自动承接前页，保留所有已审查排除和空间。')
class SpaceChoice(Record):
    item_id: str
    classification: Literal['space','furniture','table','frame','wall_band','annotation','boundary_mismatch','duplicate','uncertain']
    reference_item_id: str = ''
    reason: str = Field(min_length=8,max_length=240)
class ReviewDiscoveredSpaces(Record):
    review_id: str
    choices: list[SpaceChoice] = Field(min_length=1,max_length=12)
    top_n: int = Field(default=3,ge=1,le=12)

class DiscoveryReviewService:
    def __init__(self,w,sid):
        self.w,self.sid=w,sid;self.drawing=w.store.get('drawing',sid)
    def get(self,rid):
        r=self.w.store.get('measurement',rid)
        if r['snapshot_id']!=self.sid or r['source_sha256']!=self.drawing['sha256'] or r['kind']!='discovery_review':raise ValueError('不同来源或非空间筛选。')
        return r
    def prepare(self,args):
        source=GeometryService(self.w,self.sid).get(args.discovery_id)
        if source['kind']!='room_discovery':raise ValueError('先调用 discover_rooms。')
        prior=self.get(args.prior_review_id) if args.prior_review_id else None
        if prior and (prior.get('status')!='reviewed_candidates' or prior['discovery_id']!=args.discovery_id):raise ValueError('只能承接同一发现集合的已完成审查。')
        rid=stable_id('measure',source['measurement_id'],VERSION,json.dumps(args.model_dump(),sort_keys=True))
        try:return self.get(rid)
        except KeyError:pass
        candidates=sorted(source['candidates'],key=lambda c:(-c['candidate_shell_area_m2'],c['candidate_id']))
        rows=[]
        regions=[r.model_dump() for r in DrawingIndex(self.w,self.sid).regions]
        for n,c in enumerate(candidates[args.offset:args.offset+args.limit],args.offset+1):
            a,b,d,e=c['bbox'];margin=max(d-a,e-b)*.22
            poly=Polygon(c['boundary'])
            if not poly.is_valid:continue
            rows.append(dict(item_id=c['candidate_id'],number=n,display_label='Space '+str(n),
                crop_bbox=[a-margin,b-margin,d+margin,e+margin],boundary=c['boundary'],
                area_m2=round(poly.area*UNIT_TO_M[source['applied_unit']]**2,6),
                perimeter_m=round(poly.exterior.length*UNIT_TO_M[source['applied_unit']],6),
                diagnostics=c['diagnostics'],candidate_role=c['candidate_role'],
                source_entity_ids=c['entity_ids'],overlays=[('S'+str(n),[c['boundary']])]))
            rows[-1]['area_annotation_checks']=area_annotation_checks(c['boundary'],rows[-1]['area_m2'],regions,source['layout_id'])
        if not rows:raise ValueError('当前页没有可审查轮廓；需调整有实际线形的局部范围，不得输出零面积。')
        r=dict(review_id=rid,measurement_id=rid,kind='discovery_review',algorithm_version=VERSION,snapshot_id=self.sid,source_sha256=self.drawing['sha256'],
            discovery_id=args.discovery_id,layout_id=source['layout_id'],scope_bbox=source['scope_bbox'],applied_unit=source['applied_unit'],
            items=rows,status='awaiting_visual_review',offset=args.offset,total=len(candidates),has_more=args.offset+args.limit<len(candidates),
            omitted_larger_count=args.offset,largest_omitted_area_m2=candidates[args.offset+args.limit]['candidate_shell_area_m2'] if args.offset+args.limit<len(candidates) else None,
            checked_geometry_bboxes=source.get('subdivision',{}).get('checked_bboxes',[source['scope_bbox']]),geometry_complete=source['complete'],
            evidence_url=f'/api/drawings/{self.sid}/measurements/{rid}')
        if prior:r.update(prior_review_id=prior['review_id'],previously_reviewed_count=len(prior['items']),previous_selected=[{k:i[k] for k in ('item_id','number','area_m2','perimeter_m')} for i in prior.get('selected',[])])
        r['images']=sheets(self.w,self.sid,r);self.w.store.put('measurement',rid,r);return r
    def review(self,args,sent):
        r=deepcopy(self.get(args.review_id))
        if len({c.item_id for c in args.choices})!=len(args.choices) or {c.item_id for c in args.choices}!={i['item_id'] for i in r['items']}:raise ValueError('必须逐项筛选，不能遗漏大候选。')
        if not {i['image_id'] for i in r['images']}<=set(sent):raise ValueError('必须实际读取候选审查图像。')
        for image in r['images']:VisionService(self.w,self.sid).png(image['image_id'])
        choices={c.item_id:c for c in args.choices};accepted=[];uncertain=[]
        for row in r['items']:
            c=choices[row['item_id']];row['decision']=c.model_dump()
            if c.classification=='space':
                if row['render_status']!='available' or {'wall_scale_enclosure','boundary_reaches_search_or_frame_limit'}&set(row['diagnostics']):raise ValueError('图像不可用、墙带或触及搜索边界的轮廓不能选作空间。')
                accepted.append(row)
            elif c.classification=='uncertain':uncertain.append(row)
        prior=self.get(r['prior_review_id']) if r.get('prior_review_id') else None
        if prior:
            combined={i['item_id']:deepcopy(i) for i in prior['items']}
            combined.update({i['item_id']:i for i in r['items']})
            r['items']=sorted(combined.values(),key=lambda i:i['number'])
            accepted=[i for i in r['items'] if i['decision']['classification']=='space']
            uncertain=[i for i in r['items'] if i['decision']['classification']=='uncertain']
            source=GeometryService(self.w,self.sid).get(r['discovery_id'])
            unseen=[c['candidate_shell_area_m2'] for c in source['candidates'] if c['candidate_id'] not in combined]
            r['largest_omitted_area_m2']=max(unseen) if unseen else None
            r['omitted_larger_count']=0
            r['images']=list({i['image_id']:i for i in prior['images']+r['images']}.values())
        by_id={i['item_id']:i for i in r['items']}
        for c in args.choices:
            if c.classification=='duplicate':
                if c.reference_item_id==c.item_id or c.reference_item_id not in by_id:raise ValueError('重复候选必须指出同页另一轮廓。')
                p=Polygon(by_id[c.item_id]['boundary']);q=Polygon(by_id[c.reference_item_id]['boundary'])
                if p.intersection(q).area/min(p.area,q.area)<.8:raise ValueError('两处轮廓没有充分重叠，不能以重复排除。')
        # Review must resolve nesting/overlap instead of counting it twice.
        for n,a in enumerate(accepted):
            p=Polygon(a['boundary'])
            for b in accepted[n+1:]:
                q=Polygon(b['boundary'])
                if p.intersection(q).area>min(p.area,q.area)*1e-6:raise ValueError('所选空间内部重叠或嵌套，需重新区分外壳与独立空间。')
        selected=accepted[:args.top_n]
        threshold=selected[-1]['area_m2'] if len(selected)==args.top_n else None
        ranked=threshold is not None and r['omitted_larger_count']==0 and not any(i['area_m2']>=threshold for i in uncertain) and (r['largest_omitted_area_m2'] is None or r['largest_omitted_area_m2']<threshold)
        r.update(parent_review_id=args.review_id,status='reviewed_candidates',ranking_complete=ranked,selected=selected,
            checked_candidate_count=len(r['items']),excluded_count=sum(i['decision']['classification'] not in ('space','uncertain') for i in r['items']),uncertain_count=len(uncertain),
            review_id=stable_id('measure',args.review_id,json.dumps(args.model_dump(),sort_keys=True)))
        r['measurement_id']=r['review_id'];r['evidence_url']=f'/api/drawings/{self.sid}/measurements/{r["review_id"]}'
        r['highlight_action']=dict(mode='replace',kind='geometry',snapshot_id=self.sid,scope=dict(layout_id=r['layout_id'],bbox=r['scope_bbox']),paths=[],
            regions=[dict(kind='candidate_boundary',boundary=i['boundary'],holes=[],number=i['number']) for i in selected],object_ids=[i['item_id'] for i in selected])
        r['operation_receipts']=[dict(operation=o,fulfilled=f) for o,f in [('vision',True),('exclusions',not uncertain),('area',bool(selected)),('perimeter',bool(selected)),('contours',bool(selected)),('discovery',bool(selected)),('ranking',ranked),('selection',bool(selected)),('highlight',bool(selected)),('units',True)]]
        self.w.store.put('measurement',r['review_id'],r);return r
    @staticmethod
    def summary(r):
        out={k:v for k,v in r.items() if k not in ('items','selected','images')}
        out['items']=[{k:v for k,v in i.items() if k not in ('overlays','boundary','source_entity_ids')} for i in r['items']]
        out['selected']=[{k:v for k,v in i.items() if k in ('item_id','number','area_m2','perimeter_m','decision','area_annotation_checks')} for i in r.get('selected',[])]
        out['image_ids']=[i['image_id'] for i in r['images']]
        out['images']=[{'image_id':i['image_id']} for i in r['images']]
        out['note']='面积和外环周长来自所显示蓝色轮廓；模型逐项筛选空间/家具/表格/图框。area_annotation_checks 是轮廓内原生面积文字的交叉核对：unresolved_area_difference 必须说明标注关联及口径尚未解决，不能宣称已确认房间净面积。ranking_complete 只对已检查几何范围与发现集合成立，不证明整层召回。'
        return out
