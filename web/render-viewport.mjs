// Display-only viewport culling. Original items and source coordinates are retained.
export function visibleBounds(view, width, height, padding = 0) {
  const [x,y,w,h]=view;
  const scale=Math.min(width/w,height/h);
  const vw=width/scale, vh=height/scale;
  // SVG defaults to xMidYMid meet: include the letterboxed area too.
  return {scale, box:[x+w/2-vw*(.5+padding),-(y+h/2)-vh*(.5+padding),
    x+w/2+vw*(.5+padding),-(y+h/2)+vh*(.5+padding)]};
}
export function intersects(a,b) {
  return a[0]<=b[2] && a[2]>=b[0] && a[1]<=b[3] && a[3]>=b[1];
}
export function visibleIndices(items, box, scale, layoutId=null) {
  const indices=[];
  for(let i=0;i<items.length;i++) {
    const item=items[i];
    if (!item || (layoutId && item.layout_id!==layoutId)) continue;
    if(!intersects(item.bbox,box)) continue;
    // Unreadable overview text is restored as soon as its cap height is useful.
    if(item.type==='text' && Math.hypot(item.matrix[2],item.matrix[3])*scale<2) continue;
    // Display LOD only: subpixel geometry remains in immutable source packets
    // and comes back on zoom. Do not reduce long thin walls by their thickness.
    if(items.length>=50000 && item.type==='path' &&
       Math.max(item.bbox[2]-item.bbox[0],item.bbox[3]-item.bbox[1])*scale<.8) continue;
    indices.push(i);
  }
  return indices;
}
export function clientToView(view, rect, clientX, clientY) {
  const [x,y,w,h]=view;
  const scale=Math.min(rect.width/w,rect.height/h);
  return [x+w/2+(clientX-rect.left-rect.width/2)/scale,
    y+h/2+(clientY-rect.top-rect.height/2)/scale];
}
export function panView(view, scale, dx, dy) {
  return [view[0]-dx/scale,view[1]-dy/scale,view[2],view[3]];
}
export function wheelFactor(delta, mode=0, height=800) {
  const pixels=delta*(mode===1?16:mode===2?height:1);
  return Math.exp(Math.max(-120,Math.min(120,pixels))*.0012);
}
export function zoomView(view, full, factor, center) {
  const [x,y,w,h]=view, nw=w*factor, nh=h*factor;
  // A source can be narrower than the zoom limit. Always allow zooming back out.
  if(!Number.isFinite(factor) || factor<=0 || (nw<full[2]/1000 && nw<w) || (nw>full[2]*20 && nw>w)) return null;
  const [cx,cy]=center || [x+w/2,y+h/2];
  return [cx-(cx-x)*factor,cy-(cy-y)*factor,nw,nh];
}
export class SvgViewport {
  constructor(root, items, createNode, layoutId=null) {
    this.layoutId=layoutId;
    this.root=root;this.items=items;this.createNode=createNode;
    this.cache=new Map();this.active=[];this.query=null;
    root.setAttribute('data-total-items',items.length);
  }
  update(view,width,height) {
    if(width<=0 || height<=0) return;
    const current=visibleBounds(view,width,height);
    const q=this.query,b=current.box;
    if(q && b[0]>=q.box[0] && b[1]>=q.box[1] && b[2]<=q.box[2] && b[3]<=q.box[3]
       && current.scale>=q.scale*.8 && current.scale<=q.scale*1.2) return;
    const query=visibleBounds(view,width,height,.25);
    // Include slightly smaller text in the overscan to avoid threshold flicker.
    const next=visibleIndices(this.items,query.box,query.scale*1.2,this.layoutId),wanted=new Set(next);
    for(const i of this.active) if(!wanted.has(i)) this.cache.get(i).remove();
    let cursor=this.root.firstChild;
    for(const i of next) {
      let el=this.cache.get(i);
      if(!el){el=this.createNode(this.items[i]);this.cache.set(i,el);}
      if(el!==cursor) this.root.insertBefore(el,cursor);
      cursor=el.nextSibling;
    }
    this.active=next;this.query=query;
    this.root.setAttribute('data-visible-items',next.length);
    // Keep a bounded detached-node cache for back-and-forth navigation.
    const limit=Math.max(8192,next.length*2);
    for(const i of this.cache.keys()) {
      if(this.cache.size<=limit) break;
      if(!wanted.has(i)) this.cache.delete(i);
    }
  }
}
