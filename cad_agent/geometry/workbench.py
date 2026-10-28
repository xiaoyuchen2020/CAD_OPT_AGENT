"""Source-bound geometry workbench. Immutable bundles and local measurements."""
import hashlib
import json
from collections import Counter
from copy import deepcopy
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record, FiniteFloat, stable_id
from cad_agent.geometry.source import build_geometry, finite_box, GEOMETRY_VERSION
from cad_agent.geometry.local_measurement import measure_pair, VERSION as PAIR_VERSION
VERSION='geometry-workbench-v3:'+GEOMETRY_VERSION+':'+PAIR_VERSION

PROMPT = '''几何任务可 prepare_geometry_bundle 读取已定位范围，read_geometry_bundle 分页筛选原生线/弧；复用bundle_id，不重复加载。
measure_local_geometry 对已选两源实体计算有限直线法向间距或同心弧径向间距；工具不按规则阈值选面，单位未知仍返回drawing_unit。
render_geometry_workbench 可回看整个bundle或measurement_id测线。将平面/顶板/剖面、多个对象分别记录；图层名不确认用途。
先获得可复算局部值，再调查具体未覆盖段和柱等凸出物。几何距离不自动等于结构面/完成面净宽，投影交叠不证明同高度。
诊断只限制该项，允许换候选。结果有来源、口径、测线及具体缺口才构成进展；不要覆盖旧结果或重复最终JSON。'''


class PrepareBundle(Record):
    layout_id: str
    bbox: list[FiniteFloat] = Field(min_length=4,max_length=4)


class ReadBundle(Record):
    bundle_id: str
    kind: str = ''
    layer: str = ''
    source_paths: list[list[str]] = Field(default_factory=list,max_length=80)
    bbox: list[FiniteFloat] | None = Field(default=None,min_length=4,max_length=4,description='Optional WCS selection window within bundle. Includes intersecting entity bounding boxes; not semantic containment.')
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=30,ge=1,le=80)


class MeasurePair(Record):
    bundle_id: str
    entity_ids: list[str] = Field(min_length=2,max_length=2)
    basis: Literal['parallel_normal','concentric_radial']


class RenderWorkbench(Record):
    bundle_id: str
    measurement_id: str = ''
    bbox: list[FiniteFloat] | None = Field(default=None,min_length=4,max_length=4)
    focus_measurement: bool = True


class ReadWorkset(Record):
    pass


def _identity(data):return json.dumps(data,sort_keys=True,separators=(',',':'))


