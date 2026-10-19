// A public row refers to actual reviewed geometry, never a text-location box.
export function spatialHighlightBounds(action) {
  const points=[...(action.paths || []),...(action.regions || []).map(r=>r.boundary || [])].flat();
  if(!points.length)return action.scope.bbox;
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
  return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
}
export function spatialRows(result) {
  const reviews=result.spatial_results || result.task?.spatial_results || [];
  const action=result.highlight_action || result.task?.highlight_action;
  const types={open:'敞开界面候选',door:'经门洞连接候选',wall:'隔断边界候选',uncertain:'尚未确定',not_plan_space:'排除：非平面空间'};
  return reviews.flatMap(review=>{
    if(review.kind==='connection_review')return review.items.map(i=>({
      key:i.item_id,label:`${i.number}. ${i.labels.join(' ↔ ')}`,
      classification:types[i.decision.classification],reason:i.decision.reason,
      measurement:(i.selected_measurements || []).map(m=>`${m.value} m（图上门垛间距）`).join('；') || '—',
      evidence_url:review.evidence_url,
    }));
    if(review.kind==='discovery_review') {
      const byId=new Map((review.selected || []).map(i=>[i.item_id,i]));
      const selected=action?.kind==='geometry' && action.mode==='replace' ? (action.object_ids || []).filter(id=>byId.has(id)).map(id=>byId.get(id)) : (review.selected || []);
      return selected.map(i=>({
      key:i.item_id,label:`空间候选 ${i.number}`,classification:review.ranking_complete?'已检查集合内排序':'部分筛选，排名未完成',
      reason:i.decision.reason,
      measurement:`${i.area_m2} m² · 外环周长 ${i.perimeter_m} m`+(i.area_annotation_checks || []).filter(c=>c.status==='unresolved_area_difference').map(c=>`；轮廓内标注 ${c.annotation_area_m2} m²，与几何差 ${c.difference_m2} m²，关联与口径待核查`).join(''),evidence_url:review.evidence_url,
    }));
    }
    return [];
  });
}
