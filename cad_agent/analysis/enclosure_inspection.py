"""Inspect internal closed objects without classifying every loop as a void."""
from pydantic import Field
from shapely.geometry import Polygon, box
from cad_agent.contracts import Record, stable_id
from cad_agent.geometry.source import scoped_geometry_index, UNIT_TO_M

class EnclosureInspection(Record):
    batch_id: str
    number: int = Field(ge=1)
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=8,ge=1,le=20)


def inspect(session,args):
    b=session.room_batches.get(args.batch_id)
    if not b:raise ValueError('先读取房间结果集合。')
    item=next((i for i in b['items'] if i['number']==args.number),None)
    if not item or item['highlight']['kind']!='candidate_boundary':raise ValueError('此编号没有已核查的候选轮廓。')
    poly=Polygon(item['highlight']['boundary'])
    idx=scoped_geometry_index(session.w,session.request.snapshot_id,b['scope']['layout_id'],list(poly.bounds),b['unit'])
    records=idx.query(b['scope']['layout_id'],list(poly.bounds),'intersects')
    from cad_agent.geometry.compute import compute
    result=compute(session.w,'enclosure_inspection',dict(boundary=item['highlight']['boundary'],records=records,
        factor=UNIT_TO_M.get(b['unit']),batch_id=args.batch_id,layout_id=b['scope']['layout_id'],
        regions=[dict(region_id=r.region_id,text=r.text,bbox=r.bbox,layout_id=r.layout_id)
                 for r in session.index.regions if r.layout_id==b['scope']['layout_id']]))
    loops,texts=result['loops'],result['texts']
    return dict(batch_id=args.batch_id,number=args.number,scope_bbox=list(poly.bounds),layout_id=b['scope']['layout_id'],
        items=loops[args.offset:args.offset+args.limit],total=len(loops),has_more=args.offset+args.limit<len(loops),
        nearby_text_candidates=texts[:20],text_candidates_truncated=len(texts)>20,
        geometry_complete=idx.data.get('complete',False),scanned_entity_count=len(records),
        exclusions=[],post_exclusion_area_m2=None,
        operation_receipts=[dict(operation='exclusions',fulfilled=False)],
        note='已检查当前轮廓内部源闭合图元；分段线围合、图像符号和未支持图元可能漏检。闭合对象用途未确定，未扣除，不将零候选解释为没有洞口。请依据局部图、标注和对象关联核查用途。')


def compute_inspection(payload):
    poly=Polygon(payload['boundary']);records=payload['records']
    loops=[];seen=set();factor=payload['factor']
    for rec in records:
        for path in rec['paths']:
            if len(path)<4 or path[0]!=path[-1]:continue
            p=Polygon(path)
            if not p.is_valid or p.area<=0 or not poly.contains(p) or p.equals(poly):continue
            key=p.normalize().wkb_hex
            if key in seen:continue
            seen.add(key)
            loops.append(dict(object_id=stable_id('internal',payload['batch_id'],rec['entity_id'],key),entity_ids=[rec['entity_id']],
                kind=rec.get('kind'),layer=rec.get('effective_layer'),bbox=list(p.bounds),
                candidate_area_m2=round(p.area*factor**2,6) if factor else None,
                semantic_status='unclassified_closed_object',deducted=False))
    texts=[]
    for r in payload['regions']:
        if r['layout_id']==payload['layout_id'] and box(*r['bbox']).intersects(poly):
            texts.append(dict(region_id=r['region_id'],text=r['text'],bbox=r['bbox']))
    return dict(loops=loops,texts=texts)
