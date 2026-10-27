import test from 'node:test';
import assert from 'node:assert/strict';
import {workspaceAction, drawingDownloads} from '../web/workspace-actions.mjs';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

test('explicit interaction requests remain local and semantic questions stay with the Agent', () => {
  assert.equal(workspaceAction(' 下载原图。 '), 'download');
  assert.equal(workspaceAction('框选找相似'), 'similarity');
  assert.equal(workspaceAction('测量选中房间'), 'measure-evidence');
  for (const question of ['测量一层卧室的净面积', '这张图纸有哪些房间？',
    '查找外墙做法并下载汇总结果', '识别房间', '请判断这些门是否相似', '下载外墙做法表格', 'constructor', '__proto__']) {
    assert.equal(workspaceAction(question), null, question);
  }
});

test('download choices use the selected snapshot and expose converted files only when available', () => {
  assert.deepEqual(drawingDownloads({id:'snapshot', source_format:'DXF'}), [
    {label:'原始 DXF', href:'/api/drawings/snapshot/source'},
  ]);
  assert.deepEqual(drawingDownloads({id:'snapshot/2', source_format:'DWG', conversion:{}}), [
    {label:'原始 DWG', href:'/api/drawings/snapshot%2F2/source'},
    {label:'转换 DXF', href:'/api/drawings/snapshot%2F2/dxf'},
    {label:'图纸 JSON', href:'/api/drawings/snapshot%2F2/oda-json'},
  ]);
});

test('Agent questions keep model, subscription, evidence and cancellation plumbing without auto-opening details', async () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function ask(');
  const end = source.indexOf('\nfunction renderTrace(', start);
  const element = () => ({append(){}, replaceChildren(){}, querySelector(){return null;},
    value:'', checked:false, hidden:true, disabled:false, scrollHeight:100});
  const elements = new Map();
  const $ = id => {if (!elements.has(id)) elements.set(id,element());return elements.get(id);};
  $('chat-mode').value = 'agent';
  $('agent-runtime').value = 'responses_subscription';
  $('model-profile').value = 'siwc_sol';
  $('source-record-mode').value = 'auto';
  const calls = [];
  const state = {generation:1,drawing:{id:'snapshot'},factSet:'facts',selected:null,
    parentRun:'prior',relatedDrawings:new Map(),retrievalSupported:false};
  const result = {answer:'图纸原文',citations:[]};
  let updates = 0;
  const context = {S:state,$,node:element,workspaceAction,handleWorkspaceAction:async()=>false,
    post:async(path,payload)=>{calls.push({path,payload});return{id:'run',status:'running',stage:'读取来源'};},
    api:async()=>({id:'run',status:'completed',result}),
    runInspector:{open:()=>{throw Error('Unexpected inspector opening');},update:()=>updates++},
    runDetailsButton:element,sourceRecordView:()=>null,answerCitationLinks:element,
    publishedCitations:()=>[],setTimeout:fn=>fn(),toast:message=>{throw Error(message);}};
  vm.runInNewContext(source.slice(start,end)+'\nthis.ask=ask;',context);
  await context.ask('核对外墙材料并给出依据');
  assert.equal(calls.length,1);
  assert.equal(calls[0].path,'/api/chat-runs');
  assert.equal(calls[0].payload.runtime,'responses_subscription');
  assert.equal(calls[0].payload.model_profile,'siwc_sol');
  assert.equal(calls[0].payload.parent_run_id,'prior');
  assert.equal(calls[0].payload.allow_fallback,false);
  assert.equal(calls[0].payload.source_read_batching,false);
  assert.equal(updates,1);
  assert.equal(state.parentRun,'run');
  assert.equal(state.chatBusy,false);
  assert.equal($('cancel-chat-button').hidden,true);
  assert.equal($('send-button').disabled,false);
  state.busy = true;
  const notices = [];
  context.toast = message => notices.push(message);
  await context.ask('图纸解析过程中再次提问');
  assert.equal(calls.length,1);
  assert.equal(notices.length,1);
});

test('changing scope cancels picking and clears evidence from the previous scope', async () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const state = {drawing:{frames:[]},roomPick:true,similarityPick:false,evidence:{region_id:'old'}};
  const elements = new Map();
  const $ = id => {if (!elements.has(id)) elements.set(id,{}); return elements.get(id);};
  const context = {S:state,$,attempt:fn=>fn,cancelViewerInteraction:()=>{state.roomPick=false;state.similarityPick=false;},
    clearSelection:()=>{state.evidence=null;},renderScene(){},loadFacts:async()=>{}};
  vm.runInNewContext(source.slice(source.indexOf('$("layout-select").onchange'),source.indexOf('\nfor (const t of ["facts", "sources"])')),context);
  $('layout-select').onchange({target:{value:'layout2'}});
  assert.equal(state.roomPick,false);
  assert.equal(state.evidence,null);
  state.similarityPick=true;state.evidence={region_id:'another'};
  await $('frame-select').onchange();
  assert.equal(state.similarityPick,false);
  assert.equal(state.evidence,null);
  state.drawing=null;
  await $('frame-select').onchange();
});

test('cross-drawing answer evidence preserves the conversation and ignores stale responses', async () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const state = {generation:1,drawing:{id:'a'}};
  const evidence = {snapshot_id:'b',region_id:'source-b'};
  const opened = [], switches = [];
  const context = {S:state,api:async()=>evidence,toast:()=>{},
    selectDrawing:async(id,options)=>{switches.push({id,options});state.drawing={id};state.generation++;},
    showSource:source=>opened.push(source)};
  vm.runInNewContext(source.slice(source.indexOf('async function openAnswerCitation('),source.indexOf('\nfunction answerCitationLinks('))+'\nthis.open=openAnswerCitation;',context);
  assert.equal(await context.open({evidence_url:'/evidence/b'}),true);
  assert.equal(switches[0].id,'b');
  assert.equal(switches[0].options.keepConversation,true);
  assert.equal(opened[0],evidence);
  context.api=async()=>{state.generation++;return evidence;};
  await context.open({evidence_url:'/evidence/stale'});
  assert.equal(opened.length,1);
});

test('clearing or changing drawings cannot orphan a running Agent conversation', async () => {
  const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
  const state = {chatBusy:true,generation:7,drawing:{id:'current'},parentRun:'running'};
  const selector = {value:'other'};
  const messages = [];
  const context = {S:state,$:()=>selector,toast:message=>messages.push(message),
    runInspector:{reset(){throw Error('running view was reset');},close(){throw Error('running view was closed');}}};
  const clear = source.slice(source.indexOf('function clearChat('),source.indexOf('\nfunction bindSuggestions('));
  const select = source.slice(source.indexOf('async function selectDrawing('),source.indexOf('\nasync function ',source.indexOf('async function selectDrawing(')+1));
  vm.runInNewContext(clear+'\n'+select+'\nthis.clearChat=clearChat;this.selectDrawing=selectDrawing;',context);
  context.clearChat();
  await context.selectDrawing('other');
  assert.equal(state.generation,7);
  assert.equal(state.parentRun,'running');
  assert.equal(selector.value,'current');
  assert.equal(messages.length,2);
});
