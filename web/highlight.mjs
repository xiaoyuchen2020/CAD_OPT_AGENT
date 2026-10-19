// WCS display geometry only. Build once per selection, never during camera updates.
export const MAX_HIGHLIGHT_POINTS = 16384;
const MAX_REGIONS = 512;
const cache = new WeakMap();
const validBox = b => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite) && b[2] > b[0] && b[3] > b[1];
const boxRing = ([x,y,xx,yy]) => [[x,y],[xx,y],[xx,yy],[x,yy]];
function ring(points, positive, budget) {
  if (!Array.isArray(points) || points.length < 3 || (budget.count += points.length) > MAX_HIGHLIGHT_POINTS) throw Error('invalid_or_excessive_geometry');
  const out=[];
  for (const p of points) {
    if (!Array.isArray(p) || p.length !== 2 || !p.every(Number.isFinite)) throw Error('invalid_geometry');
    if (!out.length || p[0] !== out.at(-1)[0] || p[1] !== out.at(-1)[1]) out.push(p);
  }
  if (out.length > 1 && out[0][0] === out.at(-1)[0] && out[0][1] === out.at(-1)[1]) out.pop();
  // Translate before the area sum to avoid cancellation at large CAD coordinates.
  const [ox,oy]=out[0] || [0,0];
  let area=0;
  for(let i=0;i<out.length;i++){const a=out[i],b=out[(i+1)%out.length];area+=(a[0]-ox)*(b[1]-oy)-(b[0]-ox)*(a[1]-oy);}
  if (!Number.isFinite(area) || area === 0) throw Error('degenerate_geometry');
  if ((area > 0) !== positive) out.reverse();
  return out;
}
function compile(regions) {
  if (!Array.isArray(regions) || !regions.length || regions.length > MAX_REGIONS) throw Error('region_limit');
  const budget={count:0}, rings=[],seen=new Set();
  for (const region of regions) {
    const outer=ring(region.boundary,true,budget),holes=region.holes || [];
    if (!Array.isArray(holes) || holes.length > MAX_REGIONS) throw Error('ring_limit');
    const group=[outer,...holes.map(h=>ring(h,false,budget))];
    const key=JSON.stringify(group);
    if (!seen.has(key)) {seen.add(key);rings.push(...group);}
  }
  let bbox=[Infinity,Infinity,-Infinity,-Infinity],d='';
  for(const r of rings) {
    d += r.map(([x,y],i)=>`${i?'L':'M'}${x},${-y}`).join(' ')+' Z ';
    for(const [x,y] of r){bbox[0]=Math.min(bbox[0],x);bbox[1]=Math.min(bbox[1],y);bbox[2]=Math.max(bbox[2],x);bbox[3]=Math.max(bbox[3],y);}
  }
  return {d:d.trim(),bbox,points:budget.count,rings:rings.length,mode:'regions'};
}
export function highlightGeometry(source) {
  if (cache.has(source)) return cache.get(source);
  let result,reason='';
  const explicit=source.highlight_regions?.length ? source.highlight_regions : source.boundary ? [source] : null;
  try {
    if(explicit) result=compile(explicit);
  } catch(e) {reason=e.message;}
  if(!result) {
    const parts=source.source_parts?.length ? source.source_parts : source.parts;
    try {
      if (parts?.length > MAX_REGIONS) throw Error('region_limit');
      if (parts?.some(p=>p.highlight_regions?.length)) {
        const regions=[];
        for (const p of parts) {
          if (p.highlight_regions?.length) {
            if (regions.length+p.highlight_regions.length > MAX_REGIONS) throw Error('region_limit');
            regions.push(...p.highlight_regions);
          } else if (validBox(p.bbox)) regions.push({boundary:boxRing(p.bbox)});
          else throw Error('invalid_geometry');
        }
        result=compile(regions);
      } else {
        const boxes=source.component_boxes?.length ? source.component_boxes : parts?.length ? parts.map(p=>p.bbox) : [source.bbox];
        if (boxes.length > MAX_REGIONS || !boxes.every(validBox)) throw Error('invalid_or_excessive_boxes');
        result=compile(boxes.map(b=>({boundary:boxRing(b)})));
        result.mode='components';
      }
    } catch(e) {reason=reason || e.message;}
  }
  if (!result) result=validBox(source.bbox) ? {...compile([{boundary:boxRing(source.bbox)}]),mode:'bbox'} : {d:'',bbox:null,points:0,rings:0,mode:'empty'};
  result.fallback=reason;
  cache.set(source,result);
  return result;
}
export function renderHighlight(root, source, className='source-highlight') {
  const shape=highlightGeometry(source);
  // One compound path: overlapping components do not accumulate opacity.
  const el=root.ownerDocument.createElementNS('http://www.w3.org/2000/svg','path');
  el.setAttribute('d',shape.d);el.setAttribute('class',className);
  el.setAttribute('fill-rule','nonzero');el.setAttribute('pointer-events','none');
  root.replaceChildren(...(shape.d ? [el] : []));
  root.dataset.highlightMode=shape.mode;root.dataset.highlightFallback=shape.fallback;
  return shape;
}
