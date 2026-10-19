export function install({S,selectDrawing,setView,showSource,api,whenPreviewReady,setBufferEnabled}) {
 const panel=document.createElement('div');panel.style.cssText='position:fixed;top:0;left:30%;z-index:1000;background:white;padding:5px;font:12px monospace';
 panel.innerHTML='<button id="pan-perf-run">四图拖动对比</button> <span id="pan-perf-status">准备</span><pre id="pan-perf-results" hidden></pre>';
 document.body.append(panel);const $=id=>document.getElementById(id),frame=()=>new Promise(requestAnimationFrame);
 let buffered=true;const toggle=document.createElement('button');toggle.id='pan-perf-toggle';toggle.textContent='切换原生对照';toggle.onclick=()=>{buffered=!buffered;setBufferEnabled(buffered);toggle.textContent=buffered?'切换原生对照':'切换优化显示';};panel.append(toggle);
 const settle=async()=>{await whenPreviewReady();await new Promise(r=>setTimeout(r,200));await frame();await frame();};
 const stats=a=>{let s=[...a].sort((a,b)=>a-b);return {n:a.length,p95_ms:+s[Math.floor((s.length-1)*.95)].toFixed(2),max_ms:+s.at(-1).toFixed(2),mean_ms:+(a.reduce((x,y)=>x+y,0)/a.length).toFixed(2),over50:a.filter(x=>x>50).length};};
 const qa=document.createElement('button');qa.id='pan-perf-qa';qa.textContent='核验四图显示';const qaOutput=document.createElement('pre');qaOutput.id='pan-perf-qa-results';qaOutput.hidden=true;panel.append(qa,qaOutput);
 const display=()=>{
  const rect=document.getElementById('canvas-wrap').getBoundingClientRect();
  return [...document.querySelectorAll('#drawing-geometry [data-handle]')].filter(e=>{const r=e.getBoundingClientRect();return r.right>=rect.left&&r.left<=rect.right&&r.bottom>=rect.top&&r.top<=rect.bottom;}).map(e=>({key:JSON.stringify([e.tagName,...['data-handle','transform','d','textLength','font-size','fill','stroke','stroke-width'].map(k=>e.getAttribute(k)),e.textContent]),matrix:['a','b','c','d','e','f'].map(k=>e.getScreenCTM()[k]),rect:['left','top','right','bottom'].map(k=>e.getBoundingClientRect()[k])}));
 };
 qa.onclick=async()=>{qa.disabled=true;const results=[];
 try{for(const drawing of [...S.drawings].filter(d=>/^sample-(59|60|61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))){
 await selectDrawing(drawing.id);await settle();const sources=(await api(`/api/drawings/${drawing.id}/sources?q=${encodeURIComponent('混凝土')}&limit=100`)).items.filter(s=>s.layout_id===S.layout);
 const source=[...sources].sort((a,b)=>(b.quote||b.text||'').length-(a.quote||a.text||'').length)[0];showSource(source);await settle();
 for(const percent of [90000,1000,50000]){
 const b=source.bbox,w=S.fullView[2]*100/percent,h=S.fullView[3]*100/percent,view=[(b[0]+b[2]-w)/2,-(b[1]+b[3]+h)/2,w,h];
 for(const dx of [0,.8,2.4]){const v=[view[0]+w*dx,view[1]+h*.1,w,h];setBufferEnabled(false);setView(v);await settle();const native=display();
 setBufferEnabled(true);setView(view);await settle();setView(v);await settle();const optimized=display();
 const same=native.length===optimized.length&&native.every((x,i)=>x.key===optimized[i].key);
 const error=same?Math.max(0,...native.flatMap((x,i)=>x.matrix.map((n,j)=>Math.abs(n-optimized[i].matrix[j])))):null;
 results.push({name:drawing.name,percent,pan_widths:dx,native_count:native.length,optimized_count:optimized.length,same_attributes_and_order:same,max_screen_matrix_error:error,max_rect_error_px:same?Math.max(0,...native.flatMap((x,i)=>x.rect.map((n,j)=>Math.abs(n-optimized[i].rect[j])))):null,ready:S.preview?.ready,buffered:document.getElementById('cad-canvas').dataset.panBuffered});qaOutput.textContent=JSON.stringify(results);
 }}
 const b=source.bbox,w=S.fullView[2]/900,h=S.fullView[3]/900;setView([(b[0]+b[2]-w)/2,-(b[1]+b[3]+h)/2,w,h]);await settle();
 }$('pan-perf-status').textContent='显示核验完成';}catch(e){$('pan-perf-status').textContent='显示核验失败 '+e.message;}finally{setBufferEnabled(true);buffered=true;toggle.textContent='切换原生对照';qa.disabled=false;}
 };
 $('pan-perf-run').onclick=async()=>{const result={viewport:[innerWidth,innerHeight],dpr:devicePixelRatio,rows:[]};$('pan-perf-run').disabled=true;
 try{for(const drawing of [...S.drawings].filter(d=>/^sample-(59|60|61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))){
 await selectDrawing(drawing.id);await settle();const sources=(await api(`/api/drawings/${drawing.id}/sources?q=${encodeURIComponent('混凝土')}&limit=100`)).items.filter(s=>s.layout_id===S.layout);
 const source=[...sources].sort((a,b)=>(b.quote||b.text||'').length-(a.quote||a.text||'').length)[0];showSource(source);await settle();
 for(const percent of [25000,50000,90000])for(let pass=0;pass<3;pass++){
 $('pan-perf-status').textContent=`${drawing.name} ${percent}% ${pass+1}/3`;
 const b=source.bbox,w=S.fullView[2]*100/percent,h=S.fullView[3]*100/percent,view=[(b[0]+b[2]-w)/2,-(b[1]+b[3]+h)/2,w,h];setView(view);await settle();
 const values=[];let prev=await frame(),bufferFrames=0;
 for(let i=0;i<96;i++){setView([view[0]+Math.sin(i/10)*w*.8,view[1]+Math.cos(i/13)*h*.6,w,h]);let t=await frame();values.push(t-prev);prev=t;if(document.getElementById('cad-canvas').dataset.panBuffered==='true')bufferFrames++;}
 await settle();const texts=[...document.querySelectorAll('#drawing-geometry text')].map(e=>[e.getAttribute('data-handle'),e.textContent,e.getAttribute('transform'),e.getAttribute('textLength'),e.getAttribute('font-size')]);
 result.rows.push({name:drawing.name,region:source.region_id,percent,pass,frames:stats(values),buffer_frames:bufferFrames,ready:S.preview?.ready,view:[...S.view],texts});$('pan-perf-results').textContent=JSON.stringify(result);
 }}$('pan-perf-status').textContent='完成';}catch(e){$('pan-perf-status').textContent='失败 '+e.message;}finally{$('pan-perf-run').disabled=false;}
 };
}
