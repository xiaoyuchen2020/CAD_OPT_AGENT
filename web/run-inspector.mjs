// The inspector consumes persisted observations only. It never synthesizes reasoning.
const names = {search_sources:'检索图纸原文', read_source_context:'读取原文上下文', get_evidence:'核对原文证据',
  plan_task:'制定任务计划', finish_analysis:'校验并整理交付', get_drawing_overview:'读取图纸概览',
  render_region:'查看局部图像', read_table:'读取表格', calculate_facts:'核对计算', read_evidence_record:'读取归档结果'};
const statuses = {queued:'等待执行',running:'运行中',completed:'已完成',failed:'失败',cancelled:'已取消',interrupted:'未完成'};
const sectionOrder = ['request','instructions','context','history','arguments','execution','model_result','full_result','plan','response','draft','validation','answer','diagnostics'];
const shortLabels = {request:'模型输入',instructions:'系统提示词',context:'问题与范围',history:'历史上下文',
  arguments:'调用参数',execution:'执行参数',model_result:'回传结果',full_result:'完整结果',plan:'任务计划',
  response:'公开返回',draft:'待校验答案',validation:'校验反馈',answer:'交付内容',diagnostics:'执行诊断'};
const active = r => ['queued','running'].includes(r?.status);
const el = (tag, cls, text) => {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const button = (text, action, cls='ri-button') => {const b=el('button',cls,text);b.type='button';b.onclick=action;return b;};
const timeLabel = seconds => Number.isFinite(seconds) ? `${Math.max(0,seconds).toFixed(1)} s` : '未报告';

export function eventsForRun(run) {
  if (run?.journal_version) return (run.run_events || []).map(e =>
    !active(run)&&e.status==='running'?{...e,status:'interrupted',summary:'运行已结束；此步骤未收到完成记录。'}:e);
  return (run?.trace || run?.result?.trace || []).map((step,i)=>({
    id:`legacy-${i}`,sequence:i+1,kind:'tool',title:step.tool,tool:step.tool,status:step.status,
    duration_seconds:step.elapsed_seconds,result_summary:step.result_summary,legacy:step,sections:{},
  }));
}
export function visibleEvents(run, filter='all', query='', cursor=null) {
  return eventsForRun(run).filter((e,i)=>(cursor===null||i<=cursor)
    && (filter==='all'||(filter==='model'&&['model','input'].includes(e.kind))||e.kind===filter)
    && (!query || `${e.title} ${e.tool||''} ${e.summary||''} ${e.error_code||''}`.toLowerCase().includes(query.toLowerCase())));
}
export function runSummary(run) {
  const usage=run?.usage||run?.result?.usage||[];
  return {status:statuses[run?.status]||run?.status||'未选择',
    toolCalls:eventsForRun(run).filter(e=>e.kind==='tool').length,
    tokens:usage.length&&usage.every(u=>Number.isFinite(u.total_tokens))?usage.reduce((n,u)=>n+u.total_tokens,0):null,
    failedChecks:eventsForRun(run).filter(e=>e.kind==='validation'&&e.status==='failed').length};
}

export class RunInspector {
  constructor({workspace,api,onEvidence,onImage,onError,onCancel,onOpen}) {
    Object.assign(this,{workspace,api,onEvidence,onImage,onError,onCancel,onOpen});
    this.filter='all';this.query='';this.selected=undefined;this.sections=new Map();this.cache=new Map();
    this.rows=new Map();this.epoch=0;this.cursor=null;this.focus=false;this.visible=false;
    this.root=el('aside','run-inspector');this.root.hidden=true;this.root.setAttribute('aria-label','运行详情');
    const header=el('header','ri-header'), heading=el('div','ri-row');
    heading.append(el('h2',null,'运行详情'));this.status=el('span','ri-status');heading.append(this.status);
    this.focusButton=button('专注查看',()=>this.setFocus(!this.focus));
    heading.append(this.focusButton,button('关闭',()=>this.close()));
    this.question=el('p','ri-question');this.metrics=el('div','ri-metrics');this.stage=el('p','ri-note');this.stage.setAttribute('aria-live','polite');
    header.append(heading,this.question,this.metrics,this.stage);
    const toolbar=el('div','ri-toolbar');this.filters=el('div','ri-filters');
    for(const [key,label] of [['all','全部'],['model','模型'],['tool','工具'],['validation','校验']]){
      const b=button(label,()=>{this.filter=key;this.renderEvents();},'ri-filter');b.dataset.riFilter=key;this.filters.append(b);
    }
    this.search=el('input','ri-search');this.search.type='search';this.search.placeholder='搜索步骤';this.search.setAttribute('aria-label','搜索步骤名称');
    this.search.oninput=()=>{this.query=this.search.value;this.renderEvents();};
    this.replayButton=button('逐步回看',()=>{this.cursor=0;this.filter='all';this.query='';this.search.value='';this.selected=eventsForRun(this.run)[0]?.id;this.render();});
    this.cancelButton=button('停止分析',async()=>{this.cancelButton.disabled=true;try{await this.onCancel(this.run.id);await this.refresh();}catch(e){this.error(e);this.cancelButton.disabled=false;}});
    toolbar.append(this.filters,this.search,this.replayButton,this.cancelButton);
    this.playbar=el('div','ri-playbar');this.playbar.hidden=true;this.playLabel=el('span');
    this.nextButton=button('下一步 →',()=>{this.cursor=Math.min(eventsForRun(this.run).length-1,this.cursor+1);this.selected=eventsForRun(this.run)[this.cursor]?.id;this.render();});
    this.playbar.append(this.playLabel,button('退出回看',()=>{this.cursor=null;this.render();}),this.nextButton);
    this.list=el('div','ri-timeline');this.list.setAttribute('aria-label','运行步骤');
    this.footer=el('footer','ri-footer');this.footer.append(el('span',null,'公开记录 · 不含内部思维链'));
    this.historyButton=button('历史运行',()=>this.history());this.footer.append(this.historyButton);
    this.exportLink=el('a','ri-button','导出记录');this.exportLink.hidden=true;this.exportLink.setAttribute('download','cad-run-journal.jsonl');this.footer.append(this.exportLink);
    this.historyPanel=el('div','ri-history');this.historyPanel.hidden=true;
    this.root.append(header,toolbar,this.playbar,this.historyPanel,this.list,this.footer);workspace.append(this.root);
  }
  error(e){this.onError?.(e.message||String(e));}
  setFocus(value){this.focus=value;this.workspace.classList.toggle('inspector-focus',value&&this.visible);this.focusButton.textContent=value?'返回图纸':'专注查看';}
  close(){this.visible=false;this.root.hidden=true;this.workspace.classList.remove('inspector-open','inspector-focus');clearTimeout(this.timer);}
  reset(snapshotId){this.close();this.epoch++;this.snapshotId=snapshotId;this.run=null;this.rows.clear();this.cache.clear();this.list.replaceChildren();this.historyPanel.hidden=true;
    this.status.textContent='未选择';this.question.textContent='请选择一次历史运行';this.metrics.replaceChildren();this.stage.textContent='';this.playbar.hidden=true;this.cancelButton.hidden=true;this.replayButton.disabled=true;this.exportLink.hidden=true;}
  async showHistory(snapshotId){if(this.snapshotId!==snapshotId)this.reset(snapshotId);this.show();await this.history();}
  show(){this.visible=true;this.root.hidden=false;this.workspace.classList.add('inspector-open');this.setFocus(this.focus);}
  async open(run){
    const id=typeof run==='string'?run:run.id;
    const epoch=++this.epoch;clearTimeout(this.timer);
    if(this.run?.id!==id){this.selected=undefined;this.cursor=null;this.sections.clear();this.cache.clear();this.rows.clear();this.list.replaceChildren();}
    this.show();this.historyPanel.hidden=true;
    try{const value=typeof run==='string'?await this.api(`/api/chat-runs/${encodeURIComponent(id)}`):run;
      if(epoch!==this.epoch)return;this.run=value;this.snapshotId=value.snapshot_id;this.render();this.onOpen?.(value);this.schedule();
    }catch(e){if(epoch===this.epoch)this.error(e);}
  }
  update(run){if(this.run?.id===run.id){const ended=active(this.run)&&!active(run);this.run=run;if(this.visible){this.render();if(ended)this.onOpen?.(run);}}}
  schedule(){clearTimeout(this.timer);if(this.visible&&active(this.run))this.timer=setTimeout(()=>this.refresh(),1100);}
  async refresh(){const epoch=this.epoch,id=this.run?.id;if(!id)return;
    try{const r=await this.api(`/api/chat-runs/${encodeURIComponent(id)}`);if(epoch!==this.epoch)return;this.update(r);}
    catch(e){if(epoch===this.epoch)this.stage.textContent=`刷新失败：${e.message}；保留已读记录。`;}
    finally{if(epoch===this.epoch)this.schedule();}
  }
  async history(offset=0){
    if(!this.snapshotId)return;const epoch=this.epoch,sid=this.snapshotId;this.historyPanel.hidden=false;
    if(!offset)this.historyPanel.replaceChildren(el('p','ri-note','正在读取历史运行…'));
    try{const data=await this.api(`/api/drawings/${encodeURIComponent(sid)}/chat-runs?offset=${offset}&limit=20`);
      if(epoch!==this.epoch||sid!==this.snapshotId)return;
      if(!offset)this.historyPanel.replaceChildren(el('h3',null,'当前图纸的运行记录'));
      if(!data.items.length&&!offset)this.historyPanel.append(el('p','ri-note','还没有模型运行记录。'));
      for(const item of data.items){const b=button(`${statuses[item.status]||item.status} · ${item.question}`,()=>this.open(item.id),'ri-history-item');
        b.append(el('small',null,new Date(item.created_at).toLocaleString()));this.historyPanel.append(b);}
      if(data.has_more){const more=button('加载更早记录',()=>{more.remove();this.history(offset+data.items.length);});this.historyPanel.append(more);}
    }catch(e){if(epoch===this.epoch)this.historyPanel.replaceChildren(el('p','ri-error',e.message));}
  }
  render(){if(!this.run)return;
    const r=this.run,s=runSummary(r),events=eventsForRun(r);
    this.question.textContent=r.request?.question||'';this.status.textContent=s.status;this.status.dataset.status=r.status;
    this.stage.textContent=r.error?.message||r.stage||'';
    this.metrics.replaceChildren(...[r.model||r.result?.model||'模型未报告',`${s.toolCalls} 次工具调用`,
      `${s.failedChecks} 次校验未通过`,s.tokens===null?'Token 未报告':`${s.tokens.toLocaleString()} tokens`,
      r.result?.elapsed_seconds!=null?timeLabel(r.result.elapsed_seconds):active(r)?'进行中耗时见步骤':'总耗时未报告'].map(v=>el('span',null,v)));
    this.cancelButton.hidden=!active(r);this.cancelButton.disabled=!!r.cancel_requested;
    this.cancelButton.textContent=r.cancel_requested?'正在停止…':'停止分析';
    this.replayButton.disabled=active(r)||!events.length;
    this.exportLink.hidden=false;this.exportLink.href=`/api/chat-runs/${encodeURIComponent(r.id)}/journal-export`;
    this.playbar.hidden=this.cursor===null;
    this.playLabel.textContent=`记录回看 ${this.cursor===null?0:this.cursor+1} / ${events.length} · 展示已保存的最终步骤状态`;
    this.nextButton.disabled=this.cursor>=events.length-1;
    this.renderEvents();
  }
  renderEvents(){
    for(const b of this.filters.children)b.setAttribute('aria-pressed',String(b.dataset.riFilter===this.filter));
    const events=visibleEvents(this.run,this.filter,this.query,this.cursor),all=eventsForRun(this.run);
    if(this.selected===undefined&&events.length)this.selected=events.find(e=>e.kind==='tool')?.id||events[0].id;
    const visibleIds=new Set(events.map(e=>e.id));
    this.list.querySelector('.ri-empty')?.remove();
    for(const row of this.rows.values())row.node.hidden=true;
    for(const e of all){let row=this.rows.get(e.id);
      if(!row){const n=el('article','ri-event'),head=button('',()=>{this.selected=this.selected===e.id?null:e.id;this.renderEvents();},'ri-event-head');
        const mark=el('span','ri-dot'),label=el('span','ri-event-label'),title=el('strong'),meta=el('small'),time=el('span','ri-time'),chevron=el('span','ri-chevron');label.append(title,meta);head.append(mark,label,time,chevron);
        const detail=el('div','ri-detail');detail.id=`ri-detail-${e.id}`;head.setAttribute('aria-controls',detail.id);n.append(head,detail);this.list.append(n);
        row={node:n,head,mark,title,meta,time,chevron,detail};this.rows.set(e.id,row);}
      row.node.hidden=!visibleIds.has(e.id);row.title.textContent=names[e.tool]||e.title;
      row.mark.textContent=e.status==='completed'?'✓':e.status==='running'?'…':e.status==='failed'?'!':'–';row.mark.dataset.status=e.status;
      row.meta.textContent=[e.tool||e.kind,statuses[e.status]||e.status,e.result_summary?.total!=null?`${e.result_summary.total} 条匹配`:null,e.error?.message||e.summary].filter(Boolean).join(' · ');
      row.time.textContent=timeLabel(e.duration_seconds);row.chevron.textContent=this.selected===e.id?'⌄':'›';
      row.head.setAttribute('aria-expanded',String(this.selected===e.id));row.detail.hidden=this.selected!==e.id;
      if(this.selected===e.id){const signature=JSON.stringify([e.sections,e.status,e.evidence_ids,e.image_inputs,this.sections.get(e.id)]);
        if(row.signature!==signature){row.signature=signature;this.renderDetail(e,row.detail);}}
    }
    if(!events.length)this.list.append(el('p','ri-empty',all.length?'没有符合筛选条件的步骤。':this.run?.journal_version?'等待第一条运行记录…':'旧运行未记录详细过程；无法补回历史提示词或完整结果。'));
  }
  renderDetail(event,target){
    target.replaceChildren();
    if(event.legacy){target.append(el('p','ri-note','旧版运行：只保留原有 trace，不补写缺失信息。'),el('pre',null,JSON.stringify(event.legacy,null,2)));return;}
    const note=event.kind==='model'?(event.capture_level==='wire_payload'?'已记录实际模型请求；私密字段与图像字节已移除。':event.capture_level==='managed_runtime_input'?'记录应用提交给托管运行时的内容；内部逐轮请求不可见。':'记录应用传入模型适配器的内容。'):
      event.kind==='tool'?'工具执行成功不代表工程结论成立。回传结果与完整归档可能不同。':
      event.kind==='validation'?'仅展示程序实际执行的校验；未执行的工程检查不视为通过。':'应用运行记录';
    target.append(el('p','ri-detail-note',note));
    const keys=Object.keys(event.sections||{}).sort((a,b)=>sectionOrder.indexOf(a)-sectionOrder.indexOf(b));
    if(!keys.length){target.append(el('p','ri-note','此步骤尚未保存详情。'));return;}
    const selected=keys.includes(this.sections.get(event.id))?this.sections.get(event.id):
      keys.includes('validation')?'validation':keys.includes('model_result')?'model_result':keys[0];
    const tabs=el('div','ri-tabs');tabs.setAttribute('role','group');tabs.setAttribute('aria-label','记录内容');
    for(const key of keys){const b=button(shortLabels[key]||key,()=>{this.sections.set(event.id,key);this.renderEvents();},'ri-tab');b.setAttribute('aria-pressed',String(key===selected));tabs.append(b);}target.append(tabs);
    const body=el('div','ri-record');target.append(body);this.loadSection(event,selected,body);
    if(event.kind==='model')target.append(el('p','ri-note','决策说明仅在模型公开返回中存在时展示；缺失时不作推测。'));
    if(event.evidence_ids?.length){const links=el('details','ri-evidence');links.append(el('summary',null,`定位本步原文证据（${event.evidence_ids.length}）`));
      for(const id of event.evidence_ids)links.append(button(id,async()=>{try{const epoch=this.epoch;const source=await this.api(`/api/chat-runs/${this.run.id}/journal-evidence/${encodeURIComponent(id)}`);if(epoch!==this.epoch)return;this.setFocus(false);await this.onEvidence(source);}catch(e){this.error(e);}},'ri-evidence-button'));target.append(links);}
    for(const image of event.image_inputs||[])target.append(button('查看模型输入图像',async()=>{try{this.setFocus(false);await this.onImage(this.run.snapshot_id,image.image_id);}catch(e){this.error(e);}}));
  }
  async loadSection(event,name,body,start=0){
    const epoch=this.epoch,runId=this.run.id,ref=event.sections[name];
    const key=`${runId}/${event.id}/${name}/${ref.sha256}/${start}`;
    const status=el('p','ri-note','正在读取…');if(!start)body.replaceChildren();body.append(status);
    try{let page=this.cache.get(key);if(!page){page=await this.api(`/api/chat-runs/${runId}/events/${event.id}/sections/${name}?start=${start}`);if(this.cache.size>=80)this.cache.delete(this.cache.keys().next().value);this.cache.set(key,page);}
      if(epoch!==this.epoch||!body.isConnected)return;status.remove();
      const pre=body.querySelector('pre')||el('pre');pre.textContent+=page.content;if(!pre.isConnected)body.append(pre);
      if(!start){const copy=button('复制已加载内容',async()=>{try{await navigator.clipboard.writeText(pre.textContent);copy.textContent='已复制';}catch(e){this.error(e);}});body.append(copy);}
      const range=el('p','ri-note',`已加载 ${page.end.toLocaleString()} / ${page.total_chars.toLocaleString()} 字符${page.truncated?' · 原记录超过上限，尾部未保存':''}`);body.append(range);
      if(page.has_more){const more=button('加载下一段',()=>{more.remove();range.remove();this.loadSection(event,name,body,page.next_start);});body.append(more);}
    }catch(e){if(epoch===this.epoch&&body.isConnected){status.textContent=`读取失败：${e.message}`;body.append(button('重试',()=>{status.remove();this.loadSection(event,name,body,start);}));}}
  }
}
