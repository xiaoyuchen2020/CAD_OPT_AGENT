// Opt-in local QA. Synthetic shapes are display fixtures, never stored as evidence.
import {highlightGeometry,renderHighlight} from './highlight.mjs';
export function install({S,selectDrawing,setView,locate,api,whenPreviewReady,clearSelection}) {
 const panel=document.createElement('aside');panel.style.cssText='position:fixed;top:4px;left:25%;z-index:1000;background:white;padding:8px;border:1px solid #777;font:12px monospace';
 panel.innerHTML='<button id="highlight-check-run">检查四图高亮</button> <button id="highlight-fixture">显示合成凹形/孔洞样例</button> <span id="highlight-check-status">准备（合成轮廓仅用于显示验证）</span><pre id="highlight-check-results" hidden></pre>';
 document.body.append(panel);const $=id=>document.getElementById(id),frame=()=>new Promise(requestAnimationFrame);
 const settle=async()=>{await whenPreviewReady();await new Promise(r=>setTimeout(r,220));await frame();await frame();};
 function fixture(source){
  const [x,y,xx,yy]=source.bbox,h=Math.max(yy-y,1),w=Math.min(Math.max(xx-x,1),h*1.4);
  const point=([a,b])=>[x+a*w,y+b*h];
  return {...source,bbox:[x,y,x+w,y+h],highlight_regions:[
   {boundary:[[0,0],[1,0],[1,.35],[.4,.35],[.4,1],[0,1]].map(point),holes:[[[.1,.1],[.25,.1],[.25,.25],[.1,.25]].map(point)]},
   {boundary:[[.65,.65],[.95,.65],[.95,.95],[.65,.95]].map(point)}]};
 }
 let source;
 $('highlight-fixture').onclick=()=>{if(source){locate(fixture(source));$('highlight-check-status').textContent='合成显示样例：L 形凹口 + 孔洞 + 独立区域（非识别结果）';}};
 const stats=a=>{const s=[...a].sort((a,b)=>a-b);return {p95_ms:+s[Math.floor((s.length-1)*.95)].toFixed(2),max_ms:+s.at(-1).toFixed(2),over50:a.filter(n=>n>50).length,n:a.length};};
 function legacy(s){const margin=Math.max((s.bbox[3]-s.bbox[1])*.4,(s.bbox[2]-s.bbox[0])*.015,1);$('drawing-highlight').replaceChildren(...(s.component_boxes?.length?s.component_boxes:[s.bbox]).map(([x,y,xx,yy])=>{const e=document.createElementNS('http://www.w3.org/2000/svg','rect');for(const [k,v]of Object.entries({x,y:-yy,width:xx-x,height:yy-y,rx:margin/3,class:'source-highlight'}))e.setAttribute(k,v);return e;}));}
 $('highlight-check-run').onclick=async()=>{
  const result={viewport:[innerWidth,innerHeight],dpr:devicePixelRatio,rows:[],geometry:[]};$('highlight-check-run').disabled=true;
  try{for(const drawing of [...S.drawings].filter(d=>/^sample-(59|60|61|62)\.dxf$/.test(d.name)).sort((a,b)=>a.name.localeCompare(b.name))){
   await selectDrawing(drawing.id);await settle();
   const sources=(await api(`/api/drawings/${drawing.id}/sources?q=${encodeURIComponent('混凝土')}&limit=100`)).items.filter(s=>s.layout_id===S.layout);
   source=[...sources].sort((a,b)=>(b.parts?.length||0)-(a.parts?.length||0)||(b.text||'').length-(a.text||'').length)[0];
   const synthetic=fixture(source);locate(synthetic);await settle();
   const [x,y,xx,yy]=synthetic.bbox,w=xx-x,h=yy-y,p=$('drawing-highlight').firstChild;
   const probes=[[.05,.05,true],[.15,.15,false],[.5,.5,false],[.8,.8,true],[.5,.8,false]].map(([a,b,expected])=>{const pt=$('cad-canvas').createSVGPoint();pt.x=x+a*w;pt.y=-(y+b*h);return {expected,actual:p.isPointInFill(pt)};});
   const start=performance.now();for(let i=0;i<1000;i++)highlightGeometry(synthetic);const cached_ms=performance.now()-start;
   result.geometry.push({name:drawing.name,region:source.region_id,parts:source.parts?.length,probes,cached_1000_ms:cached_ms,nodes:$('drawing-highlight').childElementCount});
   for(const percent of [25000,90000])for(let pass=0;pass<2;pass++)for(const mode of (pass%2?['polygon','components','legacy']:['legacy','components','polygon'])){
    $('highlight-check-status').textContent=`${drawing.name} ${percent}% ${mode} ${pass+1}/2`;
    locate(mode==='polygon'?synthetic:source);if(mode==='legacy')legacy(source);
    const b=source.bbox,vw=S.fullView[2]*100/percent,vh=S.fullView[3]*100/percent,v=[(b[0]+b[2]-vw)/2,-(b[1]+b[3]+vh)/2,vw,vh];setView(v);await settle();
    const node=$('drawing-highlight').firstChild,d=node.getAttribute('d');let prev=await frame();const times=[];let mutations=0;
    const observer=new MutationObserver(records=>mutations+=records.length);observer.observe($('drawing-highlight'),{subtree:true,attributes:true,childList:true});
    for(let i=0;i<96;i++){setView([v[0]+Math.sin(i/10)*vw*.8,v[1]+Math.cos(i/13)*vh*.6,vw,vh]);const t=await frame();times.push(t-prev);prev=t;}
    observer.disconnect();result.rows.push({name:drawing.name,percent,pass,mode,frames:stats(times),nodes:$('drawing-highlight').childElementCount,stable_node:node===$('drawing-highlight').firstChild&&d===node.getAttribute('d'),highlight_mutations:mutations,ready:S.preview?.ready,buffered:$('cad-canvas').dataset.panBuffered});
    $('highlight-check-results').textContent=JSON.stringify(result);
   }
  }
  locate(fixture(source));await settle();$('highlight-check-status').textContent='完成；当前为合成轮廓展示（非识别结果）';
  }catch(e){$('highlight-check-status').textContent='失败 '+e.message;}finally{$('highlight-check-run').disabled=false;}
 };
}
