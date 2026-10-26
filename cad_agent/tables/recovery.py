"""Local recovery with explicit provenance; visual guesses never become native text."""
from uuid import uuid4
from statistics import median
from shapely.geometry import box as shape_box, Point
from cad_agent.contracts import stable_id
from cad_agent.geometry.vision import VisionService, RenderRegion
from .topology import box, recover
from .service import fingerprint


def prepare(service, args, owner):
    candidate=None
    if args.candidate_id:
        candidate=next((c for c in service.documents()['candidates'] if c['candidate_id']==args.candidate_id),None)
        if candidate is None:raise ValueError('TABLE_CANDIDATE_NOT_FOUND')
        layout,bounds=candidate['layout_id'],candidate['bbox']
    else:layout,bounds=args.layout_id,args.bbox
    service.index.scope(layout,args.frame_id)
    if args.frame_id:
        frame=next(f for f in service.index.drawing['frames'] if f['frame_id']==args.frame_id)
        if frame.get('layout_id')!=layout or not shape_box(*frame['bbox']).covers(shape_box(*bounds)):
            raise ValueError('TABLE_RECOVERY_OUTSIDE_FRAME')
    if args.method=='source_geometry':
        cs,diagnostics=recover(service.index.scene,service.snapshot,layout,bounds,service.check,signature=service.signature)
        documents=[service.document(c) for c in cs if shape_box(*bounds).buffer(service.index.scene['texts'][0].get('height',1)*.01).covers(shape_box(*c['bbox']))]
        if candidate and not documents:
            texts={t['entity_id']:t for t in service.index.scene['texts']}
            rows=[[texts[eid] for eid in row] for row in candidate['rows']]
            h=median(t['height'] for row in rows for t in row)
            xs=[min(t['origin'][0] for t in col) for col in zip(*rows)]
            xs=[bounds[0]-h]+[(a+b)/2 for a,b in zip(xs,xs[1:])]+[bounds[2]+h]
            ys=[sum(t['origin'][1] for t in row)/len(row) for row in rows]
            ys=[bounds[3]+h]+[(a+b)/2 for a,b in zip(ys,ys[1:])]+[bounds[1]-h]
            tid=stable_id('tablev2',service.snapshot,candidate['candidate_id']);cells=[]
            for r,row in enumerate(rows):
                for col,t in enumerate(row):
                    a,b,c,d=xs[col],ys[r+1],xs[col+1],ys[r]
                    cells.append(dict(cell_id=stable_id('tcell',tid,str(r),str(col)),row=r,column=col,row_span=1,column_span=1,
                        bbox=[a,b,c,d],local_bbox=[a,b,c,d],boundary=[[a,b],[c,b],[c,d],[a,d]],edge_sources=[]))
            a,b,c,d=xs[0],ys[-1],xs[-1],ys[0]
            proposal=dict(table_id=tid,layout_id=layout,instance_path=candidate['instance_path'],source_kind='borderless',
                bbox=[a,b,c,d],local_bbox=[a,b,c,d],boundary=[[a,b],[c,b],[c,d],[a,d]],cells=cells,
                texts=[t|{'local_origin':t['origin']} for row in rows for t in row],
                transform=dict(origin=[0,0],angle=0,tolerance=h*.002,height=h))
            documents=[service.document(proposal)]
        return dict(table_ids=[d['table_id'] for d in documents],diagnostics=diagnostics,
                    structure_states=[d['structure_state'] for d in documents],scope_exhaustive=False)
    image=VisionService(service.w,service.snapshot).render(RenderRegion(layout_id=layout,bbox=bounds))
    record=dict(id='trecovery_'+uuid4().hex,owner_id=owner,snapshot_id=service.snapshot,
                signature=service.signature,layout_id=layout,bbox=bounds,image_id=image['image_id'],image=image)
    service.w.store.put('table_recovery',record['id'],record)
    return dict(recovery_id=record['id'],image=image,source_kind='visual',structure_state='candidate',
                note='逐格提出范围和原生文字 ID；无法对应原生文字的转录保留为视觉候选。')


def review(service,args,owner,sent_images):
    record=service.w.store.get('table_recovery',args.recovery_id)
    if record['owner_id']!=owner or record['snapshot_id']!=service.snapshot or record['signature']!=fingerprint(service.index):
        raise ValueError('TABLE_RECOVERY_SOURCE_CHANGED')
    if record['image_id'] not in sent_images:raise ValueError('TABLE_IMAGE_NOT_DELIVERED')
    bounds=shape_box(*record['bbox']);seen=set();cells=[];shapes=[]
    source={t['entity_id']:t for t in service.index.scene['texts'] if t['layout_id']==record['layout_id']}
    for i,c in enumerate(args.cells):
        a,b,z,w=c.bbox
        if a>=z or b>=w or not bounds.covers(shape_box(*c.bbox)):raise ValueError('TABLE_VISUAL_CELL_OUTSIDE_CROP')
        shape=shape_box(*c.bbox)
        if any(shape.intersection(other).area>1e-6 for other in shapes):raise ValueError('TABLE_VISUAL_CELLS_OVERLAP')
        shapes.append(shape);fragments=[]
        for eid in c.text_entity_ids:
            if eid in seen or eid not in source or not shape.covers(Point(source[eid]['origin'])):
                raise ValueError('TABLE_VISUAL_SOURCE_ASSIGNMENT_INVALID')
            seen.add(eid);t=source[eid]
            fragments.append(dict(entity_id=eid,raw_text=t['text'],evidence=service.index.entity_evidence(eid)))
        cells.append(c.model_dump()|dict(cell_id=stable_id('vcell',record['id'],str(i)),fragments=fragments,
                    source_state='source_bound' if fragments else 'image_transcription',structure_state='candidate',binding_state='candidate'))
    inventory={eid for eid,t in source.items() if bounds.covers(Point(t['origin']))}
    result=dict(recovery_id=record['id'],snapshot_id=service.snapshot,signature=service.signature,image_id=record['image_id'],
                cells=cells,unassigned_entity_ids=sorted(inventory-seen),structure_state='candidate',binding_state='candidate',
                scope_exhaustive=False,diagnostics=['VISUAL_RELATIONS_REQUIRE_REVIEW'])
    service.w.store.put('table_visual_review',record['id']+'_review',result)
    return result
