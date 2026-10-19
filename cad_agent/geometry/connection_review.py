"""Per-pair source geometry, jamb measurements and explicit visual adjudication.

A text-to-text segment is only a search probe, never a passage certificate.
Decisions remain source-bound interpretations; no nearest-room assignment.
"""
from copy import deepcopy
import json
import math
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record, stable_id
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.source import scoped_geometry_index, UNIT_TO_M
from cad_agent.geometry.vision import VisionService
from cad_agent.geometry.review_images import sheets
from cad_agent.components.openings import arc_sources
from cad_agent.geometry.compute import compute
from cad_agent.geometry.connection_compute import jamb_measurement, local_evidence_from_sources
from cad_agent.components.dimensions import read_dimensions

VERSION='pair-geometry-review-v6'

class SpacePair(Record):
    source_region_id: str
    target_region_id: str

class PrepareConnections(Record):
    layout_id: str
    pairs: list[SpacePair] = Field(min_length=1,max_length=6)
    excluded_region_ids: list[str] = Field(default_factory=list,max_length=6,description='疑似表格等不参与空间配对的名称，也要逐项出图确认排除。')
    unit_override: Literal['mm','cm','m','in','ft'] | None = None

class ConnectionChoice(Record):
    item_id: str
    classification: Literal['open','door','wall','uncertain','not_plan_space']
    opening_ids: list[str] = Field(default_factory=list,max_length=12)
    reason: str = Field(min_length=8,max_length=300)

class ReviewConnections(Record):
    review_id: str
    choices: list[ConnectionChoice] = Field(min_length=1,max_length=12)


def local_evidence(records, source, factor, anchors, bounds, texts=()):
    """Compatibility adapter for local callers; native handles stay in parent."""
    return local_evidence_from_sources(records,arc_sources(records,source),factor,anchors,bounds,texts)


