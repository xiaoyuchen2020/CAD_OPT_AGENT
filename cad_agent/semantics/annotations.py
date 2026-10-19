"""Conservative symbol assembly from native entities and their transformed instances.

No layer-name whitelist, pixel measurement, guessed storey height or invented link.
All assemblies are candidates; native text evidence remains separately available.
"""
import math
import re
from collections import defaultdict

from shapely.geometry import Point, LineString, box
from shapely.strtree import STRtree

from cad_agent.contracts import stable_id
from cad_agent.cad.reader import union_box
from cad_agent.geometry.source import native_source

VERSION = 'annotation-semantics-v1.2'
AXIS_LABEL = re.compile(r'(?:\d{1,3}|[A-Z]{1,2})(?:[′\x27]|/\d{1,2})?\Z')
FLOOR = re.compile(r'(?:地下[一二三四五六七八九十\d]+层|[一二三四五六七八九十\d]+层|首层|屋面层?|机房层|标准层|(?<![A-Za-z0-9])B\d{1,2}(?:F|层)?(?![A-Za-z0-9])|(?<![A-Za-z0-9])\d{1,2}F(?![A-Za-z0-9]))')
ELEVATION = re.compile(r'([+\-−±]?\d{1,4}\.\d{2,3})\s*(m|米)?\Z', re.I)


class Nearby:
    def __init__(self, items, shape):
        self.items = items
        self.tree = STRtree([shape(x) for x in items])

    def query(self, bounds):
        return [self.items[int(i)] for i in sorted(self.tree.query(box(*bounds)))]


def expanded(bounds, pad):
    a, b, c, d = bounds
    return [a-pad, b-pad, c+pad, d+pad]


def text_norm(t):
    return re.sub(r'\s+', '', t['text'])


