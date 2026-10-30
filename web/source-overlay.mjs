import {highlightGeometry} from './highlight.mjs';
// The review page can select archived evidence; it cannot trigger analysis or edits.
export function validateReviewSource(source, drawing, layouts) {
  if (!source || source.snapshot_id !== drawing?.id || !source.source_sha256 || source.source_sha256 !== drawing.sha256)
    throw Error('图纸版本与任务归档不一致，未执行定位。');
  if (!layouts.some(layout => layout.layout_id === source.layout_id))
    throw Error('归档布局在此图纸中不存在，未执行定位。');
  const b = source.bbox;
  if (!Array.isArray(b) || b.length !== 4 || !b.every(Number.isFinite) || b[0] > b[2] || b[1] > b[3])
    throw Error('该来源没有有效坐标，未执行定位。');
  return {...source, entity_ids: Array.isArray(source.entity_ids) ? source.entity_ids : []};
}

const NS='http://www.w3.org/2000/svg';
export function overlayGeometry(source) {
  const line=source.measurement_line;
  if(Array.isArray(line)&&line.length===2&&line.every(p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite))){
    return {d:`M${line[0][0]},${-line[0][1]} L${line[1][0]},${-line[1][1]}`,bbox:[Math.min(...line.map(p=>p[0])),Math.min(...line.map(p=>p[1])),Math.max(...line.map(p=>p[0])),Math.max(...line.map(p=>p[1]))],line};
  }
  return highlightGeometry(source);
}
export function visibleSources(items,view,layout){
  if(!view)return [];
  const [x,y,w,h]=view;
  return items.filter(r=>r.source.layout_id===layout).filter(r=>{
    const b=overlayGeometry(r.source).bbox;
    return b&&b[2]>=x&&b[0]<=x+w&&-b[1]>=y&&-b[3]<=y+h;
  });
}
export function clusterSources(items,view,width,height){
  const cells=new Map();
  for(const row of items){
    const b=overlayGeometry(row.source).bbox;if(!b)continue;
    const px=(b[2]-b[0])/view[2]*width,py=(b[3]-b[1])/view[3]*height;
    if(items.length<=250&&Math.max(px,py)>18){cells.set(row.source_id,[row]);continue;}
    const key=Math.floor(((b[0]+b[2])/2-view[0])/view[2]*width/42)+','+Math.floor((-(b[1]+b[3])/2-view[1])/view[3]*height/42);
    if(!cells.has(key))cells.set(key,[]);cells.get(key).push(row);
  }
  return [...cells.values()];
}
export function installSourceOverlay({state,selectDrawing,renderScene,setView,ready,win=window}){
  if(win.parent===win||new URLSearchParams(win.location.search).get('sources')!=='1')return;
  win.document.body.classList.add('result-viewer','source-overview-viewer');
  const canvas=win.document.getElementById('cad-canvas');
  const group=win.document.createElementNS(NS,'g');group.id='drawing-source-overview';canvas.append(group);
  const style=win.document.createElement('style');style.textContent=`
    .source-overview-viewer #evidence-drawer{display:none!important}
    .source-overview-viewer .workspace{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr);height:100dvh;min-height:0;overflow:hidden}
    .source-overview-viewer .files-panel,.source-overview-viewer .chat-panel{display:none}
    .source-overview-viewer .drawing-panel{grid-column:1;grid-row:1;height:100dvh;min-height:0;border:0}
    .source-overview-viewer .drawing-toolbar{height:48px}
    .source-overlay-mark{fill:#327ad21a;stroke:#327ad2;stroke-width:1.5;vector-effect:non-scaling-stroke}
    .source-overlay-mark.selected-input{fill:#2785631a;stroke:#278563}
    .source-overlay-mark.pending{stroke:#bc7a18;stroke-dasharray:5 3}
    .source-overlay-mark.focused{stroke:#b23bd4;stroke-width:3}
    .source-overlay-line{fill:none;stroke-width:2.5}
    .source-overlay-dot{fill:#fff;stroke:#278563;stroke-width:2;vector-effect:non-scaling-stroke}
    .source-overlay-label{fill:#213f34;font-family:sans-serif;text-anchor:middle;dominant-baseline:central;pointer-events:none}
  `;win.document.head.append(style);
  let items=[],selected=null,enabled=true,epoch=0,queue=Promise.resolve(),frame=0,requestId=null;
  const send=data=>win.parent.postMessage(data,win.location.origin);
  const svg=(name,attrs)=>{const e=win.document.createElementNS(NS,name);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);return e;};
  function focus(row){
    const b=overlayGeometry(row.source).bbox;if(!b)return;
    const w=Math.max((b[2]-b[0])*1.6,(b[3]-b[1])*2,1),ratio=canvas.clientHeight/Math.max(canvas.clientWidth,1),h=Math.max((b[3]-b[1])*2,w*ratio);
    setView([(b[0]+b[2]-w)/2,-(b[1]+b[3]+h)/2,w,h]);
  }
  function draw(){
    frame=0;group.replaceChildren();if(!enabled||!state.view)return;
    const view=canvas.getAttribute('viewBox').split(/\s+/).map(Number);
    if(view.length!==4||!view.every(Number.isFinite)||view[2]<=0||view[3]<=0)return;
    const visible=visibleSources(items,view,state.layout),w=Math.max(canvas.clientWidth,1),h=Math.max(canvas.clientHeight,1),scale=view[2]/w;
    const groups=clusterSources(visible,view,w,h);
    for(const members of groups){
      const first=members[0],shape=overlayGeometry(first.source);let mark;
      if(members.length>1){
        const boxes=members.map(r=>overlayGeometry(r.source).bbox),x=boxes.reduce((a,b)=>a+(b[0]+b[2])/2,0)/boxes.length,y=-boxes.reduce((a,b)=>a+(b[1]+b[3])/2,0)/boxes.length;
        mark=svg('g',{});mark.append(svg('circle',{cx:x,cy:y,r:13*scale,class:'source-overlay-dot'}));
        const label=svg('text',{x,y,'font-size':12*scale,class:'source-overlay-label'});label.textContent=String(members.length);mark.append(label);
      }else{
        mark=svg('g',{});const cls='source-overlay-mark'+(first.selected?' selected-input':'')+(first.issues?.length?' pending':'')+(selected===first.source_id?' focused':'')+(shape.line?' source-overlay-line':'');
        mark.append(svg('path',{d:shape.d,class:cls,'fill-rule':'nonzero','pointer-events':'all'}));
        if(shape.line)for(const[x,y]of shape.line)mark.append(svg('circle',{cx:x,cy:-y,r:3*scale,class:'source-overlay-dot'}));
        // A wide transparent stroke makes small/linear sources usable without changing evidence geometry.
        mark.append(svg('path',{d:shape.d,fill:'none',stroke:'transparent','stroke-width':12,'vector-effect':'non-scaling-stroke','pointer-events':'stroke'}));
      }
      mark.dataset.sourceId=first.source_id;
      const title=svg('title',{});title.textContent=members.length>1?`${members.length} 处来源，点击展开`:first.source.quote||'来源';mark.append(title);
      let down=null;mark.addEventListener('pointerdown',event=>{down=[event.clientX,event.clientY];});
      mark.addEventListener('click',event=>{
        if(down&&Math.hypot(event.clientX-down[0],event.clientY-down[1])>5)return;
        event.stopPropagation();
        if(members.length>1){
          const boxes=members.map(r=>overlayGeometry(r.source).bbox),b=[Math.min(...boxes.map(b=>b[0])),Math.min(...boxes.map(b=>b[1])),Math.max(...boxes.map(b=>b[2])),Math.max(...boxes.map(b=>b[3]))];
          focus({source:{bbox:b}});send({type:'cad-sources:cluster',request_id:requestId,source_ids:members.map(r=>r.source_id)});
        }else{selected=first.source_id;schedule();send({type:'cad-sources:selected',request_id:requestId,source_id:selected});}
      });group.append(mark);
    }
    send({type:'cad-sources:viewport',request_id:requestId,visible:visible.length,marks:groups.length,total:items.length,layout_id:state.layout});
  }
  function schedule(){if(!frame)frame=win.requestAnimationFrame(draw);}
  new win.MutationObserver(schedule).observe(canvas,{attributes:true,attributeFilter:['viewBox']});
  new win.ResizeObserver(schedule).observe(canvas);
  win.addEventListener('message',event=>{
    if(event.origin!==win.location.origin||event.source!==win.parent)return;
    const m=event.data;if(!m||!['cad-sources:set','cad-sources:focus'].includes(m.type))return;
    const current=m.type==='cad-sources:set'?++epoch:epoch;
    queue=queue.catch(()=>{}).then(async()=>{
      if(current!==epoch)return;
      try{
        await ready;if(current!==epoch)return;
        if(m.type==='cad-sources:focus'){
          const row=items.find(r=>r.source_id===m.source_id);if(!row)return;
          selected=row.source_id;if(m.zoom!==false)focus(row);schedule();return;
        }
        const drawing=state.drawings.find(d=>d.id===m.snapshot_id);
        if(!drawing||drawing.sha256!==m.source_sha256)throw Error('图纸版本与来源不一致，未绘制高亮');
        const switched=state.drawing?.id!==drawing.id;
        if(switched){items=[];group.replaceChildren();await selectDrawing(drawing.id,{keepConversation:true});}
        if(current!==epoch)return;
        if(state.drawing?.id!==m.snapshot_id||state.drawing.sha256!==m.source_sha256)throw Error('加载后的图纸版本不一致');
        if(!state.scene?.layouts.some(l=>l.layout_id===m.layout_id))throw Error('来源布局不存在');
        if(state.layout!==m.layout_id){state.layout=m.layout_id;win.document.getElementById('layout-select').value=m.layout_id;renderScene();}
        const accepted=[],rejected=[];
        for(const row of m.items||[]){try{accepted.push({...row,source:validateReviewSource(row.source,state.drawing,state.scene.layouts)});}catch(e){rejected.push({source_id:row.source_id,message:e.message});}}
        items=accepted;selected=m.selected||null;enabled=m.enabled!==false;requestId=m.request_id;schedule();
        send({type:'cad-sources:loaded',request_id:m.request_id,accepted:accepted.length,rejected,layouts:state.scene.layouts.map(l=>({layout_id:l.layout_id,name:l.name||l.layout_id}))});
      }catch(error){if(current===epoch){items=[];group.replaceChildren();send({type:'cad-sources:error',request_id:m.request_id,message:error.message});}}
    });
  });
  Promise.resolve(ready).then(()=>send({type:'cad-sources:ready'}));
}
