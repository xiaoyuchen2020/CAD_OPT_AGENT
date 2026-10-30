import {drawingKey,filterSources,groupedDrawings,stageLabel,changes} from './source-overview-model.mjs';
const $=id=>document.getElementById(id),params=new URLSearchParams(location.search);
const state={jobs:[],view:null,job:params.get('job'),attempt:params.get('attempt')||'',drawing:null,selected:null,missing:false,epoch:0,frameNode:null,ready:false,request:0,pending:null,focus:null,limit:50,busy:false};
const layoutNames=new Map();let locationTimer;
const labels={running:'运行中',completed:'运行完成',failed:'执行失败',cancelled:'已取消',queued:'待执行',partial:'部分交付',complete:'完整交付',interrupted:'已中断',waiting_evidence:'等待补证',waiting_input:'等待确认'};
const label=v=>labels[v]||v||'未交付';
const text=v=>typeof v==='string'?v:JSON.stringify(v,null,2);
const el=(tag,value,cls)=>{const n=document.createElement(tag);if(value!==undefined)n.textContent=String(value);if(cls)n.className=cls;return n;};
const button=(value,fn,cls)=>{const b=el('button',value,cls);b.type='button';b.onclick=fn;return b;};
function notice(s=''){$('notice').textContent=s;$('notice').hidden=!s;}
async function api(path){const r=await fetch('/source-overview/api/'+path,{cache:'no-store',credentials:'same-origin'});const v=await r.json();if(!r.ok)throw Error(r.status===401?'登录已过期，请返回任务中心登录':v.detail||'读取失败');return v;}
const act=fn=>(...args)=>Promise.resolve().then(()=>fn(...args)).catch(e=>notice(e.message));
function options(id,values,placeholder){const select=$(id),old=select.value;select.replaceChildren(...(placeholder?[new Option(placeholder,'')]:[]),...values.map(([value,name])=>new Option(name,value)));if([...select.options].some(o=>o.value===old))select.value=old;}
function filters(withDrawing=true){return{query:$('search').value,stage:$('stage').value,rule:$('rule').value,workflow:$('workflow').value,drawing:withDrawing&&!state.missing?state.drawing||'':'',layout:withDrawing&&!state.missing?$('layout').value:'',missing:state.missing};}
function save(){const p=new URLSearchParams({job:state.job||'',scope:$('scope').value});if(state.attempt)p.set('attempt',state.attempt);if($('rule').value)p.set('rule',$('rule').value);if($('workflow').value)p.set('workflow',$('workflow').value);history.replaceState(null,'','?'+p);}
function renderDrawings(){
  const groups=groupedDrawings(state.view.items),body=$('drawing-list');body.replaceChildren();
  const matched=filterSources(state.view.items,filters(false));
  for(const d of groups){const count=matched.filter(r=>drawingKey(r.source)===d.key).length;
    const b=button(d.source.drawing_name||d.source.snapshot_id,()=>chooseDrawing(d.key));b.setAttribute('aria-pressed',String(d.key===state.drawing&&!state.missing));
    b.append(el('small',`${count} / ${d.count} 处 · ${d.missing} 项待定位`),el('small',`节点 ${(d.source.node_id||'?').toUpperCase()} · 版本 ${(d.source.source_sha256||'未知').slice(0,8)}`));body.append(b);
  }
  const missing=state.view.items.filter(r=>!r.locatable).length;$('missing').textContent=`待定位来源 · ${missing} 项`;$('missing').setAttribute('aria-pressed',String(state.missing));
  const current=groups.find(d=>d.key===state.drawing);
  options('layout',[...(current?.layouts||[])].map(l=>[l,layoutNames.get(l)||'来源布局 '+String([...current.layouts].indexOf(l)+1)]));
}
function chooseDrawing(key){state.missing=false;state.drawing=key;state.limit=50;state.selected=null;$('layout').value='';renderDrawings();renderSources();sendOverlay();}
function renderDetail(){
  const body=$('selected');body.replaceChildren();const row=state.view.items.find(r=>r.source_id===state.selected);if(!row)return;
  const source=row.source;
  body.append(el('h3',row.data[0]?.text||'原文来源'));
  for(const s of row.stages)body.append(el('span',stageLabel[s]||s,'badge'));
  body.append(el('blockquote',source.quote||'几何来源，无文字引文'));
  if(source.measurement_line)body.append(el('p',`局部量测 · 原始单位 ${source.unit||'unknown'} · 工程身份未确认`,'issue'));
  for(const issue of row.issues)body.append(el('p',text(issue),'issue'));
  const precision={table_cell:'表格单元格',entity:'实体级估算',measurement_line:'量测线',table_row:'表格行'}[source.precision]||'原文区域估算';
  body.append(el('p',`${source.drawing_name||source.snapshot_id||'图纸未知'} · ${layoutNames.get(source.layout_id)||'来源布局'} · ${precision}`,'muted'));
  const locate=button(row.locatable?'定位原图':'暂无可靠位置',()=>select(row.source_id,true));locate.disabled=!row.locatable;body.append(locate);
  if(source.location_note)body.append(el('p',source.location_note,'issue'));
  if(row.data.length){body.append(el('h3','提取数据'));const list=el('ul',undefined,'detail-list');for(const d of row.data)list.append(el('li',[d.text,d.value!==undefined?'值：'+d.value:'',d.unit?'单位：'+d.unit:'',d.conditions?.length?'条件：'+text(d.conditions):''].filter(Boolean).join(' · ')));body.append(list);}
  if(row.associations.length){body.append(el('h3','事实包与规则输入'));const list=el('ul',undefined,'detail-list');
    for(const a of row.associations){const item=el('li');
      item.append(el('span',a.rule_id?`${a.rule_id} · ${a.selection_kind==='field_selection'?'字段选择：'+a.field:'分析输入包'} · ${label(a.workflow_status)}`:'已发布事实包'));
      if(a.workflow_id)item.append(el('div',a.workflow_id,'muted'));
      if(a.rule_ref)item.append(button('筛选此规则',()=>{$('rule').value=a.rule_ref;$('workflow').value='';applyFilters();},'relation'));
      if(a.package_id)item.append(el('div',a.package_id,'muted'));if(a.review_reason)item.append(el('div',a.review_reason));list.append(item);
    }body.append(list,el('p','进入分析输入包表示该轮可用的输入，不表示模型实际引用了其中每一项。','muted'));
  }
  for(const id of row.related_ids||[]){const other=state.view.items.find(r=>r.source_id===id);if(other)body.append(button('关联来源：'+(other.source.quote||id).slice(0,35),()=>select(id,true),'relation'));}
  const details=el('details');details.append(el('summary','来源版本、实体与执行批次'),el('pre',text({来源编号:row.source_id,图纸哈希:source.source_sha256,布局:source.layout_id,实体:source.entity_ids,字符范围:source.text_span,坐标:source.bbox,坐标单位:source.unit,测线:source.measurement_line,来源批次:row.origins})));body.append(details);
}
function renderSources(){
  if(!state.view)return;const rows=filterSources(state.view.items,filters());
  $('list-heading').textContent=`${state.missing?'待定位':'当前筛选'} · ${rows.length} 处来源`;
  $('sources').replaceChildren(...rows.slice(0,state.limit).map(row=>{
    const b=button((row.source.quote||row.data[0]?.text||'几何来源').slice(0,140),()=>select(row.source_id,false),'source-row');
    b.setAttribute('aria-pressed',String(row.source_id===state.selected));b.append(el('small',`${row.selected?'已选入分析':'已提取'}${row.issues.length?' · 待核对':''} · ${row.origins.length} 条来源记录`));return b;
  }));$('more').hidden=rows.length<=state.limit;renderDetail();
  const c=state.view.counts;$('counts').textContent=`范围内 ${c.sources} 处来源 · ${c.locatable} 处有归档坐标 · ${c.sources-c.locatable} 项待定位 · ${c.data_items} 条数据陈述`;
  const gaps=state.view.gaps.filter(g=>(!$('workflow').value||g.workflow_id===$('workflow').value)&&(!$('rule').value||g.rule_id===$('rule').selectedOptions[0]?.textContent));
  $('gap-count').textContent=String(gaps.length);$('gap-list').replaceChildren(...gaps.map(g=>el('p',text(g.description))));
  $('warning-count').textContent=String(state.view.warnings.length);$('warning-list').replaceChildren(...state.view.warnings.map(w=>el('p',w)));
}
function select(id,zoom){const row=state.view.items.find(r=>r.source_id===id);if(!row)return;state.selected=id;
  if(row.locatable){const key=drawingKey(row.source),changed=state.drawing!==key||$('layout').value!==row.source.layout_id||state.missing;
    state.missing=false;state.drawing=key;renderDrawings();$('layout').value=row.source.layout_id;
    // Explicitly locating a source outside filters clears only the conflicting filters.
    if(!filterSources([row],filters()).length){$('stage').value='';$('rule').value='';$('workflow').value='';$('search').value='';}
    if(changed){state.focus={source_id:id,zoom};sendOverlay();}
    else if(state.ready)$('drawing').contentWindow.postMessage({type:'cad-sources:focus',source_id:id,zoom},location.origin);
  }renderSources();save();
}
function sendPending(){if(state.ready&&state.pending)$('drawing').contentWindow.postMessage(state.pending,location.origin);}
function sendOverlay(){
  if(!state.view)return;
  const d=groupedDrawings(state.view.items).find(d=>d.key===state.drawing);
  if(!d||!$('layout').value){$('drawing').hidden=true;$('empty').hidden=false;$('empty').textContent='当前范围没有可定位的图纸来源。';state.pending=null;return;}
  const source=d.source,rows=filterSources(state.view.items,{...filters(),missing:false}).filter(r=>r.locatable);
  state.pending={type:'cad-sources:set',request_id:String(++state.request),snapshot_id:source.snapshot_id,source_sha256:source.source_sha256,layout_id:$('layout').value,items:rows,enabled:$('overlay').checked,selected:state.selected};
  $('drawing').hidden=false;$('empty').hidden=true;
  if(state.frameNode!==source.node_id){state.frameNode=source.node_id;state.ready=false;$('drawing').src='/source-overview/nodes/'+encodeURIComponent(source.node_id)+'/?'+new URLSearchParams({snapshot_id:source.snapshot_id,review:'1',sources:'1'});}
  $('location-status').textContent='正在同步来源高亮…';clearTimeout(locationTimer);const request=state.request;
  locationTimer=setTimeout(()=>{if(request===state.request)$('location-status').textContent='原图节点未响应，来源文字仍可查阅；可刷新重试。';},25000);sendPending();
}
window.addEventListener('message',event=>{
  if(event.origin!==location.origin||event.source!==$('drawing').contentWindow)return;
  const m=event.data;if(m?.type==='cad-sources:ready'){state.ready=true;sendPending();return;}
  if(!state.pending||m?.request_id!==state.pending.request_id)return;
  if(m.type==='cad-sources:selected'){state.selected=m.source_id;renderSources();}
  if(m.type==='cad-sources:loaded'){
    clearTimeout(locationTimer);for(const l of m.layouts||[])layoutNames.set(l.layout_id,l.name);renderDrawings();
    if(m.rejected.length)notice(`${m.rejected.length} 项未通过当前图纸位置校验，已拒绝高亮。`);
    if(state.focus){$('drawing').contentWindow.postMessage({type:'cad-sources:focus',...state.focus},location.origin);state.focus=null;}
  }
  if(m.type==='cad-sources:viewport')$('location-status').textContent=`本布局 ${m.total} 处 · 当前视野 ${m.visible} 处 · 绘制 ${m.marks} 组${m.marks<m.visible?'（聚合标记可点击展开）':''}`;
  if(m.type==='cad-sources:error'){clearTimeout(locationTimer);$('location-status').textContent=m.message;notice(m.message);}
  if(m.type==='cad-sources:cluster'){$('location-status').textContent=`已展开 ${m.source_ids.length} 处相邻来源`;}
});
function applyFilters(){state.limit=50;renderDrawings();renderSources();sendOverlay();save();}
async function refresh(reset=false){
  const epoch=++state.epoch;state.busy=true;$('refresh').disabled=true;
  try{
    if(!state.jobs.length||reset){const result=await api('jobs');if(epoch!==state.epoch)return;state.jobs=result.items;
      options('job',state.jobs.map(j=>[j.id,`${label(j.status)} · ${j.question||j.id}`]));state.job=state.job||state.jobs[0]?.id;$('job').value=state.job||'';}
    if(!state.job){$('empty').textContent='还没有可查看的任务。';return;}
    const p=new URLSearchParams({scope:$('scope').value,history:String($('history').checked)});if(state.attempt)p.set('attempt_id',state.attempt);
    const view=await api('jobs/'+encodeURIComponent(state.job)+'?'+p);if(epoch!==state.epoch)return;
    notice();const count=state.view?changes(state.view,view):0;state.view=view;
    options('attempt',[['','跟随当前执行批次'],...view.attempts.slice().reverse().map(a=>[a.id,`${label(a.status)} · ${a.id.slice(0,8)}`])]);$('attempt').value=state.attempt;
    $('scope').options[1].disabled=!view.project_id;
    const rules=new Map();for(const row of view.items)for(const a of row.associations)if(a.rule_ref)rules.set(a.rule_ref,a.rule_id||a.rule_ref);
    options('rule',[...rules],'全部规则');options('workflow',view.rules.map(r=>[r.workflow_id,`${r.rule_id} · ${label(r.workflow_status)} · ${r.workflow_id.slice(0,12)}`]),'全部轮次（含历史）');
    if(reset){if(rules.has(params.get('rule')))$('rule').value=params.get('rule');if(view.rules.some(r=>r.workflow_id===params.get('workflow')))$('workflow').value=params.get('workflow');}
    const groups=groupedDrawings(view.items);if(!groups.some(d=>d.key===state.drawing))state.drawing=groups[0]?.key||null;
    if(!view.items.some(r=>r.source_id===state.selected))state.selected=null;
    $('task-status').textContent=`${view.scope==='project'?'同项目来源':'当前任务'} · 执行：${label(view.execution_status)} · 交付：${label(view.delivery_status)}`;
    $('sync-status').textContent=`${new Date(view.updated_at).toLocaleTimeString('zh-CN')} 更新${count?' · 新增 '+count+' 处来源':''}${view.stale?' · 正在同步，保留上次结果':''}${view.live_status.some(s=>!s.complete)?' · 实时记录同步中':''}`;
    $('results-link').href='/results.html?'+new URLSearchParams({job:state.job,...(state.attempt?{attempt:state.attempt}:{})});
    renderDrawings();renderSources();sendOverlay();save();
  }finally{if(epoch===state.epoch){state.busy=false;$('refresh').disabled=false;}}
}
$('scope').value=params.get('scope')==='project'?'project':'task';
$('job').onchange=act(()=>{state.job=$('job').value;state.attempt='';state.selected=null;state.view=null;return refresh(true);});
$('scope').onchange=act(()=>refresh());$('attempt').onchange=act(()=>{state.attempt=$('attempt').value;state.selected=null;return refresh();});$('history').onchange=act(()=>refresh());
$('refresh').onclick=act(()=>refresh(true));$('missing').onclick=()=>{state.missing=true;state.limit=50;renderDrawings();renderSources();};
for(const id of ['stage','rule','workflow','layout','overlay'])$(id).onchange=applyFilters;
$('search').oninput=applyFilters;$('more').onclick=()=>{state.limit+=50;renderSources();};
setInterval(()=>{if($('auto').checked&&!document.hidden&&!state.busy)act(()=>refresh())();},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&$('auto').checked&&!state.busy)act(()=>refresh())();});
await act(()=>refresh(true))();
