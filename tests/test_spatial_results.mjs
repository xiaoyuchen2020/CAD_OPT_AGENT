import test from 'node:test';
import assert from 'node:assert/strict';
import {spatialRows} from '../web/spatial-results.mjs';
test('saved spatial rows retain measurements and exclusions do not become selected rows',()=>{
 const record={kind:'discovery_review',ranking_complete:true,items:[{item_id:'furniture'}],selected:[{item_id:'space',number:7,area_m2:12.34,perimeter_m:14.6,decision:{reason:'source enclosure reviewed'}}],evidence_url:'/local'};
 const live=spatialRows({spatial_results:[record]});
 assert.deepEqual(spatialRows({task:{spatial_results:[record]}}),live);
 assert.equal(live.length,1);assert.equal(live[0].key,'space');assert.match(live[0].measurement,/12.34/);
});
test('wall separation never gets a nearby door width',()=>{
 const r=spatialRows({spatial_results:[{kind:'connection_review',items:[{item_id:'pair',number:1,labels:['A','B'],decision:{classification:'wall',reason:'observed boundary'},selected_measurements:[],openings:[{value:1.1}]}]}]});
 assert.equal(r[0].measurement,'—');assert.match(r[0].classification,/隔断/);
});
test('follow-up table follows actual replacement subset and order',()=>{
 const selected=[1,2,3].map(number=>({item_id:String(number),number,area_m2:number,perimeter_m:4,decision:{reason:'reviewed'}}));
 const r={spatial_results:[{kind:'discovery_review',selected,ranking_complete:true}],highlight_action:{kind:'geometry',mode:'replace',object_ids:['3','1']}};
 assert.deepEqual(spatialRows(r).map(row=>row.key),['3','1']);
 r.highlight_action.object_ids=[];assert.equal(spatialRows(r).length,0);
});

test('viewer fits actual selected geometry instead of broad search extent',async()=>{
 const {spatialHighlightBounds}=await import('../web/spatial-results.mjs');
 assert.deepEqual(spatialHighlightBounds({scope:{bbox:[-1000,-1000,1000,1000]},regions:[{boundary:[[10,20],[15,20],[15,30]]}],paths:[[[8,22],[12,22]]]}),[8,20,15,30]);
});

test('area annotation difference remains visible on saved selected rows',()=>{
 const selected=[{item_id:'a',number:3,area_m2:70,perimeter_m:40,decision:{reason:'reviewed'},area_annotation_checks:[{status:'unresolved_area_difference',annotation_area_m2:137.03,difference_m2:-67.03}]}];
 const row=spatialRows({task:{spatial_results:[{kind:'discovery_review',selected,ranking_complete:true}]}})[0];
 assert.match(row.measurement,/70 m²/);assert.match(row.measurement,/137.03 m²/);assert.match(row.measurement,/口径待核查/);
});
