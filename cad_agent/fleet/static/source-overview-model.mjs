export const drawingKey=s=>JSON.stringify([s.node_id,s.snapshot_id,s.source_sha256]);
export const stageLabel={read:'已提取',delivered:'已交付',packaged:'已发布事实包',analysis_input:'已选入分析'};
export function filterSources(items,{query='',stage='',rule='',workflow='',drawing='',layout='',missing=false}={}){
  const needle=query.trim().toLocaleLowerCase();
  return items.map(row=>{
    const associations=row.associations.filter(a=>(!rule||a.rule_ref===rule)&&(!workflow||a.workflow_id===workflow));
    return {...row,selected:associations.some(a=>['workflow_input','field_selection'].includes(a.selection_kind)),visibleAssociations:associations};
  }).filter(r=>(!drawing||drawingKey(r.source)===drawing)&&(!layout||r.source.layout_id===layout)
    &&(!missing||!r.locatable)&&(!rule&&!workflow||r.visibleAssociations.length)
    &&(!stage||(stage==='pending'?r.issues.length:stage==='analysis_input'?r.selected:r.stages.includes(stage)))
    &&(!needle||JSON.stringify([r.source.quote,r.data,r.associations,r.issues]).toLocaleLowerCase().includes(needle)));
}
export function groupedDrawings(items){
  const rows=new Map();for(const r of items){if(!r.source.node_id||!r.source.snapshot_id)continue;
    const key=drawingKey(r.source);if(!rows.has(key))rows.set(key,{key,source:r.source,count:0,missing:0,layouts:new Set()});
    const d=rows.get(key);d.count++;d.missing+=!r.locatable;if(r.source.layout_id)d.layouts.add(r.source.layout_id);
  }return [...rows.values()];
}
export function changes(previous,next){
  const known=new Set(previous?.items.map(r=>r.source_id)||[]);
  return next.items.filter(r=>!known.has(r.source_id)).length;
}
