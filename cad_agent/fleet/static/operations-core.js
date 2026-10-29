/* Small, side-effect-free presentation helpers shared with the Node tests. */
(function (root) {
  'use strict';
  const statuses = {queued:'等待执行',running:'运行中',pause_requested:'等待暂停',paused:'已暂停',resume_requested:'等待继续',completed:'运行结束',failed:'失败',cancelled:'已取消',cancel_requested:'取消中',partial:'部分完成',waiting_input:'等待资料',idle:'空闲',busy:'执行中',offline:'离线',pending:'待生效',applied:'已生效',rejected:'已拒绝',delivered:'已送达',accepted:'已接收',terminal:'已结束',unknown:'未知',in_flight:'交互中',waiting:'等待中',active:'执行中',succeeded:'成功',stale:'数据过期',waiting_activity:'等待活动任务',budget_exhausted:'监督预算已用完',interrupted:'检查已中断',disabled:'监督已关闭'};
  const kinds = {coordinator:'协调器',worker:'执行节点',agent:'Agent',tool:'工具',model:'模型',case:'规则实例',exploration:'探索运行',job:'任务',human:'人工引导',supervisor:'监督模型'};
  const fields = {
    'budget.max_rounds': [1,128], 'budget.max_tool_calls':[1,512], 'budget.timeout_seconds':[1,7200], 'budget.max_reported_tokens':[1,10000000],
    'sandbox.timeout_seconds':[1,300], 'sandbox.memory_mb':[64,512], 'sandbox.max_output_bytes':[1024,2097152],
    'validator.min_evidence_count':[0,20], 'dispatch_budget.max_case_starts':[1,10000], 'dispatch_budget.max_concurrent_cases':[1,100], 'dispatch_budget.max_reported_tokens':[1,100000000]
  };
  function knownNumber(value) {return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;}
  function number(value, suffix='') {const n=knownNumber(value);return n===null?'未报告':n.toLocaleString('zh-CN',{maximumFractionDigits:1})+suffix;}
  function duration(value) {const n=knownNumber(value);if(n===null)return '未报告';if(n<60)return `${Math.floor(n)} 秒`;if(n<3600)return `${Math.floor(n/60)} 分 ${Math.floor(n%60)} 秒`;return `${Math.floor(n/3600)} 小时 ${Math.floor(n%3600/60)} 分`;}
  function bytes(value) {const n=knownNumber(value);if(n===null)return '未报告';return n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`;}
  function usage(value={}) {const total=knownNumber(value.total_tokens),reported=knownNumber(value.reported_tokens);return {total:value.known===false||total===null?'用量未完整报告':number(total),reported:number(reported),model_calls:number(value.model_calls),tool_calls:number(value.tool_calls),cost:knownNumber(value.cost)===null?'费用未报告':number(value.cost)};}
  function label(value) {return statuses[value]||String(value||'未知');}
  function safePath(value) {return typeof value==='string' && value.startsWith('/') && !value.startsWith('//') && !value.includes('\\') && !/[\u0000-\u001f]/.test(value) ? value : null;}
  function itemKey(item) {return `${item.kind==='exploration'?'exploration':'job'}:${item.id||item.run_id||item.job_id}`;}
  function basePath(kind,id) {return `/${kind==='exploration'?'exploration-runs':'jobs'}/${encodeURIComponent(id)}`;}
  function configuration(entries) {
    const value={};
    for(const [key,raw] of Object.entries(entries)) {
      if(raw===''||raw===null||raw===undefined)continue;
      const [section,name]=key.split('.');
      if(key==='validator.require_evidence') {if(!['true','false',true,false].includes(raw))throw new Error('证据要求格式无效');(value[section]||={})[name]=raw===true||raw==='true';continue;}
      if(!fields[key])throw new Error('不支持的配置字段');
      const n=Number(raw),[min,max]=fields[key];
      if(!Number.isInteger(n)||n<min||n>max)throw new Error(`${key} 需要 ${min}–${max} 之间的整数`);
      (value[section]||={})[name]=n;
    }
    if(!Object.keys(value).length)throw new Error('至少填写一项需要修改的配置。');
    return value;
  }
  // Assign columns from real edges. Cycles and disconnected nodes retain visible nodes;
  // this is a runtime relationship view, never an inferred reasoning trace.
  function layoutGraph(graph={}) {
    const nodes=[],seen=new Set();
    for(const node of graph.nodes||[]) {if(!node||typeof node.id!=='string'||seen.has(node.id))continue;seen.add(node.id);nodes.push({...node});}
    const edges=(graph.edges||[]).filter(e=>e&&seen.has(e.source)&&seen.has(e.target)).map(e=>({...e}));
    const incoming=new Map(nodes.map(n=>[n.id,0]));for(const e of edges)if(e.source!==e.target)incoming.set(e.target,incoming.get(e.target)+1);
    const depths=new Map(),roots=nodes.filter(n=>incoming.get(n.id)===0);if(!roots.length&&nodes.length)roots.push(nodes[0]);
    const queue=roots.map(n=>[n.id,0]);while(queue.length){const [id,depth]=queue.shift();if(depths.has(id))continue;depths.set(id,Math.min(depth,6));for(const e of edges)if(e.source===id&&!depths.has(e.target))queue.push([e.target,depth+1]);}
    for(const n of nodes)if(!depths.has(n.id))depths.set(n.id,0);
    const columns=new Map();for(const n of nodes){const col=depths.get(n.id);if(!columns.has(col))columns.set(col,[]);columns.get(col).push(n);}
    const rows=Math.max(1,...[...columns.values()].map(c=>c.length)),width=Math.max(680,(Math.max(0,...columns.keys())+1)*260+40),height=Math.max(260,rows*110+50);
    for(const [col,items]of columns)for(const [i,n]of items.entries()){n.x=24+col*260;n.y=25+(height-50-items.length*110)/2+i*110;n.width=218;n.height=76;}
    return {nodes,edges,width,height};
  }
  // Presentation-only overlay. Never send these nodes back to telemetry or the
  // supervision input: that would make a reviewer observe its own UI projection.
  function withSupervisionGraph(graph={},supervision,detail={}) {
    const result={...graph,nodes:(graph.nodes||[]).map(node=>({...node})),edges:(graph.edges||[]).map(edge=>({...edge}))};
    if(!supervision)return result;
    const review=(supervision.reviews||[]).at(-1),enabled=supervision.config?.enabled===true;
    if(!review&&!enabled)return result;
    const targetId=detail.id||supervision.target_id;
    const targetKind=detail.kind||supervision.kind;
    const target=targetKind==='job'?'job:'+targetId:targetId;
    const ids=new Set(result.nodes.map(node=>node.id));
    const profile=review?.model_profile||supervision.config?.model_profile;
    const profileName={api_sol:'Sol',api_astra:'Astra'}[profile]||profile||'';
    const ident=review?.review_id?'supervisor-review:'+review.review_id:'supervisor-config:'+targetKind+':'+targetId;
    const status=review?review.status||'unknown':supervision.status==='waiting_activity'?'waiting_activity':supervision.status==='budget_exhausted'?'budget_exhausted':'waiting';
    const node={id:ident,label:`监督 Agent${profileName?' · '+profileName:''}${review&&review.status!=='running'?' · 最近检查':''}`,kind:'supervisor',status,
      detail:{review_id:review?.review_id||null,supervision_enabled:enabled,supervision_state:supervision.status,
        mode:supervision.config?.mode,selected_binding:review?.selected_binding||null,
        operational_snapshot:review?.operational_snapshot||null,
        created_at:review?.created_at||null,finished_at:review?.finished_at||null,
        advice:review?.advice||'',guidance:review?.guidance||'',
        guidance_state:review?(review.status==='running'?'检查进行中，尚无引导结果':review.guidance||review.applied_command_id?guidanceState(review):'未提出引导'):'尚无检查记录，未调用监督模型',error:review?.error||null}};
    if(!ids.has(ident)){result.nodes.push(node);ids.add(ident);}
    const addEdge=edge=>{if(ids.has(edge.source)&&ids.has(edge.target)&&!result.edges.some(old=>old.id===edge.id))result.edges.push(edge);};
    if(!review){
      if(ids.has(target))addEdge({id:'supervision-config:'+ident,source:target,target:ident,kind:'supervision_configuration',status:'waiting',label:'已配置，尚未检查'});
      return result;
    }
    const snapshot=review.operational_snapshot;
    let snapshotSource=snapshot?.id?(snapshot.kind==='job'?'job:'+snapshot.id:snapshot.id):null;
    // Older audits may have a binding but not an archived snapshot. Use only a
    // recorded binding that also exists in this graph; never invent an Agent.
    if(!snapshotSource&&review.selected_binding?.run_id)snapshotSource='agent:'+review.selected_binding.run_id;
    if(snapshotSource)addEdge({id:'supervision-read:'+review.review_id,source:snapshotSource,target:ident,kind:'supervision_snapshot',status,label:'读取运行快照'});
    if(review.applied_command_id){
      const command=(detail.control?.commands||[]).find(item=>item.command_id===review.applied_command_id);
      const commandStatus=command?.status||'pending';
      const labels={applied:'引导已生效',rejected:'引导已拒绝',pending:'引导已提交，待生效'};
      const guideNode='guide:'+review.applied_command_id;
      const destination=ids.has(guideNode)?guideNode:ids.has(target)?target:null;
      if(destination)addEdge({id:'supervision-guidance:'+review.applied_command_id,source:ident,target:destination,kind:'supervision_guidance',status:commandStatus,label:labels[commandStatus]||'引导已提交，回执待确认'});
    }
    return result;
  }
  function isEnded(detail) {return Boolean(detail && (['completed','failed','cancelled','terminal'].includes(detail.status)||detail.closed));}
  function supervisionAllowed(detail,enabled) {return Boolean(detail)&&(!isEnded(detail)||enabled===false);}
  function guidanceState(review) {
    const states={submitted_pending_safe_point:'已提交引导，等待执行 Agent 安全节点',not_submitted_control_changed:'未发送引导：控制版本已改变',advisory_only_or_target_changed:'仅供参考，未发送到执行 Agent'};
    return states[review.guidance_status]||(review.applied_command_id?'已提交引导，实际生效见控制记录':'仅建议，未发送到执行 Agent');
  }
  function auditReasons(command) {
    const values=[];if(command.reason)values.push(typeof command.reason==='string'?command.reason:JSON.stringify(command.reason));if(command.error)values.push(typeof command.error==='string'?command.error:JSON.stringify(command.error));
    for(const target of command.targets||[]) {const reason=target.reason||target.error;if(reason)values.push(`${target.job_id||'子任务'}：${typeof reason==='string'?reason:JSON.stringify(reason)}`);}
    return [...new Set(values)];
  }
  function effectiveField(control,path) {
    const [section,name]=path.split('.'),desired=control.desired?.[section]?.[name];
    let value=control.effective?.[section]?.[name];
    const children=control.effective_by_job;
    if(section!=='dispatch_budget'&&Array.isArray(children)&&children.length) {
      const values=children.map(child=>child.effective?.[section]?.[name]);
      if(values.some(v=>v===undefined))return {state:'pending',value:undefined,desired};
      if(values.some(v=>JSON.stringify(v)!==JSON.stringify(values[0])))return {state:'mixed',value:undefined,desired};
      value=values[0];
    }
    if(value===undefined&&desired!==undefined)return {state:'pending',value:undefined,desired};
    return {state:'known',value,desired};
  }
  function telemetryState(detail) {
    const childSamples=(detail?.resources?.items||[]).map(item=>item.sampled_at).filter(value=>value&&Number.isFinite(new Date(value).valueOf())).sort((a,b)=>new Date(a)-new Date(b));return {stale:Boolean(detail?.telemetry_stale),age:knownNumber(detail?.telemetry_age_seconds),sampled_at:detail?.telemetry_sampled_at||(detail?.kind==='exploration'?childSamples[0]:detail?.updated_at)||null};
  }
  class ReadEpoch {
    constructor(){this.epoch=0;this.controller=null;}
    invalidate(){this.epoch++;this.controller?.abort();this.controller=null;}
    begin(){this.invalidate();this.controller=new AbortController();return {epoch:this.epoch,signal:this.controller.signal};}
    current(token){return token.epoch===this.epoch&&!token.signal.aborted;}
  }
  const api={statuses,kinds,fields,knownNumber,number,duration,bytes,usage,label,safePath,itemKey,basePath,configuration,layoutGraph,withSupervisionGraph,isEnded,supervisionAllowed,guidanceState,auditReasons,effectiveField,telemetryState,ReadEpoch};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.FleetOperations=api;
})(typeof globalThis!=='undefined'?globalThis:this);
