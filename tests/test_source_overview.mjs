import test from 'node:test';
import assert from 'node:assert/strict';
import {drawingKey,filterSources,groupedDrawings,changes} from '../cad_agent/fleet/static/source-overview-model.mjs';
import {overlayGeometry,visibleSources,clusterSources} from '../web/source-overlay.mjs';
const source={snapshot_id:'s',source_sha256:'hash',node_id:'a',layout_id:'Model',bbox:[0,0,10,10],quote:'厚度150mm'};
const row={source_id:'one',source,locatable:true,selected:true,stages:['read','analysis_input'],associations:[{rule_ref:'R1',workflow_id:'run1',selection_kind:'workflow_input'}],data:[],issues:[]};
test('rule and workflow filters derive selection from actual associations',()=>{
  assert.equal(filterSources([row],{rule:'R2',stage:'analysis_input'}).length,0);
  assert.equal(filterSources([row],{rule:'R1',stage:'analysis_input'}).length,1);
  assert.equal(filterSources([row],{workflow:'run2'}).length,0);
});
test('different converted versions and nodes remain separate viewer routes',()=>{
  assert.notEqual(drawingKey(source),drawingKey({...source,source_sha256:'new'}));
  assert.notEqual(drawingKey(source),drawingKey({...source,node_id:'c'}));
  assert.equal(groupedDrawings([row,{...row,source:{...source,source_sha256:'new'}}]).length,2);
});
test('unlocatable sources remain filterable and counted',()=>{
  const missing={...row,locatable:false,issues:['单位未确认']};
  assert.equal(filterSources([row,missing],{missing:true}).length,1);
  assert.equal(filterSources([row,missing],{stage:'pending'}).length,1);
});
test('geometry displays true measurement line and endpoints, including zero-height bounds',()=>{
  const g=overlayGeometry({...source,measurement_line:[[3,5],[13,5]]});
  assert.equal(g.d,'M3,-5 L13,-5');assert.deepEqual(g.bbox,[3,5,13,5]);
});
test('viewport clipping observes WCS sign and layout',()=>{
  assert.equal(visibleSources([row],[0,-10,20,20],'Model').length,1);
  assert.equal(visibleSources([row],[0,30,20,20],'Model').length,0);
  assert.equal(visibleSources([row],[0,-10,20,20],'Paper').length,0);
});
test('dense overlays aggregate without losing source membership',()=>{
  const rows=Array.from({length:3000},(_,i)=>({...row,source_id:String(i)}));
  const groups=clusterSources(rows,[0,-1000,1000,1000],800,600);
  assert.equal(groups.length,1);assert.equal(groups.flat().length,3000);
});
test('new-source count excludes repeated refreshes and removals',()=>{
  assert.equal(changes({items:[row]},{items:[row,{...row,source_id:'new'}]}),1);
  assert.equal(changes({items:[row]},{items:[row]}),0);
});