class AnnotationBuilder:
    def __init__(self, index, layout_id):
        self.index, self.sid, self.lid = index, index.snapshot, layout_id
        self.texts = [t for t in index.scene['texts'] if t['layout_id'] == layout_id]
        self.tindex = Nearby(self.texts, lambda t: Point(t['origin'][:2]))
        self.lines, self.circles, self.triangles, self.native_leaders = [], [], [], []
        self.diagnostics = []
        native = native_source(index.w, self.sid)
        self.native = native
        self.diagnostics.extend(d.get('code','SOURCE_DIAGNOSTIC') for d in native['diagnostics'] if d.get('layout_id')==layout_id)
        for eid, (e, path, lid, bounds) in native['entities'].items():
            if lid != layout_id:
                continue
            kind = e.dxftype()
            item = dict(entity_id=eid, entity_path=path, bbox=bounds, kind=kind)
            try:
                if kind == 'LINE':
                    points = [list(e.dxf.start)[:2], list(e.dxf.end)[:2]]
                    if math.dist(*points) <= 1e-8:
                        continue
                    ltype = e.dxf.get('linetype', 'BYLAYER')
                    if ltype.upper() == 'BYLAYER':
                        ltype = native['doc'].layers.get(e.dxf.layer).dxf.linetype
                    self.lines.append(item | dict(points=points, shape=LineString(points), linetype=ltype))
                elif kind == 'CIRCLE' and e.dxf.radius > 0:
                    self.circles.append(item | dict(center=list(e.dxf.center)[:2], radius=e.dxf.radius))
                elif kind == 'LEADER':
                    pts = [list(v)[:2] for v in e.vertices]
                    if len(pts) >= 2:
                        self.native_leaders.append(item | dict(bbox=bounds or union_box([[p[0],p[1],p[0],p[1]] for p in pts]),points=pts, head=pts[0], tail=pts[-1], association_handle=e.dxf.get('annotation_handle', None)))
                elif kind in ('LWPOLYLINE', 'SOLID', 'TRACE'):
                    if kind == 'LWPOLYLINE':
                        if not e.closed or any(abs(p[4]) > 1e-9 for p in e.get_points('xyseb')):
                            continue
                        pts = [list(p[:2]) for p in e.get_points('xy')]
                    else:
                        pts = [list(e.dxf.get('vtx'+str(i)))[:2] for i in range(4)]
                    pts = list(dict.fromkeys(tuple(p) for p in pts))
                    if len(pts) == 3:
                        self.triangles.append(item | dict(points=pts))
                elif kind == 'MULTILEADER':
                    self.diagnostics.append('MULTILEADER_NOT_ASSEMBLED')
            except (ValueError, TypeError, AttributeError, KeyError):
                self.diagnostics.append('SYMBOL_ENTITY_UNAVAILABLE')
        self.lindex = Nearby(self.lines, lambda x: x['shape'])
        self.add_line_triangles()

    def add_line_triangles(self):
        # Exploded elevation/arrow triangles share actual endpoints. Do not bridge gaps.
        edges={};adj=defaultdict(set)
        for line in self.lines:
            a,b=(tuple(round(v,5) for v in p) for p in line['points'])
            edges[tuple(sorted((a,b)))]=line
            adj[a].add(b);adj[b].add(a)
        seen=set()
        for a,neighbors in adj.items():
            if len(neighbors)>24:continue
            for b in neighbors:
                if b<=a or len(adj[b])>24:continue
                for c in neighbors.intersection(adj[b]):
                    if c<=b:continue
                    key=(a,b,c)
                    if key in seen:continue
                    seen.add(key)
                    ids=[edges[tuple(sorted(pair))]['entity_id'] for pair in ((a,b),(a,c),(b,c))]
                    self.triangles.append(dict(entity_id=ids[0],support_ids=ids,points=[a,b,c],
                        bbox=[min(p[0] for p in key),min(p[1] for p in key),max(p[0] for p in key),max(p[1] for p in key)]))

    def record(self, kind, entities, texts, bounds, **fields):
        ids = sorted(set(entities + [t['entity_id'] for t in texts]))
        frame_ids = sorted({r.frame_id for t in texts for r, _ in self.index.entity_sources.get(t['entity_id'], [])})
        return dict(object_id=stable_id('symbol', self.sid, VERSION, kind, *ids),
                    kind=kind, snapshot_id=self.sid, layout_id=self.lid, frame_ids=frame_ids,
                    bbox=bounds, entity_ids=ids, text_entity_ids=[t['entity_id'] for t in texts],
                    state='candidate', **fields)

    def build(self):
        out = {k: [] for k in ('axes', 'references', 'leaders', 'elevations', 'storeys')}
        for c in self.circles:
            x, y = c['center']; r = c['radius']
            bounds = [x-r, y-r, x+r, y+r]
            texts = [t for t in self.tindex.query(expanded(bounds, r*.25))
                     if math.dist(t['origin'][:2], (x,y)) <= r*1.3 and t['height'] <= 1.7*r]
            nearby_lines = self.lindex.query(expanded(bounds, r*2))
            dividers = [l for l in nearby_lines if abs(l['points'][0][1]-l['points'][1][1]) < .15*r
                        and l['shape'].distance(Point(x,y)) < .2*r and l['shape'].length >= 1.5*r]
            upper = [t for t in texts if t['origin'][1] >= y and len(text_norm(t)) <= 16]
            lower = [t for t in texts if t['origin'][1] < y and len(text_norm(t)) <= 16]
            if dividers and len(upper) == 1 and len(lower) == 1:
                tails = [l for l in nearby_lines if l['shape'].length > 2.5*r
                         and min(abs(math.dist(p,(x,y))-r) for p in l['points']) < .35*r]
                out['references'].append(self.record('reference', [c['entity_id'], dividers[0]['entity_id']]+[l['entity_id'] for l in tails],
                    upper+lower, union_box([bounds]+[l['bbox'] for l in tails if l['bbox']]),
                    detail_number=text_norm(upper[0]), sheet_number=text_norm(lower[0]),
                    label=text_norm(upper[0])+'/'+text_norm(lower[0]), center=c['center'], radius=r,method='divided_circle_text',
                    leader_entity_ids=[l['entity_id'] for l in tails]))
                reference=out['references'][-1]
                for tail in tails:
                    ends=tail['points']
                    anchor=min(ends,key=lambda p:math.dist(p,(x,y)))
                    target=max(ends,key=lambda p:math.dist(p,(x,y)))
                    out['leaders'].append(self.record('leader',[c['entity_id'],tail['entity_id']],upper+lower,
                        union_box([bounds,tail['bbox']]),reference_id=reference['object_id'],
                        points=[anchor,target],tail=anchor,head=target,label=reference['label'],
                        method='index_connected_line',text_relation='index_circle_candidate',
                        target_state='endpoint_only_unresolved',note='索引圆边缘连接的线段；端点不确认目标构件，折线剩余段未自动追踪。'))
                continue
            labels = [t for t in texts if AXIS_LABEL.fullmatch(text_norm(t))]
            if (dividers and len(texts)>1) or len(labels) != 1 or len(texts)!=1:
                continue
            lines = [l for l in nearby_lines if l['shape'].length >= 6*r
                     and l['shape'].distance(Point(x,y)) <= 1.6*r
                     and self.center_line_distance(l['points'], (x,y)) <= .2*r]
            if not lines:
                continue
            lines.sort(key=lambda l: (-l['shape'].length, l['entity_id']))
            best = lines[0]
            out['axes'].append(self.record('axis', [c['entity_id'], best['entity_id']], labels,
                union_box([bounds, best['bbox']]), label=text_norm(labels[0]), center=c['center'], radius=r,
                line=best['points'], line_entity_id=best['entity_id'], linetype=best['linetype'],
                alternative_line_ids=[l['entity_id'] for l in lines[1:]], method='circle_label_collinear_line'))
        for leader in self.native_leaders:
            x,y = leader['tail']
            length = sum(math.dist(a,b) for a,b in zip(leader['points'],leader['points'][1:]))
            radius = max(length*.3, 1)
            texts = [t for t in self.tindex.query([x-radius,y-radius,x+radius,y+radius])
                     if math.dist(t['origin'][:2],(x,y)) <= max(t['height']*5, radius*.5)]
            exact = [t for t in texts if leader['association_handle'] and t['entity_path'][-1] == leader['association_handle']]
            chosen = exact or texts
            out['leaders'].append(self.record('leader', [leader['entity_id']], chosen[:12], leader['bbox'],
                points=leader['points'], head=leader['head'], tail=leader['tail'],
                label=' | '.join(t['text'] for t in chosen[:12]),
                text_relation='native_association' if exact else 'proximity_candidate',
                text_candidates_truncated=len(chosen)>12, target_state='unresolved',
                method='native_leader'))
        out['leaders'].extend(self.exploded_leaders())
        # Explicit text references work even when symbols have been exploded.
        for t in self.texts:
            raw = t['text'].strip()
            match = re.search(r'(?:详见|参见|见详图)\s*(?:图\s*)?([A-Za-z0-9][A-Za-z0-9.\-]*)[/／]([A-Za-z0-9][A-Za-z0-9.\-]*)', raw)
            if match:
                out['references'].append(self.record('reference', [], [t], t['bbox'],
                    detail_number=match[1], sheet_number=match[2], label=match[0],
                    method='explicit_reference_text', leader_entity_ids=[]))
        tri_index = Nearby(self.triangles, lambda t: box(*t['bbox']) if t['bbox'] else LineString(t['points']))
        for t in self.texts:
            raw = text_norm(t)
            numeric_text=re.sub(r'^(?:(?:建筑|结构|绝对|相对)标高[:：]?)','',raw)
            match = ELEVATION.fullmatch(numeric_text)
            if match:
                h=t['height']; x,y=t['origin'][:2]
                tris=tri_index.query([x-8*h,y-6*h,x+8*h,y+4*h])
                tris=[p for p in tris if p['bbox'] and max(p['bbox'][2]-p['bbox'][0],p['bbox'][3]-p['bbox'][1]) <= 8*h
                      and self.elevation_triangle(p)]
                if not tris:
                    continue
                nums=match[1].replace('−','-')
                value=float(nums.replace('±','')) if not nums.startswith('±') or float(nums[1:])==0 else None
                out['elevations'].append(self.record('elevation', [eid for p in tris for eid in p.get('support_ids',[p['entity_id']])], [t],
                    union_box([t['bbox']]+[p['bbox'] for p in tris]), label=raw, value=value,
                    value_unit='m' if match[2] else None, unit_basis='explicit_text' if match[2] else 'not_stated',
                    datum='absolute' if raw.startswith('绝对标高') else 'relative' if raw.startswith('相对标高') else 'unknown',
                    elevation_type='architectural' if raw.startswith('建筑标高') else 'structural' if raw.startswith('结构标高') else 'unknown', symbol_candidates=len(tris),
                    method='numeric_text_near_triangle'))
            # Floor labels must be short drawing labels, not clauses mentioning a floor.
            floor=FLOOR.search(raw)
            floor_prefix=raw[:floor.start()] if floor else ''
            floor_suffix=raw[floor.end():] if floor else ''
            label_shape=floor_prefix in ('','地下室','地上','(','（') and (not floor_suffix.strip(')）') or
                bool(re.fullmatch(r'(?:总)?(?:平面图|楼面|标高|平台|顶板|底板|梁板|顶|底)(?:[（(][^）)]{1,8}[）)])?',floor_suffix)))
            if floor and label_shape and len(raw) <= 35:
                out['storeys'].append(self.record('storey', [], [t], t['bbox'], label=raw,
                    floor_label=floor[0], elevation_ids=[], method='floor_label',
                    storey_number=self.floor_number(floor[0]),building_id=None,
                    datum='unknown', elevation_type='unknown'))
        # Association by aligned annotation row, retaining every competing candidate.
        elev_index=Nearby(out['elevations'], lambda e:box(*e['bbox']))
        for floor in out['storeys']:
            t=next(t for t in self.texts if t['entity_id']==floor['text_entity_ids'][0])
            x,y=t['origin'];h=t['height']
            for e in elev_index.query([x-40*h,y-2*h,x+40*h,y+3*h]):
                if set(e['frame_ids']).intersection(floor['frame_ids']):
                    floor['elevation_ids'].append(e['object_id'])
            floor['elevation_relation']='aligned_row_candidate' if floor['elevation_ids'] else 'unresolved'
            floor['elevation_candidates']=[{k:e[k] for k in ('object_id','label','value','value_unit','datum','elevation_type')} for e in out['elevations'] if e['object_id'] in floor['elevation_ids']]
            floor['height_status']='unresolved'
            floor['height_note']='楼层标签和标高行仅为关联候选；未确认同楼栋、同基准及单位，不生成层高。'
        for values in out.values():
            values.sort(key=lambda v:(-(v['bbox'] or [0,0,0,0])[3],(v['bbox'] or [0])[0],v['object_id']))
        return out | {'diagnostics': sorted(set(self.diagnostics)), 'version':VERSION,
                      'coverage':'partial', 'note':'符号与关系为候选，非工程确认；未识别不代表不存在。'}

    @staticmethod
    def center_line_distance(points, point):
        a,b=points; dx,dy=b[0]-a[0],b[1]-a[1]
        return abs(dx*(a[1]-point[1])-(a[0]-point[0])*dy)/math.hypot(dx,dy)

    @staticmethod
    def elevation_triangle(triangle):
        pts=sorted(triangle['points'],key=lambda p:p[1]);lo,a,b=pts
        width=abs(a[0]-b[0]);height=(a[1]+b[1])/2-lo[1]
        return width>0 and abs(a[1]-b[1])<width*.05 and .15*width<=height<=1.5*width and min(a[0],b[0])<lo[0]<max(a[0],b[0])

    @staticmethod
    def floor_number(label):
        if label=='首层':return 1
        digits=re.search(r'\d+',label)
        if digits:value=int(digits[0])
        else:
            match=re.search(r'[一二三四五六七八九十]+',label)
            if not match:return None
            tokens=match[0];nums={c:i for i,c in enumerate('零一二三四五六七八九',0)}
            if '十' in tokens:
                a,b=tokens.split('十',1);value=nums.get(a,1)*10+nums.get(b,0)
            else:value=nums.get(tokens)
        return -value if value is not None and (label.startswith('地下') or label.startswith('B')) else value

    def exploded_leaders(self):
        results=[];seen=set()
        for tri in self.triangles:
            pts=tri['points'];pairs=[(math.dist(pts[i],pts[j]),i,j) for i,j in ((0,1),(0,2),(1,2))]
            short,i,j=min(pairs);tip=pts[3-i-j];base=[(pts[i][k]+pts[j][k])/2 for k in (0,1)]
            length=math.dist(tip,base)
            if not short or length<2*short or length>20*short:continue
            supports=tri.get('support_ids',[tri['entity_id']]);pad=max(short*.25,1e-5)
            attached=[l for l in self.lindex.query(expanded(tri['bbox'],pad)) if l['entity_id'] not in supports
                      and min(l['shape'].distance(Point(tip)),l['shape'].distance(Point(base)))<=pad and l['shape'].length>length*2
                      and self.center_line_distance(l['points'],tip)<=pad and self.center_line_distance(l['points'],base)<=pad]
            if len(attached)!=1:continue
            line=attached[0];path=[list(tip)];ids=supports+[line['entity_id']]
            tail=max(line['points'],key=lambda p:math.dist(p,tip))
            if sum((tail[k]-tip[k])*(base[k]-tip[k]) for k in (0,1))<=0:continue
            path.append(tail);branched=False
            for _ in range(3):
                around=self.lindex.query(expanded([*tail,*tail],pad))
                nexts=[l for l in around if l['entity_id'] not in ids and min(math.dist(p,tail) for p in l['points'])<=pad]
                if len(nexts)!=1:
                    branched=len(nexts)>1
                    break
                nxt=nexts[0];tail=max(nxt['points'],key=lambda p:math.dist(p,tail));path.append(tail);ids.append(nxt['entity_id'])
            if branched:continue
            span=length*6;x,y=tail
            texts=[t for t in self.tindex.query([x-span,y-span,x+span,y+span])
                   if math.dist(t['origin'][:2],tail)<t['height']*3 and len(text_norm(t))>=4 and re.search(r'[\u4e00-\u9fff]',t['text'])]
            if not texts:continue
            key=tuple(sorted(ids))
            if key in seen:continue
            seen.add(key)
            bounds=union_box([[p[0],p[1],p[0],p[1]] for p in path]+[t['bbox'] for t in texts[:8]])
            results.append(self.record('leader',ids,texts[:8],bounds,points=path,head=list(tip),tail=tail,
                label=' | '.join(t['text'] for t in texts[:8]),method='triangle_connected_lines',
                text_relation='proximity_candidate',text_candidates_truncated=len(texts)>8,target_state='unresolved'))
        return results