class ConnectionReviewService:
    def __init__(self,w,sid):
        self.w,self.sid=w,sid;self.drawing=w.store.get('drawing',sid)
    def get(self,rid):
        r=self.w.store.get('measurement',rid)
        if r['snapshot_id']!=self.sid or r['source_sha256']!=self.drawing['sha256'] or r['kind']!='connection_review':raise ValueError('不同来源或非关系审查。')
        return r
    def prepare(self,args):
        factor=UNIT_TO_M.get(args.unit_override or self.drawing['unit'])
        if not factor:raise ValueError('需要明确单位才能选择物理范围和测距。')
        rid=stable_id('measure',self.sid,self.drawing['sha256'],VERSION,json.dumps(args.model_dump(),sort_keys=True))
        try:return self.get(rid)
        except KeyError:pass
        from cad_agent.geometry.source import source_descriptors
        reading=DrawingIndex(self.w,self.sid);reading.scope(args.layout_id,'')
        rows=[];identities=set()
        for n,pair in enumerate(args.pairs,1):
            ids=(pair.source_region_id,pair.target_region_id)
            if ids[0]==ids[1] or tuple(sorted(ids)) in identities:raise ValueError('空间配对不能重复或引用同一名称。')
            identities.add(tuple(sorted(ids)))
            labels=[reading.by_id.get(i) for i in ids]
            if any(r is None or r.layout_id!=args.layout_id for r in labels):raise ValueError('名称必须来自当前布局。')
            anchors=[[(r.bbox[0]+r.bbox[2])/2,(r.bbox[1]+r.bbox[3])/2] for r in labels]
            if math.dist(*anchors)*factor>30:raise ValueError('目标相距过远，请检查是否误配表格/其他楼层。')
            margin=2/factor;bounds=[min(p[0] for p in anchors)-margin,min(p[1] for p in anchors)-margin,max(p[0] for p in anchors)+margin,max(p[1] for p in anchors)+margin]
            index=scoped_geometry_index(self.w,self.sid,args.layout_id,bounds,args.unit_override or self.drawing['unit']);records=index.query(args.layout_id,bounds)
            if len(records)>6000:raise ValueError('配对范围几何过密，请先缩小到相邻空间。')
            descriptors=source_descriptors(self.w,self.sid,records)
            for rec in records:rec['effective_layer']=descriptors['effective_layers'][rec['entity_id']]
            texts=[t for t in reading.scene['texts'] if t['layout_id']==args.layout_id]
            evidence=compute(self.w,'connection_evidence',dict(records=records,arc_sources=descriptors["arc_sources"],factor=factor,anchors=anchors,bounds=bounds,texts=texts))
            # Include the complete observed separating segment when bounded;
            # a narrow label crop cannot establish absence of an opening.
            points=[p for b in evidence['barriers'] for p in b['full_points']]+anchors
            expanded=[min(p[0] for p in points)-margin,min(p[1] for p in points)-margin,max(p[0] for p in points)+margin,max(p[1] for p in points)+margin]
            if evidence['barriers'] and max(expanded[2]-expanded[0],expanded[3]-expanded[1])*factor<=30 and expanded!=bounds:
                bounds=expanded
                index=scoped_geometry_index(self.w,self.sid,args.layout_id,bounds,args.unit_override or self.drawing['unit']);records=index.query(args.layout_id,bounds)
                if len(records)>6000:raise ValueError('完整相接边界图元过密，需要分段核查。')
                descriptors=source_descriptors(self.w,self.sid,records)
                for rec in records:rec['effective_layer']=descriptors['effective_layers'][rec['entity_id']]
                evidence=compute(self.w,'connection_evidence',dict(records=records,arc_sources=descriptors["arc_sources"],factor=factor,anchors=anchors,bounds=bounds,texts=texts))
            dims=read_dimensions(self.w,self.sid,args.layout_id,bounds,args.unit_override or self.drawing['unit'])
            for o in evidence['openings']:
                o['dimension_checks']=[]
                for d in dims['dimensions']:
                    pts=d.get('definition_points',[])
                    if len(pts)<2:continue
                    # Only actual two endpoints, not a nearby numeric coincidence.
                    endpoints=o['measurement']['measurement_line']
                    if min(max(math.dist(pts[k][:2],endpoints[k]) for k in (0,1)),max(math.dist(pts[k][:2],endpoints[1-k]) for k in (0,1)))*factor<=.03:
                        o['dimension_checks'].append({k:d[k] for k in ('object_id','entity_id','rendered_texts','geometry_recomputed','definition_points') if k in d})
            iid=stable_id('pair',rid,*ids)
            overlays=[('B'+str(i+1),[b['points']]) for i,b in enumerate(evidence['barriers'])]
            overlays += [('O'+str(i+1),[j['points'] for j in o['measurement']['jambs']]) for i,o in enumerate(evidence['openings'])]
            for i,o in enumerate(evidence['openings']):o['display_label']='O'+str(i+1)
            rows.append(dict(item_id=iid,number=n,display_label='Pair '+str(n),crop_bbox=bounds,
                source_region_id=ids[0],target_region_id=ids[1],labels=[r.text for r in labels],
                geometry_complete=index.data['complete'],**evidence,overlays=overlays))
        used={v for p in args.pairs for v in (p.source_region_id,p.target_region_id)}
        if len(set(args.excluded_region_ids))!=len(args.excluded_region_ids) or set(args.excluded_region_ids)&used:raise ValueError('待排除名称不能重复或同时用于配对。')
        for label_id in args.excluded_region_ids:
            label=reading.by_id.get(label_id)
            if label is None or label.layout_id!=args.layout_id:raise ValueError('待排除名称不属于本布局。')
            a,b,c,d=label.bbox;h=max(d-b,.1/factor);margin=h*5
            rows.append(dict(item_id=stable_id('excluded',rid,label_id),number=len(rows)+1,display_label='Excluded label '+str(len(rows)+1),
                role='unpaired_label',crop_bbox=[a-margin,b-margin,c+margin,d+margin],source_region_id=label_id,target_region_id='',labels=[label.text],
                geometry_complete=False,barriers=[],crossing_entities=[],openings=[],probe=[],diagnostics=[],overlays=[]))
        bounds=[min(r['crop_bbox'][0] for r in rows),min(r['crop_bbox'][1] for r in rows),max(r['crop_bbox'][2] for r in rows),max(r['crop_bbox'][3] for r in rows)]
        r=dict(review_id=rid,measurement_id=rid,snapshot_id=self.sid,source_sha256=self.drawing['sha256'],kind='connection_review',algorithm_version=VERSION,
            layout_id=args.layout_id,scope_bbox=bounds,applied_unit=args.unit_override or self.drawing['unit'],request=args.model_dump(),items=rows,status='awaiting_visual_review',
            evidence_url=f'/api/drawings/{self.sid}/measurements/{rid}')
        r['images']=sheets(self.w,self.sid,r);self.w.store.put('measurement',rid,r);return r
    def review(self,args,sent):
        r=deepcopy(self.get(args.review_id))
        if len({c.item_id for c in args.choices})!=len(args.choices) or {c.item_id for c in args.choices}!={i['item_id'] for i in r['items']}:raise ValueError('必须逐处判断全部配对，不能遗漏。')
        if not {i['image_id'] for i in r['images']}<=set(sent):raise ValueError('必须先实际读取本轮审查图像。')
        for image in r['images']:VisionService(self.w,self.sid).png(image['image_id'])
        choices={c.item_id:c for c in args.choices};paths=[];object_ids=[]
        for row in r['items']:
            c=choices[row['item_id']]
            if row.get('role')=='unpaired_label' and c.classification not in ('not_plan_space','uncertain'):raise ValueError('单个待排除名称只能确认非平面空间或保留未知。')
            os={o['opening_id']:o for o in row['openings']}
            if len(set(c.opening_ids))!=len(c.opening_ids) or not set(c.opening_ids)<=os.keys():raise ValueError('开口不属于该配对或重复。')
            if row['render_status']!='available' and c.classification!='uncertain':raise ValueError('图像不可用，只能 uncertain。')
            if c.classification=='door' and (not c.opening_ids or any(not os[i]['opposite_anchor_sides'] or not os[i]['visual_door_eligible'] or not os[i]['access_legs_clear'] or os[i].get('operation_type')=='conflict' for i in c.opening_ids)):raise ValueError('门连接需要双方在门两侧、源门符号和两条实测门垛。')
            if c.classification=='open' and (row['barriers'] or any(o['access_legs_clear'] and o['opposite_anchor_sides'] and o.get('operation_type') in ('sliding','single_swing','double_swing') for o in row['openings'])):raise ValueError('名称之间存在实际横穿墙线，不能直接认定敞开。')
            if c.classification=='wall' and not row['barriers']:raise ValueError('缺少实测横穿边线，不能认定隔墙。')
            if c.classification not in ('door','uncertain') and c.opening_ids:raise ValueError('非门判断不能附加门洞测值。')
            row['decision']=c.model_dump();row['status']='unresolved' if c.classification=='uncertain' else 'reviewed_spatial_candidate'
            row['selected_measurements']=[os[i]['measurement']|{'opening_id':i} for i in c.opening_ids]
            row['selected_door_count']=len(c.opening_ids) if c.classification=='door' else 0 if c.classification=='wall' else None
            row['minimum_jamb_width_m']=min((m['value'] for m in row['selected_measurements']),default=None)
            for i in c.opening_ids:
                o=os[i];paths.extend(j['points'] for j in o['measurement']['jambs']);paths.append(o['measurement']['measurement_line']);object_ids.extend(j['entity_id'] for j in o['measurement']['jambs'])
            if c.classification=='wall':paths.extend(b['points'] for b in row['barriers']);object_ids.extend(b['entity_id'] for b in row['barriers'])
            if c.classification=='open':paths.append(row['probe']) # Explicitly a tested cross-space segment, not a wall.
        r['parent_review_id']=args.review_id;r['review_id']=r['measurement_id']=stable_id('measure',args.review_id,json.dumps(args.model_dump(),sort_keys=True));r['status']='reviewed_candidates'
        r['evidence_url']=f'/api/drawings/{self.sid}/measurements/{r["review_id"]}'
        r['highlight_action']=dict(mode='replace',kind='geometry',snapshot_id=self.sid,scope=dict(layout_id=r['layout_id'],bbox=r['scope_bbox']),paths=paths,regions=[],object_ids=sorted(set(object_ids)))
        complete=all(i['status']!='unresolved' for i in r['items'])
        measured=any(i['selected_measurements'] for i in r['items'])
        r['operation_receipts']=[dict(operation=o,fulfilled=f) for o,f in [('relations',complete),('vision',True),('geometry',measured or bool(paths)),('highlight',bool(paths)),('units',True),('dimensions',any(o['dimension_checks'] for i in r['items'] for o in i['openings'])),('exclusions',complete and any(i['decision']['classification']=='not_plan_space' for i in r['items']))]]
        self.w.store.put('measurement',r['review_id'],r);return r
    @staticmethod
    def summary(r):
        out={k:v for k,v in r.items() if k not in ('items','images')}
        out['items']=[]
        for item in r['items']:
            row={k:v for k,v in item.items() if k not in ('overlays',)}
            row['openings']=[{k:v for k,v in o.items() if k in ('opening_id','display_label','faces','measurement','opposite_anchor_sides','opening_type','operation_type','access_legs_clear','visual_door_eligible','dimension_checks','diagnostics')} for o in item['openings']]
            out['items'].append(row)
        out['image_ids']=[i['image_id'] for i in r['images']]
        out['images']=[{k:v for k,v in i.items() if k in ('image_id','image_url','cells')} for i in r['images']]
        out['note']='逐处模型看图解释；墙线/门垛坐标及宽度由源几何计算。open 的高亮是查验连线；wall 为横穿边线；door 为实际门垛和测距线。名称连线不证明整室通行，门垛净距不冒充完成面通行净宽。'
        return out
