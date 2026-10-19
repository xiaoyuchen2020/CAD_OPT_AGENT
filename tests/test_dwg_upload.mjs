import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const app=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const start=app.indexOf('$("file-input").onchange = attempt(async () => {');
const end=app.indexOf('$("drawing-select").onchange',start);
assert.ok(start>0 && end>start);
function harness(error){
  const elements={'file-input':{files:[{name:'测试.DWG'}],value:'selected'},'import-status':{hidden:true,textContent:''},'extract-mode':{value:'local'}};
  const calls=[];
  const context={
    $:id=>elements[id], attempt:fn=>fn,
    busy:(on,text)=>calls.push(['busy',on,text]),
    FormData:class{append(){}},
    api:async(path)=>{calls.push(['api',path]);if(error)throw Error(error);return{id:'new',conversion:{}};},
    refreshDrawings:async()=>calls.push(['refresh']),selectDrawing:async(id)=>calls.push(['select',id]),
    post:async(path)=>{calls.push(['post',path]);return{id:'job'};},waitJob:async()=>calls.push(['wait']),
  };
  vm.runInNewContext(app.slice(start,end),context);
  return{elements,calls,run:()=>elements['file-input'].onchange()};
}
test('failed DWG conversion shows persistent error, clears selection, never selects drawing or starts extraction',async()=>{
  const h=harness('内网转换失败，本次导入已终止。');
  await assert.rejects(h.run(),/已终止/);
  assert.match(h.elements['import-status'].textContent,/已终止/);
  assert.equal(h.elements['import-status'].hidden,false);
  assert.equal(h.elements['file-input'].value,'');
  assert.deepEqual(h.calls.map(x=>x[0]),['busy','api','busy']);
  assert.equal(h.calls.at(-1)[1],false);
});
test('successful DWG conversion selects imported snapshot before extraction',async()=>{
  const h=harness();await h.run();
  assert.match(h.calls[0][2],/内网转换 DWG/);
  assert.deepEqual(h.calls.map(x=>x[0]),['busy','api','refresh','select','post','wait','busy']);
  assert.match(h.elements['import-status'].textContent,/已转换为 DXF/);
});
