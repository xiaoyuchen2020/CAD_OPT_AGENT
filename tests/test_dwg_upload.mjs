import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const app=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const start=app.indexOf('async function importDrawingFile(file) {');
const end=app.indexOf('$("drawing-select").onchange',start);
assert.ok(start>0 && end>start);
function harness(error){
  const elements={'file-input':{files:[{name:'测试.DWG'}],value:'selected'},'import-status':{hidden:true,textContent:''},'extract-mode':{value:'local'}};
  const calls=[];
  const listeners={};
  const documentListeners={};
  const classes=new Set();
  elements['upload-button']={
    textContent:'＋ 导入图纸',
    classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)},
    addEventListener:(name,fn)=>{listeners[name]=fn;},
  };
  const state={busy:false};
  const context={
    $:id=>elements[id], attempt:fn=>fn,
    S:state,toast:message=>calls.push(['toast',message]),
    document:{addEventListener:(name,fn)=>{documentListeners[name]=fn;}},
    busy:(on,text)=>{state.busy=on;calls.push(['busy',on,text]);},
    FormData:class{append(key,value){this[key]=value;}},
    api:async(path,options)=>{calls.push(['api',path,options.body.file]);if(error)throw Error(error);return{id:'new',conversion:/\.dwg$/i.test(options.body.file.name)?{}:null};},
    refreshDrawings:async()=>calls.push(['refresh']),selectDrawing:async(id)=>calls.push(['select',id]),
    post:async(path)=>{calls.push(['post',path]);return{id:'job'};},waitJob:async()=>calls.push(['wait']),
  };
  vm.runInNewContext(app.slice(start,end),context);
  return{elements,calls,state,classes,listeners,documentListeners,run:()=>elements['file-input'].onchange()};
}
function dragEvent(names=[]){
  return {
    dataTransfer:{types:['Files'],files:names.map(name=>({name})),dropEffect:'none'},
    preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},
  };
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
for(const name of ['图纸.DWG','图纸.DxF']){
  test(`dropping ${name} uses the existing import and extraction flow`,async()=>{
    const h=harness();
    const event=dragEvent([name]);
    h.listeners.dragover(event);
    assert.equal(event.dataTransfer.dropEffect,'copy');
    assert.equal(h.classes.has('drag-over'),true);
    await h.listeners.drop(event);
    assert.equal(event.prevented,true);
    assert.equal(event.stopped,true);
    assert.equal(h.classes.has('drag-over'),false);
    assert.equal(h.elements['upload-button'].textContent,'＋ 导入图纸');
    assert.deepEqual(h.calls.map(x=>x[0]),['busy','api','refresh','select','post','wait','busy']);
    assert.equal(h.calls[1][2].name,name);
  });
}
test('unsupported and multiple dropped files never start an import',async()=>{
  for(const names of [['图纸.pdf'],['a.dwg','b.dxf']]){
    const h=harness();
    await h.listeners.drop(dragEvent(names));
    assert.deepEqual(h.calls.map(x=>x[0]),['toast']);
  }
});
test('busy workspace rejects drops without disturbing the running operation',async()=>{
  const h=harness();h.state.busy=true;
  const event=dragEvent(['图纸.dxf']);
  h.listeners.dragover(event);
  assert.equal(event.dataTransfer.dropEffect,'none');
  assert.equal(h.classes.has('drag-over'),false);
  await h.listeners.drop(event);
  assert.deepEqual(h.calls.map(x=>x[0]),['toast']);
  assert.equal(h.state.busy,true);
});
test('file drops outside the button prevent navigation, text drags are untouched',()=>{
  const h=harness();
  const fileEvent=dragEvent(['图纸.dxf']);
  h.listeners.dragenter(fileEvent);
  h.documentListeners.drop(fileEvent);
  assert.equal(fileEvent.prevented,true);
  assert.equal(h.classes.has('drag-over'),false);
  assert.equal(h.calls.length,0);
  const textEvent=dragEvent();
  textEvent.dataTransfer.types=['text/plain'];
  h.documentListeners.dragover(textEvent);
  assert.equal(textEvent.prevented,undefined);
});
test('failed dropped DWG retains the error and restores the workspace',async()=>{
  const h=harness('转换失败');
  await assert.rejects(h.listeners.drop(dragEvent(['图纸.dwg'])),/转换失败/);
  assert.equal(h.elements['import-status'].textContent,'转换失败');
  assert.equal(h.state.busy,false);
  assert.deepEqual(h.calls.map(x=>x[0]),['busy','api','busy']);
});
