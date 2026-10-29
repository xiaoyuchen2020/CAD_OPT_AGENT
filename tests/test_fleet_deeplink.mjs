import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const start=source.indexOf('async function openLinkedDrawing(');
const end=source.indexOf('\nawait attempt(',start);
function fixture(run=null){
  const calls=[];
  const context={URLSearchParams,encodeURIComponent,S:{drawings:[{id:'first'},{id:'second'}]},
    api:async url=>{calls.push(['get',url]);return run;},
    selectDrawing:async id=>{calls.push(['select',id]);context.S.drawing={id};},
    runInspector:{onOpen:r=>calls.push(['open',r.id])}};
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);
  return {calls,open:q=>context.openLinkedDrawing(new URLSearchParams(q))};
}
test('fleet link selects correct snapshot and restores saved answer without new analysis',async()=>{
  const f=fixture({id:'run-2',snapshot_id:'second'});await f.open('snapshot_id=second&run_id=run-2');
  assert.deepEqual(f.calls,[['get','/api/chat-runs/run-2'],['select','second'],['open','run-2']]);
});
test('mismatched and missing snapshot links fail without selecting an unrelated drawing',async()=>{
  const f=fixture({id:'run-2',snapshot_id:'second'});
  await assert.rejects(f.open('snapshot_id=first&run_id=run-2'),/不一致/);
  assert.equal(f.calls.length,1);
  const missing=fixture();await assert.rejects(missing.open('snapshot_id=missing'),/没有链接指定/);
  assert.deepEqual(missing.calls,[]);
});
test('ordinary startup and load performance behavior remain unchanged',async()=>{
  const f=fixture();await f.open('');assert.deepEqual(f.calls,[['select','first']]);
  const perf=fixture();await perf.open('loadPerf=1');assert.deepEqual(perf.calls,[]);
});
