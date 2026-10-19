"""One bounded scoped location pass; labels locate targets, never prove connections."""
from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.geometry.room_batch import RoomBatchService,RoomBatchQuery,normalized

class LocateSpaceLabels(Record):
    layout_id: str = ''
    floor_title: str = Field(default='',max_length=80)
    frame_id: str = ''
    names: list[str] = Field(min_length=1,max_length=4)


def locate(w,sid,args):
    service=RoomBatchService(w,sid);index=service.index
    if len(set(map(normalized,args.names)))!=len(args.names) or any(not 1<=len(n.strip())<=24 for n in args.names):raise ValueError('需要不重复的简短空间名称。')
    index.scope(args.layout_id,args.frame_id)
    frames=[];scope=None
    if args.floor_title or args.frame_id:
        frames=service.frames(RoomBatchQuery(floor_title=args.floor_title,frame_id=args.frame_id,layout_id=args.layout_id,room_label=args.names[0]))
        checked=[(f,service.diagnose_frame(f)) for f in frames]
        eligible=[(f,d) for f,d in checked if d[0]['eligible']]
        if len(eligible)!=1:
            return dict(items=[],scope=None,scope_choices=[dict(frame_id=f['frame_id'],name=f['name'],bbox=f['bbox'],diagnostics=d[0]) for f,d in checked],total=0,enumerated=False,note='目标范围未唯一解决；未执行名称枚举，不表示不存在空间。')
        scope=eligible[0][0];rows=eligible[0][1][1]
    else:rows=index.scope(args.layout_id,'')
    keys={normalized(n) for n in args.names}
    hits=[r for r in rows if len(r.entity_ids)==1 and normalized(r.text) in keys]
    hits.sort(key=lambda r:(-r.bbox[3],r.bbox[0],r.region_id))
    items=[];seen=set()
    for r in hits:
        identity=tuple(r.entity_ids)
        if identity in seen:continue
        seen.add(identity)
        tables=[t['table_id'] for t in index.tables if any(r.region_id in cell['region_ids'] for row in t['rows'] for cell in row['cells'])]
        items.append(dict(text=r.text,source_region_ids=[r.region_id],entity_id=r.entity_ids[0],layout_id=r.layout_id,bbox=r.bbox,table_candidate_ids=tables,
                          note='表格归属为解析候选；须结合局部图排除，不以文字相邻推导连通。'))
    return dict(items=items[:60],total=len(items),has_more=len(items)>60,enumerated=True,scope=scope,unit=service.drawing['unit'],
                coverage='exact_labels_in_selected_title_border_candidate',name_counts={n:sum(normalized(i['text'])==normalized(n) for i in items) for n in args.names})
