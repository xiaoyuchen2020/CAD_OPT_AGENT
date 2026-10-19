"""Unitless, source-bound boundary hypotheses, visual selection and validation.

This bounded MVP supports rectilinear wall envelopes with closed wall returns.
It does not infer arbitrary polygons, physical units or confirmed net areas.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Literal
from pydantic import Field
from shapely.geometry import Point, Polygon, LineString, box
from shapely.ops import unary_union, polygonize
from cad_agent.contracts import Record, stable_id
from cad_agent.geometry.source import scoped_geometry_index, source_descriptors
from cad_agent.geometry.room_batch import RoomBatchService
from cad_agent.geometry.vision import VisionService, MAX_IMAGE_BYTES
from cad_agent.cad.render import render_svg

VERSION = 'room-boundary-visual-v1.4'
WALL = re.compile(r'墙|WALL|柱|COLUMN|(?:^|[-_])COL(?:$|[-_])', re.I)
FURNITURE = re.compile(r'家具|FURN|BED|床', re.I)


def review_crop(row):
    """Fit every hypothesis and its label, keeping context without a whole floor."""
    boxes=[row['label_evidence']['bbox']]+[c['bbox'] for c in row['candidates']]
    a=min(b[0] for b in boxes);b=min(b[1] for b in boxes)
    c=max(b[2] for b in boxes);d=max(b[3] for b in boxes)
    h=row['label_evidence']['bbox'][3]-row['label_evidence']['bbox'][1]
    side=max(c-a,d-b,h*8)*1.3
    x=(a+c)/2;y=(b+d)/2
    return [x-side/2,y-side/2,x+side/2,y+side/2]


class PrepareBoundaries(Record):
    batch_id: str


class BoundaryChoice(Record):
    region_id: str
    candidate_id: str = ''
    decision: Literal['select', 'uncertain']
    reason: str = Field(min_length=8, max_length=240)


class SelectBoundaries(Record):
    review_id: str
    choices: list[BoundaryChoice] = Field(min_length=1, max_length=12)


def center(item):
    a,b,c,d = item['label_evidence']['bbox']
    return Point((a+c)/2, (b+d)/2)


def generate_candidates(records, item, labels, scope):
    """Pure, unitless operation. Unsupported topology returns a gap, not a guess."""
    seed = center(item)
    h = max(item['label_evidence']['bbox'][3]-item['label_evidence']['bbox'][1], 1e-8)
    tol = h / 1e6  # Numerical matching tolerance, not an assumed engineering unit.
    wall = [r for r in records if r.get('linear') and WALL.search(r['effective_layer']) and not FURNITURE.search(r['effective_layer'])]
    # Layer names are hints, not a required room vocabulary. Unknown layers
    # contribute geometric hypotheses that still require visual selection.
    if not wall:
        wall=[r for r in records if r.get('linear') and not FURNITURE.search(r['effective_layer'])]
    segments = [(a,b,r) for r in wall for p in r['paths'] for a,b in zip(p,p[1:]) if math.dist(a,b)>tol]
    result = []
    def candidate(poly, kind, sources=None):
        poly=poly.simplify(tol,preserve_topology=True)
        if poly.geom_type!='Polygon' or not poly.is_valid or not poly.contains(seed) or len(poly.exterior.coords)>128:
            return
        boundary = [list(p) for p in poly.exterior.coords]
        edges = []
        for a,b in zip(boundary,boundary[1:]):
            line = LineString([a,b]); parts=[]; ids=[]; layers=[]
            for c,d,r in segments:
                # Collinearity (not perpendicular intersections) supports a face.
                cross=lambda p:abs((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))/line.length
                if max(cross(c),cross(d))>tol:continue
                part=line.intersection(LineString([c,d]).buffer(tol,cap_style=2))
                if part.length>tol*10:parts.append(part);ids.append(r['entity_id']);layers.append(r['effective_layer'])
            union=unary_union(parts)
            missing=line.difference(union.buffer(tol))
            gaps=[list(map(list,g.coords)) for g in getattr(missing,'geoms',[missing]) if g.geom_type=='LineString' and g.length>tol*10]
            edges.append(dict(points=[a,b],observed_fraction=min(1.,round(union.length/line.length,6)),
                source_entity_ids=sorted(set(ids)),source_layers=sorted(set(layers)),unclosed_gaps=gaps))
        own=[x['region_id'] for x in labels if poly.contains(center(x))]
        checks=dict(valid_polygon=True, contains_own_label=True, within_scope=box(*scope).buffer(tol).covers(poly),
            single_batch_label=own==[item['region_id']], wall_supported=all(e['source_entity_ids'] for e in edges),
            furniture_excluded=kind!='furniture_loop', no_interior_rings=not poly.interiors,
            observed_perimeter=sum(LineString(e['points']).length*e['observed_fraction'] for e in edges)/poly.length>=.5)
        cid=stable_id('outline',item['region_id'],kind,json.dumps(boundary))
        result.append(dict(candidate_id=cid,kind=kind,boundary=boundary,bbox=list(poly.bounds),edge_evidence=edges,
            source_entity_ids=sorted(set(sources or [])|{v for e in edges for v in e['source_entity_ids']}),
            checks=checks, eligible=all(checks.values()), area_m2=None,
            issues=['opening_closure_unconfirmed','finish_face_unconfirmed'] if any(e['unclosed_gaps'] for e in edges) else ['finish_face_unconfirmed']))
    # Closed wall faces are preferable when they exist, but still need semantic review.
    lines=[LineString([a,b]) for a,b,_ in segments]
    if lines:
        for p in sorted(polygonize(unary_union(lines)),key=lambda p:p.area):
            if p.contains(seed):candidate(p,'closed_wall_face')
            if len(result)>=2:break
    # Bounded rectilinear hypothesis. No rotated/curved fallback or invented vertices.
    vertical=[(a,b) for a,b,_ in segments if abs(a[0]-b[0])<tol and min(a[1],b[1])<seed.y<max(a[1],b[1])]
    lefts=[a[0] for a,b in vertical if a[0]<seed.x];rights=[a[0] for a,b in vertical if a[0]>seed.x]
    if lefts and rights:
        left,right=max(lefts),min(rights); levels={}
        for a,b,_ in segments:
            if abs(a[1]-b[1])>tol:continue
            lo=max(left,min(a[0],b[0]));hi=min(right,max(a[0],b[0]))
            if hi<=lo:continue
            levels.setdefault(round(a[1]/tol),{'y':a[1],'spans':[]})['spans'].append(LineString([(lo,0),(hi,0)]))
        ys=[v['y'] for v in levels.values() if unary_union(v['spans']).length/(right-left)>=.2]
        below=[y for y in ys if y<seed.y];above=[y for y in ys if y>seed.y]
        if below and above:
            poly=box(left,max(below),right,min(above));intrusions=[]
            for r in wall:
                for path in r['paths']:
                    if len(path)<4 or path[0]!=path[-1]:continue
                    shape=Polygon(path)
                    if shape.is_valid and poly.intersection(shape).area>tol*tol:
                        intrusions.append(r['entity_id']);poly=poly.difference(shape)
            if not any(Polygon(c['boundary']).equals(poly) for c in result):candidate(poly,'rectilinear_wall_envelope',intrusions)
    furniture=[r for r in records if r.get('linear') and FURNITURE.search(r['effective_layer'])]
    furniture_lines=[LineString(p) for r in furniture for p in r['paths'] if len(p)>1]
    if furniture_lines:
        for p in sorted(polygonize(unary_union(furniture_lines)),key=lambda p:p.area):
            if p.contains(seed):
                candidate(Polygon(p.exterior),'furniture_loop',[r['entity_id'] for r in furniture if box(*r['bbox']).intersects(p)])
                break
    return result[:3]


class RoomBoundaryService:
    def __init__(self,w,sid):
        self.w,self.sid=w,sid
        self.drawing=w.store.get('drawing',sid)
        self.batches=RoomBatchService(w,sid)

    def get(self,rid):
        record=self.w.store.get('room_boundary_review',rid)
        if record['snapshot_id']!=self.sid or record['source_sha256']!=self.drawing['sha256']:raise KeyError(rid)
        return record

    def prepare(self,args):
        batch=self.batches.get(args.batch_id)
        if not batch['scope'] or not batch['items']:raise ValueError('需要先定位图框并枚举房间名称。')
        rid=stable_id('boundary',self.sid,self.drawing['sha256'],VERSION,args.batch_id)
        try:return self.get(rid)
        except KeyError:pass
        scope=batch['scope'];bounds=scope['bbox'];lid=scope['layout_id']
        index=scoped_geometry_index(self.w,self.sid,lid,bounds,'unknown')
        rows=[];anchors=batch.get('label_anchors',batch['items'])
        for item in batch['items']:
            p=center(item);h=item['label_evidence']['bbox'][3]-item['label_evidence']['bbox'][1]
            neighbors=[p.distance(center(other)) for other in anchors if other['region_id']!=item['region_id'] and p.distance(center(other))>h]
            radius=min(min(neighbors)*.9 if neighbors else h*24,h*32)
            crop=[max(bounds[0],p.x-radius),max(bounds[1],p.y-radius),min(bounds[2],p.x+radius),min(bounds[3],p.y+radius)]
            records=index.query(lid,crop)
            # Dense annotations must not prevent all rows from being reviewed.
            # Requery a smaller source region; do not silently truncate geometry.
            attempts=[dict(bbox=crop,count=len(records))]
            for _ in range(3):
                if len(records)<=4000:break
                radius*=.65
                crop=[max(bounds[0],p.x-radius),max(bounds[1],p.y-radius),min(bounds[2],p.x+radius),min(bounds[3],p.y+radius)]
                records=index.query(lid,crop)
                attempts.append(dict(bbox=crop,count=len(records)))
            complete=index.data['complete'] and len(records)<=4000
            bounded=len(records)<=4000
            records=records[:4000]
            if bounded:
                descriptors=source_descriptors(self.w,self.sid,records)
                records=[dict(r,effective_layer=descriptors['effective_layers'][r['entity_id']]) for r in records]
            from cad_agent.geometry.compute import compute
            candidates=compute(self.w,'boundary_candidates',dict(records=records,item=item,labels=anchors,scope=bounds)) if bounded else []
            for n,c in enumerate(candidates):c['display_label']=chr(65+n)
            rows.append(dict(number=item['number'],region_id=item['region_id'],label_evidence=item['label_evidence'],
                crop_bbox=crop,candidates=candidates,geometry_complete=complete,
                geometry_search_attempts=attempts,
                status='awaiting_visual_selection' if candidates else 'unresolved',
                issues=([] if complete else ['partial_source_geometry'])+([] if candidates else ['no_supported_boundary_or_geometry_budget'])))
        record=dict(review_id=rid,version=VERSION,snapshot_id=self.sid,source_sha256=self.drawing['sha256'],
            batch_id=args.batch_id,layout_id=lid,scope_bbox=bounds,items=rows,status='awaiting_visual_selection',
            unit=batch['unit'],area_m2=None,identity_confirmed=False,
            source_geometry_diagnostics=index.data.get('diagnostics',[])[:24],
            source_geometry_complete=index.data['complete'],
            evidence_url=f'/api/drawings/{self.sid}/boundary-reviews/{rid}',images=[])
        record['images']=self.render_sheets(record)
        self.w.store.put('room_boundary_review',rid,record)
        return record

    def render_sheets(self,record):
        """Two sheets maximum; each cell has its own source bbox and affine map."""
        import resvg_py
        render=self.w.rendering(self.sid);vision=VisionService(self.w,self.sid);images=[]
        for start in range(0,len(record['items']),6):
            rows=record['items'][start:start+6];width=min(len(rows),3)*512;height=math.ceil(len(rows)/3)*550
            cells=[];svgparts=[]
            for n,row in enumerate(rows):
                x=(n%3)*512;y=(n//3)*550+38
                bounds=review_crop(row);a,b,c,d=bounds;scale=min(512/(c-a),512/(d-b))
                # Square crops, with explicit independent maps per cell.
                c=a+512/scale;d=b+512/scale;bounds=[a,b,c,d]
                items=[i for i in render['items'] if i['layout_id']==record['layout_id'] and i['bbox'][0]<=c and i['bbox'][2]>=a and i['bbox'][1]<=d and i['bbox'][3]>=b]
                available=bool(items) and len(items)<=12000 and len(json.dumps(items))<=4_000_000
                row['render_status']='available' if available else 'unavailable'
                row['render_item_count']=len(items)
                if not available:row['issues'].append('local_image_unavailable')
                svg=render_svg(render|{'items':items if available else []},record['layout_id'],bounds,width=512,height=512,raster=True)
                overlays=[];mapping=[]
                for candidate in row['candidates']:
                    if not available:continue
                    points=candidate['boundary'];label=candidate['display_label'];color={'A':'#0077cc','B':'#b45309','C':'#9333ea'}[label]
                    path='M '+' L '.join(f'{px},{-py}' for px,py in points)
                    overlays.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{2/scale}"/>')
                    for edge in candidate['edge_evidence']:
                        for gap in (edge['unclosed_gaps'] if candidate['kind']!='furniture_loop' else []):
                            path='M '+' L '.join(f'{px},{-py}' for px,py in gap)
                            overlays.append(f'<path d="{path}" fill="none" stroke="#ef4444" stroke-width="{3/scale}" stroke-dasharray="{6/scale} {4/scale}"/>')
                    px,py=candidate['bbox'][0],candidate['bbox'][3]
                    overlays.append(f'<text x="{px}" y="{-py}" font-size="{18/scale}" fill="{color}" stroke="white" stroke-width="{3/scale}" paint-order="stroke">{label}</text>')
                    mapping.append(dict(candidate_id=candidate['candidate_id'],display_label=label))
                svg=svg.replace('</svg>',''.join(overlays)+'</svg>')
                svg=svg.replace('<svg ',f'<svg x="{x}" y="{y}" ',1)
                svgparts.append(f'<text x="{x+12}" y="{y-12}" font-size="22">Room candidate {row["number"]} (A/B/C overlays)</text>'+svg)
                if not available:svgparts.append(f'<text x="{x+12}" y="{y+40}" font-size="20">Source image unavailable; choose uncertain</text>')
                cells.append(dict(number=row['number'],region_id=row['region_id'],bbox=bounds,pixel_rect=[x,y,x+512,y+512],
                    wcs_to_pixel=[scale,0,0,-scale,x-a*scale,y+d*scale],
                    pixel_to_wcs=[1/scale,0,0,-1/scale,a-x/scale,d+y/scale],candidates=mapping,render_status=row['render_status']))
            svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="100%" height="100%" fill="white"/>'+''.join(svgparts)+'</svg>'
            raw=resvg_py.svg_to_bytes(svg_string=svg)
            if len(raw)>MAX_IMAGE_BYTES:raise ValueError('边界图像超过传输预算。')
            sha=hashlib.sha256(raw).hexdigest();iid=stable_id('image',record['review_id'],sha)
            path=vision.path(iid);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            url=f'/api/drawings/{self.sid}/images/{iid}'
            image=dict(image_id=iid,snapshot_id=self.sid,source_sha256=self.drawing['sha256'],image_sha256=sha,
                width=width,height=height,byte_count=len(raw),mime_type='image/png',layout_id=record['layout_id'],bbox=record['scope_bbox'],
                kind='boundary_contact_sheet',review_id=record['review_id'],cells=cells,render_truncated=any(c['render_status']=='unavailable' for c in cells),
                coordinate_space='per_cell_dxf_wcs',image_url=url+'/image.png',evidence_url=url,
                note='A/B/C 是候选叠加，红色虚线是未观察到闭合墙线的段；每格是独立原图局部，不能跨格测量。')
            self.w.store.put('visual_image',iid,image);images.append(image)
        return images

    def select(self,args,sent_image_ids):
        original=self.get(args.review_id)
        if len({c.region_id for c in args.choices})!=len(args.choices) or {c.region_id for c in args.choices}!={i['region_id'] for i in original['items']}:
            raise ValueError('必须逐项选择或说明 uncertain，不可遗漏、重复或加入其他房间。')
        vision=VisionService(self.w,self.sid)
        if not {i['image_id'] for i in original['images']}<=set(sent_image_ids):raise ValueError('边界图像尚未发送给当前模型。')
        for image in original['images']:vision.png(image['image_id'])
        record=deepcopy(original);record['parent_review_id']=args.review_id
        record['review_id']=stable_id('boundary',args.review_id,json.dumps(args.model_dump(),sort_keys=True))
        record['evidence_url']=f'/api/drawings/{self.sid}/boundary-reviews/{record["review_id"]}'
        choices={c.region_id:c for c in args.choices};selected=[]
        for row in record['items']:
            choice=choices[row['region_id']];row['model_choice']=choice.model_dump()
            candidate=next((c for c in row['candidates'] if c['candidate_id']==choice.candidate_id),None)
            if choice.decision=='select' and candidate is None:raise ValueError('所选轮廓不属于该名称对应的图像候选。')
            row.update(selected_candidate_id=None,status='unresolved')
            if choice.decision=='select':
                poly=Polygon(candidate['boundary'])
                valid=row.get('render_status','available')=='available' and poly.is_valid and poly.contains(center(row)) and box(*record['scope_bbox']).covers(poly) and candidate['eligible']
                row['geometry_validation']=dict(candidate['checks'],valid_polygon=poly.is_valid,passed=valid)
                if valid:
                    row.update(selected_candidate_id=candidate['candidate_id'],status='visual_boundary_candidate')
                    selected.append((row,poly))
                else:row['issues'].append('selection_rejected_by_geometry_or_furniture_filter')
        # A batch cannot silently assign overlapping room interiors to two labels.
        for n,(row,poly) in enumerate(selected):
            for other,p in selected[n+1:]:
                if poly.intersection(p).area>min(poly.area,p.area)*1e-6:
                    for r in (row,other):r.update(selected_candidate_id=None,status='unresolved');r['issues'].append('overlapping_room_candidates')
        record['status']='reviewed_candidates';record['area_m2']=None
        batch=deepcopy(self.batches.get(original['batch_id']));oldbid=batch['batch_id']
        batch.update(batch_id=stable_id('rooms',oldbid,record['review_id']),parent_batch_id=oldbid,boundary_review_id=record['review_id'])
        batch['evidence_url']=f'/api/drawings/{self.sid}/room-batches/{batch["batch_id"]}'
        batch['coverage']['highlight']='visual_boundary_candidates_or_unresolved'
        batch['coverage']['boundary_review']='completed_with_uncertainty'
        for item,row in zip(batch['items'],record['items']):
            item.update(area_m2=None,candidate_area_m2=None,measurement_id=None,measurement_evidence_url=None,
                status='needs_unit_confirmation' if batch['unit']=='unknown' else 'needs_review',
                boundary_status=row['status'],boundary_evidence_url=record['evidence_url'],identity_status='visual_candidate_unverified',
                diagnostics=row['issues']+(['unknown_units'] if batch['unit']=='unknown' else []))
            item['highlight']=dict(kind='label_locator',layout_id=record['layout_id'],bbox=item['label_evidence']['bbox'])
            if row['selected_candidate_id']:
                c=next(c for c in row['candidates'] if c['candidate_id']==row['selected_candidate_id'])
                item['diagnostics']+=c['issues']
                item['highlight']=dict(kind='candidate_boundary',layout_id=record['layout_id'],bbox=c['bbox'],boundary=c['boundary'],holes=[],
                    gaps=[g for e in c['edge_evidence'] for g in e['unclosed_gaps']])
        record['result_batch_id']=batch['batch_id']
        self.batches.measure_selected(batch)
        self.w.store.put('room_boundary_review',record['review_id'],record)
        self.w.store.put('room_batch',batch['batch_id'],batch)
        return record,batch

    @staticmethod
    def summary(record):
        return {k:record[k] for k in ('review_id','batch_id','status','unit','area_m2','evidence_url')}|{
            'image_ids':[i['image_id'] for i in record['images']],
            'items':[dict(number=i['number'],region_id=i['region_id'],status=i['status'],issues=i['issues'],
                selected_candidate_id=i.get('selected_candidate_id'),model_choice=i.get('model_choice'),
                candidates=[dict(candidate_id=c['candidate_id'],display_label=c['display_label'],kind=c['kind'],eligible=c['eligible'],
                    gap_count=sum(len(e['unclosed_gaps']) for e in c['edge_evidence']),issues=c['issues']) for c in i['candidates'] if record['status']=='awaiting_visual_selection' or c['candidate_id']==i.get('selected_candidate_id')]) for i in record['items']],
            'instruction':'逐格看图：A/B/C是候选叠加，红虚线为开口闭合假设。选择表示轮廓可作为带条件的围合候选，不表示确认净边界。墙线吻合且未跨入其他房间时，可select并保留开口/完成面缺口；几何错配、家具/表格、跨房间或原图不可用时uncertain。每项说明理由，面积只能引用选择后返回的测量值及口径。'}


def compute_boundary_candidates(payload):
    """Fixed worker operation; input/output are source-bound JSON records."""
    return generate_candidates(payload['records'],payload['item'],payload['labels'],payload['scope'])
