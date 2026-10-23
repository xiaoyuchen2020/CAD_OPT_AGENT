import test from 'node:test';
import assert from 'node:assert/strict';
import {eventsForRun,visibleEvents,runSummary} from '../web/run-inspector.mjs';

test('terminal runs never display unfinished records as still running',()=>{
  const run={journal_version:1,status:'failed',run_events:[{id:'a',kind:'model',status:'running'}]};
  assert.equal(eventsForRun(run)[0].status,'interrupted');
  assert.equal(run.run_events[0].status,'running');
});
test('filter, search and replay use chronological recorded events',()=>{
  const run={journal_version:1,status:'completed',run_events:[
    {id:'a',title:'用户上下文',kind:'input',status:'completed'},
    {id:'b',title:'search_sources',kind:'tool',status:'completed'},
    {id:'c',title:'校验',kind:'validation',status:'failed'},
    {id:'d',title:'校验',kind:'validation',status:'completed'}]};
  assert.deepEqual(visibleEvents(run,'model').map(e=>e.id),['a']);
  assert.deepEqual(visibleEvents(run,'validation','',2).map(e=>e.id),['c']);
  assert.deepEqual(visibleEvents(run,'all','SEARCH').map(e=>e.id),['b']);
  assert.equal(runSummary(run).failedChecks,1);
  assert.equal(runSummary(run).tokens,null);
  assert.equal(runSummary({...run,usage:[{}]}).tokens,null);
  assert.equal(runSummary({...run,usage:[{total_tokens:12},{total_tokens:8}]}).tokens,20);
});
test('legacy trace does not invent prompts, results, or validation',()=>{
  const run={status:'completed',trace:[{tool:'read_table',status:'completed',arguments:{limit:5}}]};
  const events=eventsForRun(run);
  assert.equal(events.length,1);assert.deepEqual(events[0].sections,{});
  assert.deepEqual(events[0].legacy.arguments,{limit:5});
});
test('zero usage is shown only for a completed automatic source workflow with no model calls',()=>{
 const source_records={mode:'auto',model_requests:0,fallback:false};
 const run={status:'completed',journal_version:1,result:{source_records},run_events:[]};
 assert.equal(runSummary(run).tokens,0);
 assert.equal(runSummary({...run,status:'failed'}).tokens,null);
 assert.equal(runSummary({...run,result:{source_records:{...source_records,mode:'review'}}}).tokens,null);
 assert.equal(runSummary({...run,run_events:[{kind:'model'}]}).tokens,null);
});
