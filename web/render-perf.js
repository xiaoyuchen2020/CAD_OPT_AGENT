// Opt-in local verification only: /?renderPerf=1. No model calls or data writes.
export function install({S, selectDrawing, fit, zoom, setView, showSource, clearSelection, api, whenPreviewReady=async()=>{}}) {
  const panel=document.createElement('div');
  panel.style.cssText='position:fixed;top:3px;left:38%;z-index:1000;background:#fff;padding:4px;border:1px solid #aaa;font:12px monospace';
  const button=document.createElement('button');button.id='render-perf-run';button.textContent='运行四图交互基准';
  const status=document.createElement('span');status.id='render-perf-status';status.textContent='准备就绪';
  const output=document.createElement('pre');output.id='render-perf-results';output.hidden=true;
  panel.append(button,status,output);document.body.append(panel);
  const frame=()=>new Promise(resolve=>requestAnimationFrame(resolve));
  const settle=async()=>{await frame();await whenPreviewReady();await frame();await frame();};
  const stats=values=>{const sorted=[...values].sort((a,b)=>a-b);return {n:values.length,mean_ms:+(values.reduce((a,b)=>a+b,0)/values.length).toFixed(2),p95_ms:+sorted[Math.floor((sorted.length-1)*.95)].toFixed(2),max_ms:+sorted.at(-1).toFixed(2),over50:values.filter(v=>v>50).length};};
  async function measure(action,n=72) {
    const intervals=[],cost=[];let prev=await frame();
    for(let i=0;i<n;i++) {const begin=performance.now();action(i);cost.push(performance.now()-begin);const now=await frame();intervals.push(now-prev);prev=now;}
    return {frames:stats(intervals),handler:stats(cost),nodes:document.querySelectorAll('#drawing-geometry text,#drawing-geometry path').length};
  }
  button.onclick=async()=>{
    button.disabled=true;const results=[];output.textContent='';
    try {
      for(const drawing of [...S.drawings].filter(d=>/^sample-(59|60|61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))) {
        status.textContent=drawing.name;await selectDrawing(drawing.id);await settle();
        const sources=(await api(`/api/drawings/${drawing.id}/sources?q=${encodeURIComponent('混凝土')}&limit=30`)).items.filter(s=>s.layout_id===S.layout);
        const r={name:drawing.name,viewport:[innerWidth,innerHeight],initial_nodes:document.querySelectorAll('#drawing-geometry text,#drawing-geometry path').length};
        fit();await settle();r.zoom=await measure(i=>zoom(i%48<24?.96:1/.96),96);
        fit();const full=[...S.view];await settle();r.pan_overview=await measure(i=>setView([full[0]+Math.sin(i/12)*full[2]*.2,full[1]+Math.cos(i/12)*full[3]*.1,full[2],full[3]]));
        if(sources.length) {
          showSource(sources[0]);await settle();const close=[...S.view];
          r.pan_detail=await measure(i=>setView([close[0]+Math.sin(i/12)*close[2]*.4,close[1]+Math.cos(i/12)*close[3]*.2,close[2],close[3]]));
          const times=[];for(let i=0;i<12;i++){await settle();const start=performance.now();showSource(sources[i%sources.length]);await settle();times.push(performance.now()-start);}r.locate=stats(times);
        }
        if(sources.length) {
          clearSelection();fit();await settle();
          const [x,y,xx,yy]=sources[0].bbox,w=S.view[2],h=S.view[3];
          setView([(x+xx-w)/2,-(y+yy+h)/2,w,h]);await settle();
          r.zoom_deep=await measure(i=>zoom(i<48?.92:1/.92),96);
        }
        clearSelection();fit();await settle();results.push(r);output.textContent=JSON.stringify(results);
      }
      status.textContent='完成';output.hidden=false;output.style.cssText='max-height:100px;max-width:500px;overflow:auto';
    }catch(e){status.textContent='失败: '+e.message;}finally{button.disabled=false;}
  };
}
