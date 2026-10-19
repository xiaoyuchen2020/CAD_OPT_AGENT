import test from 'node:test';
import assert from 'node:assert/strict';
import {highlightGeometry,renderHighlight,MAX_HIGHLIGHT_POINTS} from '../web/highlight.mjs';
const box=[0,0,10,10];
const concave=[[0,0],[10,0],[10,3],[3,3],[3,10],[0,10]];
// Independent SVG winding-number probe of the generated path.
function winding(d,x,y) {
 let n=0;
 for(const sub of d.split('Z').filter(s=>s.trim())){
  const p=[...sub.matchAll(/[ML]([^, ]+),([^ ]+)/g)].map(m=>[+m[1],+m[2]]);
  for(let i=0;i<p.length;i++){
   const a=p[i],b=p[(i+1)%p.length],cross=(b[0]-a[0])*(y-a[1])-(x-a[0])*(b[1]-a[1]);
   if(a[1]<=y&&b[1]>y&&cross>0)n++;
   if(a[1]>y&&b[1]<=y&&cross<0)n--;
  }
 }return n;
}
test('concave WCS outline, reversed holes and separate islands retain coverage',()=>{
 const source={bbox:box,highlight_regions:[{boundary:concave,holes:[[[1,1],[2,1],[2,2],[1,2]]]},{boundary:[[20,0],[25,0],[25,5],[20,5]].reverse()}]};
 const g=highlightGeometry(source);
 assert.equal(g.mode,'regions');assert.deepEqual(g.bbox,[0,0,25,10]);
 for(const [x,y,inside] of [[.5,.5,true],[1.5,1.5,false],[6,6,false],[22,2,true],[15,2,false]])assert.equal(winding(g.d,x,-y)!==0,inside);
 assert.strictEqual(highlightGeometry(source),g);
});
test('parts retain gaps; overlapping components never punch holes',()=>{
 const g=highlightGeometry({bbox:box,parts:[{bbox:[0,0,4,4]},{bbox:[2,0,6,4]},{bbox:[8,0,10,4]},{bbox:[8,0,10,4]}]});
 assert.equal(g.rings,3);assert.notEqual(winding(g.d,3,-2),0);assert.equal(winding(g.d,7,-2),0);
});
test('narrow evidence uses source_parts and mixed polygon/box components',()=>{
 const g=highlightGeometry({bbox:box,source_parts:[{bbox:box,highlight_regions:[{boundary:concave}]},{bbox:[20,0,22,2]}]});
 assert.equal(winding(g.d,6,-6),0);assert.notEqual(winding(g.d,21,-1),0);
});
test('invalid/oversized outlines fall back without drawing partial polygons',()=>{
 for(const boundary of [[[NaN,1],[2,2],[3,3]],[[0,0],[1,1],[2,2]],Array(MAX_HIGHLIGHT_POINTS+1).fill([0,0])]){
  const g=highlightGeometry({bbox:box,highlight_regions:[{boundary}]});assert.ok(g.fallback);assert.equal(g.mode,'components');assert.equal(g.rings,1);
 }
 const g=highlightGeometry({bbox:box,component_boxes:Array(513).fill(box)});assert.equal(g.mode,'bbox');
 assert.equal(highlightGeometry({bbox:[0,0,NaN,1]}).d,'');
});
test('large WCS origins keep valid tiny shapes and only one SVG node',()=>{
 const source={boundary:[[1e9,1e9],[1e9+1,1e9],[1e9+1,1e9+1],[1e9,1e9+1]]};
 const root={dataset:{},ownerDocument:{createElementNS:()=>({attrs:{},setAttribute(k,v){this.attrs[k]=v;}})},replaceChildren(...nodes){this.nodes=nodes;}};
 renderHighlight(root,source);assert.equal(root.nodes.length,1);assert.equal(root.nodes[0].attrs['fill-rule'],'nonzero');assert.equal(root.dataset.highlightMode,'regions');
});
