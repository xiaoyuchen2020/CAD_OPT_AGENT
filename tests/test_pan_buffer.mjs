import test from 'node:test';
import assert from 'node:assert/strict';
import {bufferGeometry,bufferOffset,PanBuffer} from '../web/pan-buffer.mjs';

test('buffer and CSS translation preserve exact screen coordinates including letterboxing',()=>{
 for(const [width,height] of [[600,400],[400,600],[1200,300]])for(const view of [[100,-300,80,40],[1e6,-9e5,41,79]]){
 const g=bufferGeometry(view,width,height),anchor={view,width,height,geometry:g};
 const target=[view[0]+view[2]*.2,view[1]-view[3]*.1,view[2],view[3]],offset=bufferOffset(anchor,target,width,height);
 for(const point of [[view[0],view[1]],[view[0]+20,view[1]+33]]){
 const native=[width/2+(point[0]-target[0]-target[2]/2)*g.scale,height/2+(point[1]-target[1]-target[3]/2)*g.scale];
 const buffered=[-width+offset[0]+(point[0]-g.view[0])*g.scale,-height+offset[1]+(point[1]-g.view[1])*g.scale];
 assert.ok(native.every((v,i)=>Math.abs(v-buffered[i])<1e-7));
 }
 }
});
test('rebase before buffer edges and invalidate on zoom or container resize',()=>{
 const view=[0,0,100,50],a={view,width:600,height:300,geometry:bufferGeometry(view,600,300)};
 assert.ok(bufferOffset(a,[80,0,100,50],600,300));
 assert.equal(bufferOffset(a,[100,0,100,50],600,300),null);
 assert.equal(bufferOffset(a,[0,0,99,50],600,300),null);
 assert.equal(bufferOffset(a,view,600,301),null);
});
test('low zoom, continuous zoom, oversized surface and reset retain native rendering',()=>{
 const canvas={style:{},dataset:{}},p=new PanBuffer(canvas,()=>{}),v=[0,0,100,50],full=[0,0,90000,45000];
 let display=p.update(v,full,600,300);assert.deepEqual(display.view,v);
 p.until=0;display=p.update(v,full,600,300);assert.equal(canvas.dataset.panBuffered,'true');assert.equal(display.width,1800);
 const a=p.anchor;p.update([10,0,100,50],full,600,300);assert.equal(p.anchor,a);
 p.update([10,0,80,40],full,600,300);assert.equal(canvas.dataset.panBuffered,'false');
 p.until=0;p.update([10,0,80,40],full,4000,3000);assert.equal(p.anchor,null);
 p.until=0;p.update([10,0,80,40],[0,0,100,50],600,300);assert.equal(p.anchor,null);
 p.update(v,full,0,300);assert.equal(p.anchor,null);
 p.reset();assert.equal(p.last,null);assert.equal(p.anchor,null);
});