class GeometryWorkbench:
    def __init__(self,w,sid):
        self.w,self.sid=w,sid
        self.drawing=w.store.get('drawing',sid)

    def check(self):
        path=self.w.store.artifact(self.sid,'source.dxf')
        with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        if digest!=self.drawing['sha256']:raise ValueError('GEOMETRY_SOURCE_CHANGED')

    def bundle(self,bid):
        self.check()
        b=self.w.store.get('geometry_bundle',bid)
        if b['snapshot_id']!=self.sid or b['source_sha256']!=self.drawing['sha256']:
            raise ValueError('FOREIGN_GEOMETRY_BUNDLE')
        if b.get('workbench_version')!=VERSION:raise ValueError('STALE_GEOMETRY_BUNDLE: prepare a bundle with the current algorithm version.')
        return b

    def prepare(self,args):
        finite_box(args.bbox);self.check()
        if args.layout_id not in {l['layout_id'] for l in self.drawing['layouts']}:
            raise ValueError('FOREIGN_LAYOUT')
        bid=stable_id('gb',self.sid,self.drawing['sha256'],VERSION,_identity(args.model_dump()))
        try:b=self.bundle(bid)
        except KeyError:
            b=build_geometry(self.w,self.sid,bounds=args.bbox,layout_id=args.layout_id)
            b.update(bundle_id=bid,layout_id=args.layout_id,request=args.model_dump(),workbench_version=VERSION)
            self.w.store.put('geometry_bundle',bid,b)
        return self.summary(b)

    def summary(self,b):
        from cad_agent.geometry.workbench_routes import contains
        return dict(bundle_id=b['bundle_id'],snapshot_id=self.sid,source_sha256=b['source_sha256'],
            scope=b['request'],unit=b['unit'],entity_count=len(b['entities']),
            kinds=dict(Counter(r['kind'] for r in b['entities'])),
            layers=dict(Counter(r['layer'] for r in b['entities'])),
            coverage=dict(loader_complete=b['complete'],target_complete=False,diagnostics=b['diagnostics'],
                crossing_window_entities=sum(bool(r.get('bbox')) and not contains(b['request']['bbox'],r['bbox']) for r in b['entities']),
                selection='bbox intersection selects whole native entities; not a clipping or object-completeness proof'),
            next_action=dict(tool='render_geometry_workbench',arguments=dict(bundle_id=b['bundle_id'])),
            read_action=dict(tool='read_geometry_bundle',arguments=dict(bundle_id=b['bundle_id'])))

    def read(self,args):
        b=self.bundle(args.bundle_id)
        if args.kind and args.kind not in {r['kind'] for r in b['entities']}:
            return dict(bundle_id=args.bundle_id,items=[],total=0,has_more=False,next_offset=None,
                diagnostic='NATIVE_KIND_NOT_PRESENT',available_kinds=sorted({r['kind'] for r in b['entities']}),
                note='kind is a native DXF type, not an engineering role. No COLUMN kind does not prove absence of columns.',target_complete=False)
        if args.bbox:
            finite_box(args.bbox)
            from cad_agent.geometry.workbench_routes import contains
            if not contains(b['request']['bbox'],args.bbox):raise ValueError('READ_SCOPE_OUTSIDE_BUNDLE')
        records=[r for r in b['entities'] if (not args.kind or r['kind']==args.kind) and (not args.layer or r['layer']==args.layer) and (not args.source_paths or r['entity_path'] in args.source_paths)]
        if args.bbox:
            a,c,d,e=args.bbox
            records=[r for r in records if r.get('bbox') and not (r['bbox'][2]<a or r['bbox'][0]>d or r['bbox'][3]<c or r['bbox'][1]>e)]
        items=[]
        for r in records[args.offset:args.offset+args.limit]:
            item={k:r[k] for k in ('entity_id','entity_path','layout_id','kind','layer','status','bbox','native')}
            item=deepcopy(item)
            item['native_truncated']=False
            for key,value in item['native'].items():
                if isinstance(value,list) and len(value)>64:
                    item['native'][key]=value[:64];item['native_truncated']=True
            items.append(item)
        end=args.offset+len(items)
        return dict(bundle_id=args.bundle_id,items=items,total=len(records),next_offset=end if end<len(records) else None,
                    has_more=end<len(records),unit=b['unit'],target_complete=False)

    def measure(self,args):
        b=self.bundle(args.bundle_id);by_id={r['entity_id']:r for r in b['entities']}
        if any(e not in by_id for e in args.entity_ids):raise ValueError('SOURCE_NOT_IN_BUNDLE: use read_geometry_bundle.source_paths with exact native instance paths; never construct entity_id from handle.')
        mid=stable_id('lm',self.sid,VERSION,_identity(args.model_dump()))
        try:return self.w.store.get('local_measurement',mid)
        except KeyError:pass
        result=measure_pair(*(by_id[e] for e in args.entity_ids),args.basis)
        result.update(measurement_id=mid,bundle_id=args.bundle_id,snapshot_id=self.sid,
            source_sha256=b['source_sha256'],request=args.model_dump(),source_unit=b['unit'],
            unit=dict(unit=b['unit'],basis='drawing_header',conditional=False),
            spatial_fact_scope=dict(identity=dict(engineering_confirmed=False),extent='local_pair_only'))
        box=b['request']['bbox']
        result['measurement_within_request_window']=all(box[0]<=p[0]<=box[2] and box[1]<=p[1]<=box[3] for p in result.get('measurement_line',[])) if result.get('measurement_line') else None
        self.w.store.put('local_measurement',mid,result)
        return result

    def render(self,args):
        from xml.sax.saxutils import escape
        import resvg_py
        b=self.bundle(args.bundle_id);box=args.bbox or b['request']['bbox'];selected=set()
        if args.measurement_id:
            m=self.w.store.get('local_measurement',args.measurement_id)
            if m['bundle_id']!=args.bundle_id:raise ValueError('FOREIGN_MEASUREMENT')
            selected=set(m['source_entity_ids'])
            boxes=[r['bbox'] for r in b['entities'] if r['entity_id'] in selected and r.get('bbox')]
            if args.focus_measurement and not args.bbox and boxes:
                bounds=[min(r[0] for r in boxes),min(r[1] for r in boxes),max(r[2] for r in boxes),max(r[3] for r in boxes)]
                pad=max(bounds[2]-bounds[0],bounds[3]-bounds[1])*0.15
                a,c,d,e=b['request']['bbox'];box=[max(a,bounds[0]-pad),max(c,bounds[1]-pad),min(d,bounds[2]+pad),min(e,bounds[3]+pad)]
        finite_box(box)
        from cad_agent.geometry.workbench_routes import contains
        if not contains(b['request']['bbox'],box):raise ValueError('RENDER_SCOPE_OUTSIDE_BUNDLE')
        a,c,d,e=box
        scale=1400/max(d-a,e-c);width,height=round((d-a)*scale)+40,round((e-c)*scale)+70
        xy=lambda p:f'{20+(p[0]-a)*scale:.3f},{30+(e-p[1])*scale:.3f}'
        parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><rect width="100%" height="100%" fill="white"/>']
        for r in b['entities']:
            color='#2563eb' if r['entity_id'] in selected else '#8b99a3'
            stroke=2 if r['entity_id'] in selected else 0.7
            for path in r.get('paths',[]):
                parts.append(f'<polyline points="{" ".join(xy(p) for p in path)}" fill="none" stroke="{color}" stroke-width="{stroke}"/>')
        # Original text anchors preserve view titles and elevation context.
        for t in self.w.rendering(self.sid)['items']:
            box=t['bbox']
            if t['layout_id']==b['layout_id'] and box[0]>=a and box[2]<=d and box[1]>=c and box[3]<=e:
                text=t.get('text','')
                if text:
                    x,y=xy([box[0],box[3]]).split(',')
                    parts.append(f'<text x="{x}" y="{y}" font-size="9" fill="#374151">{escape(text)}</text>')
        if args.measurement_id:
            m=self.w.store.get('local_measurement',args.measurement_id)
            if m['bundle_id']!=args.bundle_id:raise ValueError('FOREIGN_MEASUREMENT')
            line=m.get('measurement_line')
            if line:
                parts.append(f'<polyline points="{" ".join(xy(p) for p in line)}" fill="none" stroke="#dc2626" stroke-width="3"/>')
                label=f"{m['basis']} = {m['measurements']['width_drawing_units']:.6g} drawing units; local candidate only"
                parts.append(f'<text x="20" y="20" font-size="15">{escape(label)}</text>')
        parts.append('</svg>');svg=''.join(parts);raw=resvg_py.svg_to_bytes(svg_string=svg)
        iid=stable_id('image',self.sid,VERSION,_identity(args.model_dump()),hashlib.sha256(raw).hexdigest())
        from cad_agent.geometry.vision import VisionService
        path=VisionService(self.w,self.sid).path(iid);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        record=dict(image_id=iid,snapshot_id=self.sid,source_sha256=b['source_sha256'],image_sha256=hashlib.sha256(raw).hexdigest(),
            layout_id=b['layout_id'],bbox=box,kind='geometry_workbench',candidates=[],highlight_entity_ids=sorted(selected),
            image_url=f'/api/drawings/{self.sid}/images/{iid}',measurement_id=args.measurement_id,
            note='source geometry overview; semantic identity and complete coverage unconfirmed')
        self.w.store.put('visual_image',iid,record)
        return record


