// Keep native SVG text/shaping. At high zoom, pan its compositor surface instead
// of changing viewBox on every frame. The buffer uses the same CSS pixel scale.
export function bufferGeometry(view,width,height) {
 const [x,y,w,h]=view,scale=Math.min(width/w,height/h),vw=width/scale,vh=height/scale;
 return {view:[x+w/2-vw*1.5,y+h/2-vh*1.5,vw*3,vh*3],width:width*3,height:height*3,scale};
}
export function bufferOffset(anchor,view,width,height) {
 if(anchor.width!==width||anchor.height!==height||anchor.view[2]!==view[2]||anchor.view[3]!==view[3])return null;
 const dx=(anchor.view[0]-view[0])*anchor.geometry.scale,dy=(anchor.view[1]-view[1])*anchor.geometry.scale;
 return Math.abs(dx)<=width*.9&&Math.abs(dy)<=height*.9?[dx,dy]:null;
}
export class PanBuffer {
 constructor(canvas,wake){this.canvas=canvas;this.wake=wake;this.anchor=null;this.last=null;this.until=0;this.timer=0;}
 reset(){this.anchor=null;this.last=null;clearTimeout(this.timer);this.restore();}
 restore(){if(this.canvas.dataset.panBuffered==='true'){this.canvas.style.cssText='';this.canvas.dataset.panBuffered='false';}}
 update(view,full,width,height,enabled=true) {
  if(width<=0||height<=0){this.anchor=null;this.restore();return {view,width,height};}
  const now=performance.now(),scaleChanged=!this.last||this.last[2]!==view[2]||this.last[3]!==view[3];
  this.last=[...view];
  if(scaleChanged){this.until=now+120;clearTimeout(this.timer);this.timer=setTimeout(this.wake,130);this.anchor=null;}
  const ratio=(full?.[2]||view[2])/view[2],dpr=globalThis.devicePixelRatio||1;
  // Bound compositor memory (about 32 MiB RGBA) and texture dimensions.
  if(!enabled||this.disabled||ratio<250-1e-9||now<this.until||width*height*9*dpr*dpr>8_000_000||Math.max(width,height)*3*dpr>8192){
   this.anchor=null;this.restore();return {view,width,height};
  }
  let offset=this.anchor&&bufferOffset(this.anchor,view,width,height);
  if(!offset){this.anchor={view:[...view],width,height,geometry:bufferGeometry(view,width,height)};offset=[0,0];}
  const g=this.anchor.geometry;
  if(this.canvas.dataset.panBuffered!=='true'||this.canvas.style.width!==`${g.width}px`||this.canvas.style.height!==`${g.height}px`){
   this.canvas.style.cssText=`position:absolute;left:${-width}px;top:${-height}px;width:${g.width}px;height:${g.height}px;will-change:transform;transform-origin:0 0`;
   this.canvas.dataset.panBuffered='true';
  }
  this.canvas.style.transform=`translate3d(${offset[0]}px,${offset[1]}px,0)`;
  return {view:g.view,width:g.width,height:g.height};
 }
}
