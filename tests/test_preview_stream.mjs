import test from 'node:test';
import assert from 'node:assert/strict';
import {PreviewStream} from '../web/preview-stream.mjs';
import {visibleIndices} from '../web/render-viewport.mjs';
const manifest={snapshot_id:'s',revision:'rev',total_items:5,bases:{L:'base-0',P:'base-1'},chunks:[
 {file:'text-0',layout_id:'L',bbox:[0,-10,10,0],max_height:1},
 {file:'text-1',layout_id:'L',bbox:[1000,-10,1010,0],max_height:100},
]};
const line={type:'path',layout_id:'L',bbox:[0,-10,100,0]},small={type:'text',layout_id:'L',bbox:[0,-10,10,0],matrix:[1,0,0,1,0,0]};
const flush=()=>new Promise(setImmediate);
test('load overview first, restore local text in original order, and isolate layouts',async()=>{
 const calls=[];const packets={'base-0':[[0,line]],'base-1':[[4,{...line,layout_id:'P'}]],'text-0':[[1,small]],'text-1':[[2,{...small,bbox:[1000,-10,1010,0]}]]};
 const stream=new PreviewStream(manifest,async url=>{calls.push(url.split('/').at(-1));return packets[calls.at(-1)];});
 stream.setTarget('L',[0,0,100,100],100,100);await stream.wait();
 assert.deepEqual(calls,['base-0']);assert.equal(stream.items[1],undefined);
 stream.setTarget('L',[0,0,20,20],200,200);await stream.wait();assert.deepEqual(calls,['base-0','text-0']);
 assert.deepEqual(visibleIndices(stream.items,[-1,-11,11,1],10,'L'),[0,1]);
 stream.setTarget('P',[0,0,20,20],200,200);await stream.wait();
 assert.deepEqual(visibleIndices(stream.items,[-1,-11,11,1],10,'P'),[4]);
 stream.setTarget('L',[0,0,20,20],200,200);await stream.wait();assert.equal(calls.length,3);
});
test('failed chunks are visible as failures and retryable rather than complete',async()=>{
 let fail=true;const stream=new PreviewStream(manifest,async()=>{if(fail)throw Error('offline');return [[0,line]];});
 stream.setTarget('L',[0,0,100,100],100,100);await assert.rejects(stream.wait(),/offline/);
 assert.equal(stream.ready,false);fail=false;stream.retry();await stream.wait();assert.equal(stream.ready,true);
});
test('switching drawings aborts outstanding work and ignores late data',async()=>{
 let finish,signal;const stream=new PreviewStream(manifest,(_,options)=>{signal=options.signal;return new Promise(r=>finish=r);});
 stream.setTarget('L',[0,0,100,100],100,100);const waiting=stream.wait();stream.dispose();await waiting;
 assert.ok(signal.aborted);finish([[0,line]]);await flush();assert.equal(stream.items[0],undefined);
});
test('rapid viewport changes have at most four concurrent requests and drop stale queued work',async()=>{
 const chunks=Array.from({length:20},(_,i)=>({file:'text-'+i,layout_id:'L',bbox:[0,-10,10,0],max_height:100}));
 const pending=[];const stream=new PreviewStream({...manifest,chunks},url=>new Promise(resolve=>pending.push({url,resolve})));
 stream.setTarget('L',[0,0,100,100],100,100);assert.equal(pending.length,4);
 stream.setTarget('P',[0,0,100,100],100,100);
 pending[0].resolve([]);await flush();assert.ok(pending[4].url.endsWith('base-1'));assert.equal(stream.pending.size,4);
 stream.dispose();for(const p of pending)p.resolve([]);
});

test('geometry packets use screen size, evict offscreen data and restore on return',async()=>{
 const chunks=[0,1000].map((x,i)=>({file:'geom-'+i,layout_id:'L',bbox:[x,-1,x+1,0],max_height:1,min_pixels:.8}));
 const calls=[];const s=new PreviewStream({snapshot_id:'s',revision:'r',total_items:10001,bases:{},chunks},async url=>{
  const i=Number(url.split('-').at(-1));calls.push(i);return [[i,{type:'path',layout_id:'L',bbox:chunks[i].bbox}]];
 });
 s.cacheLimit=1;
 s.setTarget('L',[0,0,1000,1000],100,100);await s.wait();assert.equal(calls.length,0);
 s.setTarget('L',[0,0,10,10],100,100);await s.wait();assert.ok(s.items[0]);
 s.setTarget('L',[1000,0,10,10],100,100);await s.wait();assert.equal(s.items[0],undefined);assert.ok(s.items[1]);
 s.setTarget('L',[0,0,10,10],100,100);await s.wait();assert.ok(s.items[0]);assert.equal(s.items[1],undefined);
 assert.deepEqual(calls,[0,1,0]);assert.equal(s.residentCount,1);
});