SPECS={
 'prepare_geometry_bundle':(PrepareBundle,'读取源范围为不可变几何包；保留块路径、原生参数、所有图层和诊断。不确认对象完整。'),
 'read_geometry_bundle':(ReadBundle,'分页读取几何包原生参数，可按kind/layer筛选；复用已读bundle，不重读相同范围。'),
 'measure_local_geometry':(MeasurePair,'计算两条源LINE的有限法向间距或两条同心ARC有效重叠角内径向间距；未知单位仍计算原图单位。不是全程净宽。'),
 'render_geometry_workbench':(RenderWorkbench,'渲染源几何包和可选局部测线，保留原文定位。图像供支持视觉的运行时发送，不能以像素测量。'),
 'read_geometry_workset':(ReadWorkset,'读回本任务已取得的几何包、测量、对象假设、路线、校验与失败回执摘要，不重放原始大数组。'),
}
HANDLERS={'prepare_geometry_bundle':'prepare','read_geometry_bundle':'read','measure_local_geometry':'measure','render_geometry_workbench':'render'}


def execute(session,name,args):
    service=GeometryWorkbench(session.w,session.request.snapshot_id)
    ledger=session.task.data.setdefault('geometry_workset',{'bundles':{},'measurements':{},'attempts':[]})
    # Early experiment ledgers indexed every referenced ID as an owned record.
    # Recover overwritten hypothesis summaries from immutable source records.
    for hid,row in list(ledger.get('hypothesis_ids',{}).items()):
        if 'view_kind' not in row.get('request',{}):
            try:original=session.w.store.get('geometry_hypothesis',hid)
            except KeyError:continue
            if original.get('snapshot_id')==session.request.snapshot_id:
                ledger['hypothesis_ids'][hid]=original
                ledger.setdefault('recovered_hypothesis_ids',[]).append(hid)
    if name=='read_geometry_workset':
        service.check()
        data=ledger;session.task.save()
        fields={'bundle_id','measurement_id','route_id','hypothesis_id','result_id','validation_id','status','measurements','scope','unit','unresolved','source_entity_ids','supported_claim_ids','blocked_claim_ids','supersedes'}
        result={k:({ref:{name:v for name,v in row.items() if name in fields} for ref,row in values.items()} if isinstance(values,dict) and k!='superseded_hypotheses' else deepcopy(values)) for k,values in data.items()}
        result.update(spatial_fact_scope=dict(identity=dict(engineering_confirmed=False),extent='navigation_only'),unit=dict(unit='unknown'),
            note='Use original measurement observation for numerical delivery; workset is navigation, not new verification.')
        from cad_agent.geometry.workbench_checks import view_conflicts
        result['view_conflicts']=view_conflicts(data)
        return result
    try:result=ROUTE_HANDLERS[name](service,args) if name in ROUTE_HANDLERS else getattr(service,HANDLERS[name])(args)
    except (ValueError,KeyError) as ex:
        ledger['attempts'].append(dict(tool=name,request=args.model_dump(),error=str(ex)));session.task.save();raise
    if name=='prepare_geometry_bundle':ledger['bundles'][result['bundle_id']]=result
    if name=='measure_local_geometry':
        ledger['measurements'][result['measurement_id']]=result
        ledger['attempts'].append(dict(measurement_id=result['measurement_id'],status=result['status']))
    owned_id={'register_geometry_hypothesis':'hypothesis_id','measure_geometry_route':'route_id',
              'export_geometry_results':'result_id','validate_geometry_claims':'validation_id'}.get(name)
    if owned_id:ledger.setdefault(owned_id+'s',{})[result[owned_id]]=result
    if name=='register_geometry_hypothesis' and result.get('supersedes'):
        ledger.setdefault('superseded_hypotheses',{}).setdefault(result['supersedes'],[]).append(result['hypothesis_id'])
    if name=='register_geometry_hypothesis':
        from cad_agent.geometry.workbench_checks import view_conflicts
        result['view_conflicts']=view_conflicts(ledger)
    if name=='render_geometry_workbench':session.visual_images[result['image_id']]=result
    if name in ('measure_local_geometry','measure_geometry_route'):
        result['operation_receipts']=[dict(operation='geometry',fulfilled=False,local_result_available=True,
            reference=result.get('measurement_id') or result.get('route_id'),extent='selected_sources_only',tool=name)]
    session.task.save()
    return result

