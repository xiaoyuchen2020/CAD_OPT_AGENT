// Explicit local load benchmark; no model calls or writes to application data.
export function install({S,selectDrawing,whenPreviewReady=async()=>{}}) {
 const panel=document.createElement('div');panel.style.cssText='position:fixed;top:0;left:30%;z-index:1000;background:white;padding:6px;font:12px monospace';
 const button=document.createElement('button');button.id='load-perf-run';button.textContent='测量四图首次加载';
 const status=document.createElement('span');status.id='load-perf-status';status.textContent='准备';
 const output=document.createElement('pre');output.id='load-perf-results';output.hidden=true;
 panel.append(button,status,output);document.body.append(panel);
 const frame=()=>new Promise(resolve=>requestAnimationFrame(resolve));
 button.onclick=async()=>{button.disabled=true;const result=[];
 try{for(let pass=0;pass<3;pass++)for(const d of [...S.drawings].filter(d=>/^sample-(59|60|61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))){
  status.textContent=`${pass+1}/3 ${d.name}`;performance.clearResourceTimings();
  const start=performance.now();await selectDrawing(d.id);const apiDone=performance.now();
  await frame();await whenPreviewReady();await frame();await frame();
  const end=performance.now();
  const texts=[...document.querySelectorAll('#drawing-geometry text')].map(e=>[e.getAttribute('data-handle'),e.textContent,e.getAttribute('transform')]);
  const paths=[...document.querySelectorAll('#drawing-geometry path')].map(e=>[e.getAttribute('data-handle'),e.getAttribute('d'),e.getAttribute('fill'),e.getAttribute('stroke')]);
  const fingerprint=async(data)=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(data))))].map(b=>b.toString(16).padStart(2,'0')).join('');
  const entries=performance.getEntriesByType('resource').filter(e=>e.name.includes('/api/drawings/'+d.id));
  result.push({pass,name:d.name,viewport:[innerWidth,innerHeight],open_ms:+(end-start).toFixed(1),select_ms:+(apiDone-start).toFixed(1),paint_wait_ms:+(end-apiDone).toFixed(1),requests:entries.map(e=>({path:new URL(e.name).pathname,ms:+e.duration.toFixed(1),transfer:e.transferSize,encoded:e.encodedBodySize,decoded:e.decodedBodySize})),text_count:texts.length,path_count:paths.length,text_hash:await fingerprint(texts),path_hash:await fingerprint(paths)});
  output.textContent=JSON.stringify(result);
 }status.textContent='完成';}catch(e){status.textContent='失败 '+e.message;}finally{button.disabled=false;}
 };
}
