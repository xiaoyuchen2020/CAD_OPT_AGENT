"""Detail keys from source text and underlined, numbered title markers.

Reuse native geometry, the reading index, table cells and closed-border context.
A key match is never a confirmation of a construction detail's applicability.
"""
import math
import re
from shapely.geometry import Point

from cad_agent.cad.reader import union_box
from cad_agent.components.context import build_context
from cad_agent.geometry.source import native_source
from .annotations import Nearby

TRACE_VERSION = 'detail-trace-v1.4'
KEY = re.compile(r'[A-Za-z0-9]{1,8}')
SCALE = re.compile(r'1\s*[:：]\s*\d+(?:\.\d+)?')


def detail_catalog(index):
    texts=index.scene['texts']
    native=native_source(index.w,index.snapshot)
    targets=[];borders=[];sheets=[];unscaled_markers=[]
    for layout in index.drawing['layouts']:
        lid=layout['layout_id'];ts=[t for t in texts if t['layout_id']==lid]
        tidx=Nearby(ts,lambda t:Point(t['origin'][:2]))
        frames=[f for f in index.drawing['frames'] if f['layout_id']==lid and f['method']!='layout_fallback']
        context=build_context(index,lid,frames)
        for f in context['frames']:
            if f.get('boundary_basis')=='layout_fallback' or f.get('kind')=='titleblock_fields':continue
            borders.append(dict(id=f['object_id'],layout_id=lid,bbox=f['bbox'],entity_ids=f.get('entity_ids',[])))
        segments=[]
        for line in index.scene['lines']:
            if line['layout_id']!=lid:continue
            for a,b in zip(line['points'],line['points'][1:]):
                if abs(a[1]-b[1])<.01 and abs(a[0]-b[0])>.01:
                    segments.append(dict(y=(a[1]+b[1])/2,left=min(a[0],b[0]),right=max(a[0],b[0]),entity_id=line['entity_id']))
        for eid,(e,path,elid,bounds) in native['entities'].items():
            if elid!=lid:continue
            if e.dxftype()=='CIRCLE':
                x,y=list(e.dxf.center)[:2];r=e.dxf.radius
            elif e.dxftype()=='LWPOLYLINE' and e.closed:
                pts=list(e.get_points('xyb'))
                # Two equal semicircular bulges are an exact native circle;
                # arbitrary closed polylines and nonuniform ellipses are not.
                if len(pts)!=2 or abs(abs(pts[0][2])-1)>1e-8 or abs(pts[0][2]-pts[1][2])>1e-8:continue
                x=(pts[0][0]+pts[1][0])/2;y=(pts[0][1]+pts[1][1])/2;r=math.dist(pts[0][:2],pts[1][:2])/2
            else:continue
            if r<=0:continue
            inside=[t for t in tidx.query([x-1.4*r,y-1.4*r,x+1.4*r,y+1.4*r]) if math.dist(t['origin'][:2],(x,y))<=1.4*r]
            if len(inside)!=1:continue
            label=inside[0];key=label['text'].strip()
            if not KEY.fullmatch(key):continue
            marker_start=len(targets)
            for t in tidx.query([x-22*r,y-2*r,x+22*r,y+3*r]):
                title=t['text'].strip();h=t['height'];tx,ty=t['origin'][:2]
                if not 2<=len(title)<=55 or not re.search('[\u3400-\u9fff]',title) or re.search('详见|参见|另见|图纸|目录|[；;。]',title):continue
                if abs((t.get('rotation',0)+180)%360-180)>.1 or not .2*r<=h<=2*r:continue
                # Underline starts with the title, is below its baseline, and
                # stays within the title width. Two rails or explicit scale
                # distinguish a detail heading from a nearby room/axis label.
                lines=[l for l in segments if abs(l['left']-tx)<.7*h and -.2*h<=ty-l['y']<=1.5*h and
                       2*h<l['right']-l['left']<=max(t['bbox'][2]-t['bbox'][0],4*h)*1.4]
                scales=[u for u in tidx.query([t['bbox'][0]-h,ty-2*h,t['bbox'][2]+5*h,ty+2*h]) if SCALE.fullmatch(u['text'].strip())]
                double=any(abs(a['left']-b['left'])<.1*h and abs(a['right']-b['right'])<.1*h and .05*h<abs(a['y']-b['y'])<.6*h for a in lines for b in lines)
                if not lines or not (double or SCALE.search(title) or scales):continue
                # Heading must be adjacent to the marker, not across another
                # figure. Retain competing headings instead of picking nearest.
                if tx>=x:
                    if tx-(x+r)>3*r or abs(ty-y)>1.6*r:continue
                elif t['bbox'][2]<x and x-r-t['bbox'][2]>3*r:continue
                elif tx<x and abs(ty-y)>1.6*r:continue
                tids=[label['entity_id'],t['entity_id']]+[u['entity_id'] for u in scales]
                targets.append(dict(detail_number=key,title=title,layout_id=lid,entity_id=t['entity_id'],
                    text_entity_ids=list(dict.fromkeys(tids)),support_entity_ids=[eid]+sorted({l['entity_id'] for l in lines}),
                    bbox=union_box([[x-r,y-r,x+r,y+r],t['bbox']]),method='circle_underlined_title',marker_center=[x,y]))
            if len(targets)==marker_start:
                # Some exploded drawings retain only a circle key and a scale;
                # their heading is outline geometry. Report a marker target and
                # leave its title unknown instead of inventing the missing name.
                scales=[u for u in tidx.query([x,y-1.6*r,x+6*r,y+3*r]) if
                        SCALE.fullmatch(u['text'].strip()) and .3*r<=u['height']<=1.2*r]
                axis=False
                for i in native['tree'].query(Point(x,y).buffer(2*r)):
                    en=native['entities'][native['ids'][int(i)]][0]
                    if en.dxftype()!='LINE':continue
                    a,b=list(en.dxf.start)[:2],list(en.dxf.end)[:2];length=math.dist(a,b)
                    if length<=6*r or min(math.dist(a,(x,y)),math.dist(b,(x,y)))>2*r:continue
                    distance=abs((b[0]-a[0])*(a[1]-y)-(a[0]-x)*(b[1]-a[1]))/length
                    if distance<.2*r:axis=True;break
                if len(scales)==1 and not axis:
                    scale=scales[0]
                    targets.append(dict(detail_number=key,title=key+' · '+scale['text'],title_state='unresolved',
                        layout_id=lid,entity_id=label['entity_id'],text_entity_ids=[label['entity_id'],scale['entity_id']],
                        support_entity_ids=[eid],bbox=union_box([[x-r,y-r,x+r,y+r],scale['bbox']]),
                        method='circle_scale_marker',marker_center=[x,y]))
                elif not scales and not axis and 1.2*r<label['height']<=1.8*r:
                    # Large standalone numbering without a local scale needs
                    # additional sheet-level evidence before becoming a target.
                    unscaled_markers.append(dict(detail_number=key,layout_id=lid,entity_id=label['entity_id'],
                        text_entity_ids=[label['entity_id']],support_entity_ids=[eid],
                        bbox=[x-r,y-r,x+r,y+r],marker_center=[x,y]))
        # Keep the original inline-key path, excluding directory/title-block names.
        for t in ts:
            title=t['text'].strip()
            if len(title)>80 or re.search('详见|参见|图纸|目录',title):continue
            match=re.fullmatch(r'\s*(?:详图\s*)?([A-Za-z0-9]+)(?![A-Za-z0-9])\s*(?:[.、:：)）\-]\s*)?(.{0,40}(?:详图|大样|节点).*)',title)
            suffix=re.fullmatch(r'.{0,24}(?:详图|大样|节点)\s*([A-Za-z0-9]+)(?:\s+1\s*[:：]\s*\d+)?',title)
            if not match and not suffix:continue
            # A separate marker is the index key; the title's “大样4” may be a
            # descriptive variant and must not become a second key (real case).
            if any(v['entity_id']==t['entity_id'] for v in targets):continue
            key=(match or suffix)[1]
            targets.append(dict(detail_number=key,title=title,layout_id=lid,entity_id=t['entity_id'],
                text_entity_ids=[t['entity_id']],support_entity_ids=[],bbox=t['bbox'],method='inline_numbered_title'))
    # Recover explicit field/value pairs from existing source table cells. No
    # inference from filenames, sheet directories, or a nearest arbitrary code.
    for table in index.tables:
        for row in table['rows']:
            cells=row['cells']
            for i,cell in enumerate(cells[:-1]):
                if re.sub(r'\s+','',cell['text']) not in ('图号','图纸编号','图纸号'):continue
                value=cells[i+1]['text'].strip()
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,20}',value):continue
                ids=cell['entity_ids']+cells[i+1]['entity_ids']
                sheets.append(dict(value=value,layout_id=table['layout_id'],text_entity_ids=ids,
                    bbox=union_box([cell['bbox'],cells[i+1]['bbox']]),method='explicit_table_field'))
    for t in texts:
        m=re.fullmatch(r'\s*(?:图号|图纸编号)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9.\-]{0,20})\s*',t['text'])
        if m:sheets.append(dict(value=m[1],layout_id=t['layout_id'],text_entity_ids=[t['entity_id']],bbox=t['bbox'],method='explicit_inline_field'))
    def covers(outer,inner):return outer[0]-.2<=inner[0] and outer[1]-.2<=inner[1] and outer[2]+.2>=inner[2] and outer[3]+.2>=inner[3]
    def owners(lid,bounds):
        matches=[b for b in borders if b['layout_id']==lid and covers(b['bbox'],bounds)]
        # Preserve nested scope, choosing the smallest explicit border.
        matches.sort(key=lambda b:(b['bbox'][2]-b['bbox'][0])*(b['bbox'][3]-b['bbox'][1]))
        return matches
    # Some title blocks are not available as table grids (merged cells / mixed
    # graphics). Recover only unique, aligned field/value pairs in a real border.
    fields=[]
    all_texts=Nearby(texts,lambda t:Point(t['origin'][:2]))
    for label in texts:
        field=re.sub(r'\s+','',label['text'])
        if field not in ('图号','图纸编号','图纸号','图纸名称','图名','比例'):continue
        lid=label['layout_id'];h=label['height'];x,y=label['origin'][:2]
        if abs((label.get('rotation',0)+180)%360-180)>.1:continue
        choices=[]
        for t in all_texts.query([label['bbox'][2]-.1*h,y-.6*h,label['bbox'][2]+8*h,y+.6*h]):
            if t['layout_id']!=lid or not .7*h<=t['height']<=1.4*h or abs((t.get('rotation',0)+180)%360-180)>.1:continue
            value=t['text'].strip()
            valid=(bool(re.fullmatch(r'[A-Za-z]+[A-Za-z0-9.\-]*\d[A-Za-z0-9.\-]*',value)) if field in ('图号','图纸编号','图纸号') else
                   bool(SCALE.fullmatch(value)) if field=='比例' else
                   len(value)<=40 and bool(re.search('详图|大样|节点',value)) and not re.search('详见|参见',value))
            bounds=union_box([label['bbox'],t['bbox']]);owner=owners(lid,bounds)
            if valid and owner:choices.append((t,bounds,owner[0]))
        if len(choices)!=1:continue
        t,bounds,owner=choices[0]
        item=dict(value=t['text'].strip(),layout_id=lid,text_entity_ids=[label['entity_id'],t['entity_id']],
                  bbox=bounds,method='explicit_aligned_field',field=field,border=owner)
        fields.append(item)
        if field in ('图号','图纸编号','图纸号') and not any(label['entity_id'] in v['text_entity_ids'] for v in sheets):
            sheets.append(item)
    for sheet in sheets:
        owner=owners(sheet['layout_id'],sheet['bbox'])
        sheet['border']=owner[0] if owner else None
    for marker in unscaled_markers:
        owner=owners(marker['layout_id'],marker['bbox'])
        if not owner:continue
        bid=owner[0]['id']
        names=[f for f in fields if f['border']['id']==bid and f['field'] in ('图纸名称','图名')]
        scales=[f for f in fields if f['border']['id']==bid and f['field']=='比例']
        numbered=[s for s in sheets if s['border'] and s['border']['id']==bid]
        peers=[m for m in unscaled_markers if m['layout_id']==marker['layout_id'] and covers(owner[0]['bbox'],m['bbox'])]
        if len(names)!=1 or len(scales)!=1 or not numbered or len(peers)!=1:continue
        targets.append(marker|dict(title=marker['detail_number']+' · '+names[0]['value'],title_state='sheet_context_only',
            text_entity_ids=marker['text_entity_ids']+names[0]['text_entity_ids']+scales[0]['text_entity_ids'],
            method='circle_detail_sheet_marker'))
    for target in targets:
        target['sheet_candidates']=[s for s in sheets if s['border'] and s['layout_id']==target['layout_id'] and covers(s['border']['bbox'],target['bbox'])]
        target['border_ids']=[b['id'] for b in owners(target['layout_id'],target['bbox'])]
    return dict(targets=targets,sheets=sheets,borders=borders)
