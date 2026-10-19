// Pure presentation contract: label boxes never stand in for room boundaries.
export function roomBatchPresentation(batch) {
  const labels=[],boundaries=[],numbers=[];
  for(const item of batch.items || []) {
    const evidence=item.label_evidence;
    if(evidence?.bbox) {
      labels.push({bbox:evidence.bbox});
      numbers.push({number:item.number,bbox:evidence.bbox});
    }
    const h=item.highlight;
    if(h?.kind==='candidate_boundary' && h.boundary?.length)
      boundaries.push({boundary:h.boundary,holes:h.holes || [],gaps:h.gaps || []});
  }
  return {labels,boundaries,numbers,layout_id:batch.scope?.layout_id,
    countText:!batch.scope ? '尚未执行名称枚举，候选数量未知。' : `${batch.total} 个匹配名称候选；不是已确认的房间总数。`,
    unitPending:batch.status==='needs_unit_confirmation',
    note:!labels.length ? '尚无可高亮的名称定位。' : boundaries.length ? `文字编号与候选边界已高亮；橙色虚线为开口闭合假设，净边界仍需核查。${batch.unit==='unknown' ? '面积换算还需明确单位。' : ''}` : '已高亮名称文字位置；这些框不代表房间边界。'};
}

export function roomAreaText(item) {
  if(item.status==='needs_unit_confirmation') return '等待确认单位';
  if(item.status==='measured' && item.area_m2!=null) return `净面积候选 ${Number(item.area_m2.toFixed(2))} m²`;
  if(item.candidate_area_m2!=null) return `围合面积候选 ${Number(item.candidate_area_m2.toFixed(2))} m²（待复核）`;
  return item.status==='not_measured' ? '本批次未测量（时间限制）' : '无法确定面积';
}

// Merge only already-delivered pages in one source/frame for initial highlights.
export function combinedBatchHighlights(batches) {
  const groups=new Map();
  for(const batch of batches) {
    if(!batch.scope)continue;
    const key=[batch.snapshot_id,batch.scope.layout_id,batch.scope.frame_id].join(':');
    if(!groups.has(key))groups.set(key,{...batch,items:[],seen:new Set()});
    const group=groups.get(key);
    for(const item of batch.items)if(!group.seen.has(item.region_id)) {group.seen.add(item.region_id);group.items.push(item);}
  }
  return [...groups.values()].map(({seen,...batch})=>batch);
}

export function displayedBatches(result) {
  const batches=result.room_batches || (result.room_batch?[result.room_batch]:[]);
  const action=result.highlight_action;
  if(action?.mode!=='replace')return batches;
  if(action.kind==='geometry')return [];
  return batches.filter(b=>b.batch_id===action.batch_id).map(b=>({...b,items:action.items,
    total:action.items.length,has_more:false,next_offset:null,selection:true}));
}
