"""Fixed pure geometric operations. No persistence or source file access."""
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union, nearest_points
from shapely.strtree import STRtree


def measure(payload):
    records, factor, operation = payload['records'], payload['factor'], payload['operation']
    shapes = [unary_union([LineString(p) for p in r['paths']]) for r in records]
    result = {}
    if operation == 'distance':
        value = shapes[0].distance(shapes[1])*factor
        result['measurement_line'] = [list(p.coords)[0] for p in nearest_points(*shapes)]
    elif operation == 'length':
        value = sum(LineString(p).length for p in records[0]['paths'])*factor
    elif operation == 'area':
        paths = records[0]['paths']
        if len(paths) != 1 or paths[0][0] != paths[0][-1]:
            raise ValueError('面积工具要求单一闭合轮廓；不以包围框代替面积。')
        poly = Polygon(paths[0])
        if not poly.is_valid:
            raise ValueError('轮廓自相交或无效，无法测量面积。')
        value = poly.area*factor**2
        result['boundary'] = paths[0]
    else:
        raise ValueError('Unsupported measurement operation')
    return result | dict(status='measured', value=round(value,6))


def spatial_query(payload):
    from .source import GeometryIndex
    index = GeometryIndex(payload['data'])
    return [r['entity_id'] for r in index.query(payload['layout_id'],payload['bounds'],payload['relation'])]


def discovery_anchors(payload):
    anchors = payload['anchors']
    tree = STRtree([Point(point) for _,point in anchors])
    return [list(dict.fromkeys(anchors[int(i)][0] for i in tree.query(
        Polygon(c['boundary'],c.get('internal_rings',[])),predicate='contains')))
        for c in payload['candidates']]


def visual_candidates(payload):
    nearby,box=payload['nearby'],payload['bounds']
    x0,y0,x1,y1=box
    selected=[e for eid in payload['highlight_entity_ids'] for e in nearby if e['entity_id']==eid]
    from shapely.geometry import LineString, box as shape_box
    window=shape_box(*box); ranked=[]
    for e in nearby:
        if e['entity_id'] in payload['highlight_entity_ids']:continue
        length=sum(LineString(p).intersection(window).length for p in e['paths'] if len(p)>1)
        if length<=max(x1-x0,y1-y0)*1e-8:continue
        a,b,c,d=e['bbox'];cx=max(x0,min(x1,(a+c)/2));cy=max(y0,min(y1,(b+d)/2))
        cell=(min(3,int((cx-x0)/(x1-x0)*4)),min(3,int((cy-y0)/(y1-y0)*4)))
        ranked.append((-length,e['entity_id'],cell,e))
    ranked.sort(key=lambda row:row[:2]);others=[];used=set()
    for _,_,cell,e in ranked:
        if cell not in used:others.append(e);used.add(cell)
    included={e['entity_id'] for e in others}
    others.extend(e for _,_,_,e in ranked if e['entity_id'] not in included)
    return [e['entity_id'] for e in selected + others[:20-len(selected)]]