from cad_agent.geometry.workbench_routes import SPECS as ROUTE_SPECS, HANDLERS as ROUTE_HANDLERS
SPECS.update(ROUTE_SPECS)
from cad_agent.geometry.workbench_checks import SPECS as CHECK_SPECS, HANDLERS as CHECK_HANDLERS
SPECS.update(CHECK_SPECS);ROUTE_HANDLERS.update(CHECK_HANDLERS)
PROMPT += "\n同框多图示先inspect_geometry_views列出原生标题，再register_geometry_hypothesis分别登记对象。歧义不阻止读取源几何。候选配对优先discover_geometry_pairs，已读尺寸可传anchor_dimension_id核对端点；不能依据阈值选候选。read_geometry_bundle.source_paths能按完整原生路径查回精确ID。measure_geometry_route按对象假设记录已测区间/缺口及条件障碍影响；export_geometry_results直接保存已有回执，不需编程抄写。"
PROMPT += '\n调查每个对象时先看图示和候选，不要一次取多个图框的最大页。kind是原生DXF类型，柱可能为多段线或块。候选排序不确认通行边界；用render_geometry_workbench的局部测线图核对所穿过区域，错误选边保留为已排除尝试。几何页可能按entity_table列名与行表示，common_fields补充每行共同字段，missing_fields表示原记录缺项，内容和分页状态不变。交付前validate_geometry_claims逐项检查；不支持的全程或完成面声明降为局部原图单位结果，不必重算已经正确的数值。'
PROMPT += '\n新范围先渲染原图并结合原生标题辨认各独立图示，用bbox缩小到一个对象后再生成边界候选；不能把共用图框当成同对象，也不能把每个标题当成新的独立路线。尺寸作锚点前核对其端点和方向是否横跨所问区域，第一条尺寸或最长平行线并不优先。局部测线应落在所选对边并横跨目标空间；轴网、车位、车辆符号和同墙双面可能满足几何前提，须从图面解释后再沿路线扩展。'
