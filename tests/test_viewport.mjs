import test from 'node:test';
import assert from 'node:assert/strict';
import {SvgViewport, visibleBounds, visibleIndices, clientToView, panView, zoomView, wheelFactor} from '../web/render-viewport.mjs';

test('letterboxed SVG view includes the extra visible area and flips world Y',()=>{
  assert.deepEqual(visibleBounds([0,0,100,100],200,100),{scale:1,box:[-50,-100,150,0]});
  assert.deepEqual(clientToView([0,0,100,100],{left:20,top:30,width:200,height:100},20,30),[-50,0]);
});
test('pan uses initial screen delta and preserves size at every input rate',()=>{
  assert.deepEqual(panView([100,200,40,80],2,20,-10),[90,205,40,80]);
  const initial=[100,200,40,80];
  // Ten move events and a single coalesced move finish at the same viewport.
  let result;for(let x=1;x<=10;x++) result=panView(initial,2,x*2,0);
  assert.deepEqual(result,panView(initial,2,20,0));
});
test('zoom anchors cursor and can exit a source beyond the zoom limit',()=>{
  const view=[10,20,100,50],point=[30,40];const next=zoomView(view,view,.5,point);
  assert.equal((point[0]-next[0])/next[2],(point[0]-view[0])/view[2]);
  assert.equal((point[1]-next[1])/next[3],(point[1]-view[1])/view[3]);
  assert.ok(zoomView([0,0,.01,.01],[0,0,1000,1000],1.25));
  assert.equal(zoomView([0,0,.01,.01],[0,0,1000,1000],.8),null);
});
test('trackpad zoom follows distance instead of jumping a notch for each tiny event',()=>{
 assert.ok(wheelFactor(1)>1 && wheelFactor(1)<1.002);
 assert.equal(wheelFactor(0),1);
 assert.ok(Math.abs(wheelFactor(10)**4-wheelFactor(40))<1e-12);
 assert.ok(Math.abs(wheelFactor(40)*wheelFactor(-40)-1)<1e-12);
 assert.equal(wheelFactor(1,1),wheelFactor(16));
});
const items=[
 {type:'path',bbox:[0,-10,10,0]},
 {type:'text',bbox:[0,-10,10,0],matrix:[1,0,0,1,0,0]},
 {type:'path',bbox:[1000,-10,1010,0]},
 {type:'text',bbox:[1000,-10,1010,0],matrix:[1,0,0,1,1000,0]},
];
test('offscreen objects and unreadable text return on pan and zoom',()=>{
 assert.deepEqual(visibleIndices(items,[-1,-11,11,1],.1),[0]);
 assert.deepEqual(visibleIndices(items,[-1,-11,11,1],3),[0,1]);
 assert.deepEqual(visibleIndices(items,[999,-11,1011,1],3),[2,3]);
});
class Root {
 constructor(){this.children=[];this.attrs={};this.writes=0;}
 setAttribute(k,v){this.attrs[k]=v;}
 get firstChild(){return this.children[0]??null;}
 insertBefore(el,cursor){el.remove();const index=cursor?this.children.indexOf(cursor):this.children.length;assert.ok(index>=0);this.children.splice(index,0,el);el.parent=this;this.writes++;}
}
class Element {
 constructor(id){this.id=id;this.parent=null;}
 remove(){if(this.parent){const p=this.parent;p.children.splice(p.children.indexOf(this),1);p.writes++;this.parent=null;}}
 get nextSibling(){const p=this.parent;return p?.children[p.children.indexOf(this)+1]??null;}
}
test('viewport preserves drawing order, reuses nodes, and restores content after panning',()=>{
 const root=new Root();const v=new SvgViewport(root,items,item=>new Element(items.indexOf(item)));
 v.update([0,0,20,20],200,200);assert.deepEqual(root.children.map(e=>e.id),[0,1]);
 const first=root.children[0],writes=root.writes;
 v.update([1,0,20,20],200,200);assert.equal(root.writes,writes);
 v.update([1000,0,20,20],200,200);assert.deepEqual(root.children.map(e=>e.id),[2,3]);
 v.update([0,0,20,20],200,200);assert.deepEqual(root.children.map(e=>e.id),[0,1]);assert.equal(root.children[0],first);
 v.update([0,0,20,20],0,0);assert.equal(root.children.length,2);
});

test('large drawing subpixel detail returns on zoom while long thin walls stay',()=>{
 const sparse=new Array(50000);
 sparse[0]={type:'path',bbox:[0,0,.1,.1]};sparse[1]={type:'path',bbox:[0,0,100,0]};sparse[2]={type:'point',bbox:[0,0,0,0]};
 assert.deepEqual(visibleIndices(sparse,[-1,-1,101,1],1),[1,2]);
 assert.deepEqual(visibleIndices(sparse,[-1,-1,101,1],10),[0,1,2]);
});
