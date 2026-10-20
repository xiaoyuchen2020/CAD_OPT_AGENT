"""One immutable record contract, shared by HTTP and all agent runtimes."""
import json

from cad_agent.analysis.general import DrawingIndex
from cad_agent.contracts import stable_id
from cad_agent.geometry.contracts import RoomDiscover
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.source import GEOMETRY_VERSION
from cad_agent.geometry.discovery import DISCOVERY_VERSION
from .context import build_context
from .dimensions import read_dimensions, DIMENSION_VERSION
from .relations import refine_discovery, enrich_openings

COMPONENT_VERSION = "drawing-components-v1.6-native-viewports"
COLLECTIONS = {"context": ("frames", "subframes"), "dimensions": ("dimensions",),
               "relations": ("rooms", "walls", "openings", "relations")}


class ComponentService:
    def __init__(self, workspace, snapshot_id):
        self.w, self.sid = workspace, snapshot_id
        self.drawing = workspace.store.get("drawing", snapshot_id)

    def scope(self, args):
        if not args.layout_id:
            layouts=self.drawing['layouts']
            if len(layouts)!=1:raise ValueError('需指定布局；当前图纸有多个布局。')
            args.layout_id=layouts[0]['layout_id']
        reading=DrawingIndex(self.w,self.sid)
        reading.scope(args.layout_id,args.frame_id)
        frames=[f for f in self.drawing["frames"] if f["layout_id"]==args.layout_id and
                (not args.frame_id or f["frame_id"]==args.frame_id)]
        bounds=args.bbox
        if args.frame_id:
            if not frames:raise ValueError("图框与布局不匹配。")
            b=frames[0]["bbox"]
            bounds=[max(bounds[0],b[0]),max(bounds[1],b[1]),min(bounds[2],b[2]),min(bounds[3],b[3])] if bounds else b
            if bounds[0]>=bounds[2] or bounds[1]>=bounds[3]:raise ValueError("查询范围与图框不相交。")
        return reading,frames,bounds

    def build(self, kind, args):
        # Pydantic defaults may remain int (3), while HTTP inputs become float
        # (3.0). Normalize both before hashing so all callers share evidence IDs.
        args=type(args).model_validate(args.model_dump())
        reading,frames,bounds=self.scope(args)
        request=args.model_dump(exclude={"offset","limit","view"}) | {"bbox":bounds}
        key=stable_id("component",self.sid,self.drawing["sha256"],COMPONENT_VERSION,GEOMETRY_VERSION,
                      DISCOVERY_VERSION,kind,json.dumps(request,sort_keys=True),
                      *([DIMENSION_VERSION] if kind=='dimensions' else []))
        try:return self.get(key)
        except KeyError:pass
        if kind=="context":
            data=build_context(reading,args.layout_id,frames)
            if args.title:
                from cad_agent.cad.titles import same_plan_title
                exact=[s for s in data['subframes'] if same_plan_title(s['title'],args.title)]
                data['subframes']=exact or [s for s in data['subframes'] if args.title in s['title']]
                owners={fid for s in data['subframes'] for fid in s['frame_ids']}
                data['frames']=[f for f in data['frames'] if f.get('frame_id') in owners or f['object_id'] in {s.get('parent_border_id') for s in data['subframes']}]
            if bounds:
                from shapely.geometry import box
                for collection in COLLECTIONS[kind]:
                    data[collection]=[x for x in data[collection] if box(*bounds).intersects(box(*(x.get("title_bbox") or x["bbox"])))]
            # Paper layouts may contain only annotation and viewport frames.
            # Expose their native model references without copying paper-space
            # coordinates into a model-space geometry request.
            if args.layout_id != stable_id('layout',self.sid,'Model'):
                from .viewports import attach_model_scopes
                attach_model_scopes(self.w,self.sid,args.layout_id,data['subframes'])
            data["truncated"]=any(len(data[c])>5000 for c in COLLECTIONS[kind])
            for c in COLLECTIONS[kind]:data[c]=data[c][:5000]
        elif kind=="dimensions":
            data=read_dimensions(self.w,self.sid,args.layout_id,bounds,args.unit_override or self.drawing["unit"])
            data["unit_basis"]="request_override" if args.unit_override else "dxf_header"
            data['unit_status']='assumption' if args.unit_override else 'source_declared' if self.drawing['unit']!='unknown' else 'unknown'
            data['unit_source_conflict']=bool(args.unit_override and self.drawing['unit']!='unknown' and args.unit_override!=self.drawing['unit'])
            data['dimension_version']=DIMENSION_VERSION
        elif kind=="relations":
            from cad_agent.geometry.source import ScopedSourceBundle
            source_bundle=ScopedSourceBundle(self.w,self.sid)
            discovery=GeometryService(self.w,self.sid).discover(RoomDiscover.model_validate({k:v for k,v in request.items() if k!="linework_mode"}), source_bundle=source_bundle)
            discovery=refine_discovery(self.w,self.sid,discovery,reading,args,source_bundle=source_bundle)
            from cad_agent.geometry.compute import compute
            data=compute(self.w,'component_relations',dict(discovery=discovery,options=args.model_dump()))
            enrich_openings(self.w,self.sid,data,discovery,reading,args,source_bundle=source_bundle)
            data["linework_basis"]=discovery.get("linework_basis","all_supported_linework")
            data["layer_selection"]=discovery.get("layer_selection")
            data["scope_bbox"]=discovery["scope_bbox"]
            data["applied_unit"]=discovery["applied_unit"]
            data["unit_basis"]=discovery["unit_basis"]
        else:raise ValueError("未知公共组件。")
        result=dict(record_id=key,snapshot_id=self.sid,source_sha256=self.drawing["sha256"],
            component_version=COMPONENT_VERSION,kind=kind,layout_id=args.layout_id,request=request,
            coordinate_space="dxf_wcs",unit=self.drawing["unit"],status="needs_review",
            evidence_url=f"/api/drawings/{self.sid}/components/{key}",**data)
        result["source_diagnostics"]=self.drawing.get("diagnostics",[])
        self.w.store.put("component",key,result)
        return result

    def get(self, record_id):
        record=self.w.store.get("component",record_id)
        if record["snapshot_id"]!=self.sid or record["source_sha256"]!=self.drawing["sha256"]:
            raise KeyError(record_id)
        return record

    @staticmethod
    def page(record, view, offset=0, limit=8, *, page_call_budget=None):
        if view not in COLLECTIONS[record["kind"]]:raise ValueError("该记录不包含所请求的视图。")
        items=record[view]
        keys=("record_id","snapshot_id","source_sha256","component_version","layout_id","kind",
              "coordinate_space","unit","unit_basis","unit_status","unit_source_conflict","dimension_version","applied_unit","status","evidence_url",
              "diagnostics","coverage","truncated","scope_bbox","discovery_id","source_diagnostics")
        result={k:record[k] for k in keys if k in record}
        result["linework_basis"]=record.get("linework_basis")
        for key in ("diagnostics","source_diagnostics"):
            if key in result:
                result[key+"_count"]=len(result[key]);result[key+"_truncated"]=len(result[key])>20
                result[key]=result[key][:20]
        if record['kind']=='relations':
            from .pagination import relation_page
            result.update(view=view,counts={k:len(record[k]) for k in COLLECTIONS[record['kind']]},
                claim_contract="工具观测只证明提取/推导结果；对象与关系身份待确认。候选解释标 derived/unknown，不得宣称已确认设计事实。")
            return relation_page(result,items,offset,limit,page_call_budget)
        page=[]
        for item in items[offset:offset+limit]:
            item=dict(item)
            for key in ("entity_ids","boundary","rendered_texts"):
                if isinstance(item.get(key),list) and len(item[key])>64:
                    item[key+"_count"]=len(item[key]);item[key+"_truncated"]=True;item[key]=item[key][:64]
            if isinstance(item.get('scale_candidates'),list):
                scales=item['scale_candidates']
                item['scale_source_count']=len(scales)
                item['scale_candidates']=[next(s for s in scales if s['value']==v) for v in dict.fromkeys(s['value'] for s in scales)]
                item['scale_sources_deduplicated']=True
            page.append(item)
        result.update(view=view,items=page,total=len(items),offset=offset,
            requested_limit=limit,effective_limit=len(page),next_offset=offset+len(page) if offset+len(page)<len(items) else None,
            has_more=offset+len(page)<len(items),counts={k:len(record[k]) for k in COLLECTIONS[record["kind"]]},
            claim_contract="工具观测只证明提取/推导结果；对象与关系身份待确认。候选解释标 derived/unknown，不得宣称已确认设计事实。")
        return result
