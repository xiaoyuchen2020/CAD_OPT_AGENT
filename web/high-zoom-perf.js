// Explicit local diagnostic only; camera operations use the production renderer.
export function install({S,selectDrawing,fit,zoom,setView,showSource,clearSelection,api,whenPreviewReady}) {
 const panel=document.createElement('div');panel.style.cssText='position:fixed;top:0;left:30%;z-index:1000;background:white;padding:6px;font:12px monospace';
 const button=document.createElement('button');button.id='high-zoom-run';button.textContent='测试高倍缩放';
 const status=document.createElement('span');status.id='high-zoom-status';status.textContent='准备';
 const output=document.createElement('pre');output.id='high-zoom-results';output.hidden=true;
 panel.append(button,status,output);document.body.append(panel);
 const frame=()=>new Promise(requestAnimationFrame);
 const settle=async()=>{await frame();await whenPreviewReady();await frame();await frame();};
 const stats=a=>{const s=[...a].sort((a,b)=>a-b);return {n:a.length,mean_ms:+(a.reduce((x,y)=>x+y,0)/a.length).toFixed(2),p95_ms:+s[Math.floor((s.length-1)*.95)].toFixed(2),max_ms:+s.at(-1).toFixed(2),over33:a.filter(x=>x>33.4).length,over50:a.filter(x=>x>50).length,over100:a.filter(x=>x>100).length};};
 async function measure(action,n=96) {
  let previous=await frame(),nodeMax=0,pendingFrames=0;const intervals=[],actionCost=[];
  for(let i=0;i<n;i++) {const begin=performance.now();action(i);actionCost.push(performance.now()-begin);const now=await frame();intervals.push(now-previous);previous=now;nodeMax=Math.max(nodeMax,document.querySelector('#drawing-geometry g')?.childElementCount||0);if(S.preview&&!S.preview.ready)pendingFrames++;}
  return {frames:stats(intervals),action:stats(actionCost),node_max:nodeMax,pending_frames:pendingFrames,final_zoom:document.getElementById('zoom-label').textContent};
 }
 const abButton=document.createElement('button');abButton.id='high-zoom-ab';abButton.textContent='高倍率瓶颈对照';panel.append(abButton);
 const abOutput=document.createElement('pre');abOutput.id='high-zoom-ab-results';abOutput.hidden=true;panel.append(abOutput);
 abButton.onclick=async()=>{
  abButton.disabled=true;button.disabled=true;const rows=[],style=document.createElement('style');document.head.append(style);
  try {
   for(const drawing of [...S.drawings].filter(d=>/^sample-(61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))) {
    await selectDrawing(drawing.id);await settle();
    const sources=(await api(`/api/drawings/${drawing.id}/sources?q=${encodeURIComponent('混凝土')}&limit=100`)).items.filter(s=>s.layout_id===S.layout);
    const source=[...sources].sort((a,b)=>(b.quote||b.text||'').length-(a.quote||a.text||'').length)[0];showSource(source);await settle();
    const b=source.bbox,w=S.fullView[2]/900,h=S.fullView[3]/900,view=[(b[0]+b[2]-w)/2,-(b[1]+b[3]+h)/2,w,h];
    for(let pass=0;pass<2;pass++)for(const variant of ['all','hide_text','hide_paths','hide_highlight']) {
     status.textContent=`对照 ${drawing.name} ${pass+1} ${variant}`;
     style.textContent=variant==='all'?'':`${variant==='hide_text'?'#drawing-geometry text':variant==='hide_paths'?'#drawing-geometry path':'#drawing-highlight'} {visibility:hidden !important}`;
     setView(view);await settle();
     const timing=await measure(i=>setView([view[0]+Math.sin(i/10)*w*.8,view[1]+Math.cos(i/13)*h*.6,w,h]));
     rows.push({name:drawing.name,pass,variant,percent:90000,...timing});abOutput.textContent=JSON.stringify(rows);
    }
   }
   status.textContent='对照完成';
  }catch(e){status.textContent='对照失败 '+e.message;}finally{style.remove();abButton.disabled=false;button.disabled=false;}
 };
 button.onclick=async()=>{
  button.disabled=true;const result={viewport:[innerWidth,innerHeight],device_pixel_ratio:devicePixelRatio,user_agent:navigator.userAgent,rows:[]};output.textContent='';
  try {
   result.idle=await measure(()=>{},120);
   for(const drawing of [...S.drawings].filter(d=>/^sample-(59|60|61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))) {
    await selectDrawing(drawing.id);await settle();
    const sources=(await api(`/api/drawings/${drawing.id}/sources?q=${encodeURIComponent('混凝土')}&limit=100`)).items.filter(s=>s.layout_id===S.layout);
    const targets=[sources[0],[...sources].sort((a,b)=>(b.quote||b.text||'').length-(a.quote||a.text||'').length)[0]];
    for(let target=0;target<targets.length;target++) {
     const source=targets[target];showSource(source);await settle();
     const bounds=source.bbox,center=[(bounds[0]+bounds[2])/2,-(bounds[1]+bounds[3])/2];
     for(const percent of [1000,5000,10000,25000,50000,90000]) {
      status.textContent=`${drawing.name} 区域${target+1} ${percent}%`;
      const w=S.fullView[2]*100/percent,h=S.fullView[3]*100/percent,view=[center[0]-w/2,center[1]-h/2,w,h];
      const start=performance.now();setView(view);await settle();
      const row={name:drawing.name,target,region:source.region_id,quote:source.quote||source.text,percent,enter_ms:+(performance.now()-start).toFixed(1),loaded_packets:S.preview?.loaded.size};
      row.pan=await measure(i=>setView([view[0]+Math.sin(i/10)*w*.8,view[1]+Math.cos(i/13)*h*.6,w,h]));await settle();
      setView(view);await settle();row.zoom=await measure(i=>zoom(i%32<16?1/.98:.98));await settle();
      row.ready=S.preview?.ready;row.error=String(S.preview?.error||'');result.rows.push(row);output.textContent=JSON.stringify(result);
     }
    }
    clearSelection();fit();await settle();
   }
   status.textContent='完成';
  }catch(e){status.textContent='失败 '+e.message;result.error=String(e);output.textContent=JSON.stringify(result);}finally{button.disabled=false;}
 };
}
