import test from 'node:test';
import assert from 'node:assert/strict';
import {roomBatchPresentation,roomAreaText} from '../web/room-batch.mjs';
import {highlightGeometry} from '../web/highlight.mjs';
test('all unmeasured labels highlight without invented room boundaries',()=>{
 const items=Array.from({length:6},(_,i)=>({number:i+1,status:'needs_unit_confirmation',area_m2:null,
   label_evidence:{bbox:[i*10,0,i*10+2,2]},highlight:{kind:'label_locator',bbox:[i*10,0,i*10+2,2]}}));
 const p=roomBatchPresentation({status:'needs_unit_confirmation',scope:{layout_id:'floor'},items});
 assert.equal(p.labels.length,6);assert.equal(p.boundaries.length,0);assert.equal(p.unitPending,true);
 assert.equal(highlightGeometry({source_parts:p.labels}).rings,6);
 assert.equal(roomAreaText(items[0]),'等待确认单位');
});
test('measurement and candidate boundary states remain distinct',()=>{
 const i={number:1,status:'needs_review',area_m2:null,candidate_area_m2:24,label_evidence:{bbox:[1,1,2,2]},
  highlight:{kind:'candidate_boundary',boundary:[[0,0],[6,0],[6,4],[0,4]],holes:[]}};
 const p=roomBatchPresentation({items:[i]});assert.equal(p.boundaries.length,1);
 assert.match(roomAreaText(i),/围合面积候选/);assert.match(roomAreaText({...i,status:'measured',area_m2:24}),/净面积候选/);
});

test('all delivered pages highlight without merging different frames',async()=>{
 const {combinedBatchHighlights}=await import('../web/room-batch.mjs');
 const batch={snapshot_id:'s',scope:{layout_id:'l',frame_id:'f'},items:[{region_id:'a',number:1,label_evidence:{bbox:[0,0,1,1]}}]};
 const merged=combinedBatchHighlights([batch,{...batch,items:[...batch.items,{region_id:'b',number:2,label_evidence:{bbox:[2,0,3,1]}}]},
   {...batch,scope:{layout_id:'l',frame_id:'another'}}]);
 assert.equal(merged.length,2);assert.equal(roomBatchPresentation(merged[0]).labels.length,2);
});

test('unresolved scope is unknown count and cannot claim highlights',()=>{
 const p=roomBatchPresentation({scope:null,status:'needs_scope_confirmation',total:0,items:[]});
 assert.match(p.countText,/尚未执行.*数量未知/);assert.doesNotMatch(p.countText,/0 个/);
 assert.match(p.note,/尚无可高亮/);assert.equal(p.unitPending,false);
 const zero=roomBatchPresentation({scope:{layout_id:'l'},status:'no_matching_labels',total:0,items:[]});
 assert.match(zero.countText,/0 个匹配/);
});

test('visual boundary keeps gap overlays and unknown units without area',()=>{
 const boundary=[[0,0],[6,0],[6,4],[0,4],[0,0]], gaps=[[[2,0],[3,0]]];
 const item={status:'needs_unit_confirmation',area_m2:null,candidate_area_m2:null,number:1,
   label_evidence:{bbox:[1,1,2,2]},highlight:{kind:'candidate_boundary',boundary,gaps}};
 const p=roomBatchPresentation({status:'needs_unit_confirmation',scope:{layout_id:'l'},items:[item]});
 assert.deepEqual(p.boundaries[0].gaps,gaps);assert.equal(p.unitPending,true);
 assert.match(p.note,/开口闭合假设/);assert.equal(roomAreaText(item),'等待确认单位');
});

test('explicit replacement displays only selected originals without mutating history',async()=>{
 const {displayedBatches}=await import('../web/room-batch.mjs');
 const items=[1,2,3,4].map(number=>({number,region_id:`r${number}`}));
 const batch={batch_id:'b',snapshot_id:'s',items,total:4,has_more:true};
 const result={room_batches:[batch],highlight_action:{mode:'replace',batch_id:'b',items:[items[1],items[3]]}};
 const shown=displayedBatches(result);
 assert.deepEqual(shown[0].items.map(i=>i.number),[2,4]);
 assert.equal(shown[0].has_more,false);assert.equal(batch.items.length,4);
 result.highlight_action={mode:'replace',kind:'geometry'};
 assert.deepEqual(displayedBatches(result),[]);
});
