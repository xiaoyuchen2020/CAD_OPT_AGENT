import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const C=require('../cad_agent/fleet/static/operations-core.js');

test('unknown consumption stays unknown while explicitly reported zero remains zero',()=>{
  assert.equal(C.usage({known:false,total_tokens:0}).total,'用量未完整报告');
  assert.equal(C.usage({known:true,total_tokens:0,reported_tokens:0}).total,'0');
  assert.equal(C.usage({}).model_calls,'未报告');
  assert.equal(C.usage({}).cost,'费用未报告');
  assert.equal(C.number(null),'未报告');
  assert.equal(C.number(NaN),'未报告');
  assert.equal(C.number(-1),'未报告');
  assert.equal(C.bytes(0),'0.0 KB');
});
test('configuration only submits explicit changes and respects enforceable boundaries',()=>{
  assert.deepEqual(C.configuration({'budget.max_rounds':'8','sandbox.memory_mb':'256','validator.require_evidence':'false','validator.min_evidence_count':'0','budget.max_tool_calls':''}),{budget:{max_rounds:8},sandbox:{memory_mb:256},validator:{require_evidence:false,min_evidence_count:0}});
  for(const entries of [{'sandbox.memory_mb':513},{'sandbox.memory_mb':63},{'budget.max_rounds':1.5},{'sandbox.network_enabled':'true'},{'validator.require_evidence':'no'},{'budget.max_rounds':''}])assert.throws(()=>C.configuration(entries));
});
test('runtime graph contains only reported nodes and valid reported relationships',()=>{
  const layout=C.layoutGraph({nodes:[{id:'agent',kind:'agent'},{id:'tool',kind:'tool'},{id:'disconnected'},{id:'agent',kind:'bogus'}],edges:[{source:'agent',target:'tool'},{source:'tool',target:'agent'},{source:'missing',target:'tool'}]});
  assert.equal(layout.nodes.length,3);assert.equal(layout.edges.length,2);
  assert.deepEqual(layout.nodes.map(n=>n.id),['agent','tool','disconnected']);
  assert.equal(layout.nodes[0].kind,'agent');
  assert.ok(layout.nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
  assert.deepEqual(C.layoutGraph({}).nodes,[]);
});
test('switching selection or hiding the page invalidates stale network results',()=>{
  const reads=new C.ReadEpoch(),first=reads.begin();assert.equal(reads.current(first),true);
  const second=reads.begin();assert.equal(first.signal.aborted,true);assert.equal(reads.current(first),false);assert.equal(reads.current(second),true);
  reads.invalidate();assert.equal(second.signal.aborted,true);assert.equal(reads.current(second),false);
});
test('task identifiers stay encoded and externally supplied navigation stays local',()=>{
  assert.equal(C.basePath('job','a/b?c'),'/jobs/a%2Fb%3Fc');
  assert.equal(C.basePath('exploration','exp 1'),'/exploration-runs/exp%201');
  for(const p of ['https://bad.test','//bad.test','/\\bad.test','/x\n'])assert.equal(C.safePath(p),null);
  assert.equal(C.safePath('/nodes/a/'),'/nodes/a/');
});
test('UI renders untrusted labels as text and controls use revision and idempotency',()=>{
  const ui=readFileSync(new URL('../cad_agent/fleet/static/operations.js',import.meta.url),'utf8');
  assert.doesNotMatch(ui,/\.innerHTML\s*=/);
  assert.match(ui,/expected_revision:snapshot\.revision/);
  assert.match(ui,/body\.command_id=previous\.body\.command_id/);
  assert.match(ui,/控制版本已被其他操作更新/);
  assert.match(ui,/visibilitychange/);
  assert.match(ui,/saveDraft\(\)/);
});

test('terminal tasks permit disabling supervision but never enabling or resuming it',()=>{
  assert.equal(C.supervisionAllowed({status:'completed'},false),true);
  assert.equal(C.supervisionAllowed({status:'completed'},true),false);
  assert.equal(C.supervisionAllowed({status:'partial',closed:true},true),false);
  assert.equal(C.supervisionAllowed({status:'waiting_input'},true),true);
  assert.equal(C.supervisionAllowed(null,false),false);
});
test('E0 dispatch configuration bounds match backend admission limits',()=>{
  assert.deepEqual(C.configuration({'dispatch_budget.max_case_starts':10000,'dispatch_budget.max_concurrent_cases':100,'dispatch_budget.max_reported_tokens':100000000}),{dispatch_budget:{max_case_starts:10000,max_concurrent_cases:100,max_reported_tokens:100000000}});
  for(const item of [{'dispatch_budget.max_case_starts':10001},{'dispatch_budget.max_concurrent_cases':101},{'dispatch_budget.max_reported_tokens':100000001}])assert.throws(()=>C.configuration(item));
});
test('rejected controls retain their own and child rejection reasons',()=>{
  const reasons=C.auditReasons({boundary:'before_model',reason:'runtime unsupported',error:'lease expired',targets:[{job_id:'child-a',reason:'already finished'}]});
  assert.deepEqual(reasons,['runtime unsupported','lease expired','child-a：already finished']);
});
test('supervisor proposed guidance and submitted guidance are visibly distinguished',()=>{
  assert.match(C.guidanceState({guidance_status:'advisory_only_or_target_changed'}),/未发送/);
  assert.match(C.guidanceState({guidance_status:'not_submitted_control_changed'}),/控制版本已改变/);
  assert.match(C.guidanceState({applied_command_id:'c1'}),/实际生效见控制记录/);
});
test('E0 effective configuration only reports consistent acknowledged child values',()=>{
  const base={desired:{budget:{max_rounds:8}},effective:{dispatch_budget:{max_case_starts:3}}};
  assert.equal(C.effectiveField(base,'budget.max_rounds').state,'pending');
  assert.equal(C.effectiveField(base,'dispatch_budget.max_case_starts').value,3);
  const child=value=>({job_id:'child',effective:{budget:{max_rounds:value}}});
  assert.equal(C.effectiveField({...base,effective_by_job:[child(8),child(8)]},'budget.max_rounds').value,8);
  assert.equal(C.effectiveField({...base,effective_by_job:[child(8),child(12)]},'budget.max_rounds').state,'mixed');
  assert.equal(C.effectiveField({...base,effective_by_job:[child(8),{effective:{}}]},'budget.max_rounds').state,'pending');
});
test('stale execution telemetry remains distinct from a fresh center response',()=>{
  const state=C.telemetryState({kind:'exploration',updated_at:'2026-10-29T12:00:00Z',telemetry_stale:true,telemetry_age_seconds:90,resources:{items:[{sampled_at:'2026-10-29T11:58:30Z'},{sampled_at:'2026-10-29T11:59:00Z'}]}});
  assert.equal(state.stale,true);assert.equal(state.age,90);assert.equal(state.sampled_at,'2026-10-29T11:58:30Z');
  assert.equal(C.telemetryState({kind:'exploration',updated_at:'2026-10-29T12:00:00Z',telemetry_stale:true}).sampled_at,null);
});

test('enabled supervision without any review is waiting, never a fabricated running call',()=>{
  const graph={nodes:[{id:'job:j1',kind:'coordinator',status:'running'}],edges:[]};
  const projected=C.withSupervisionGraph(graph,{kind:'job',target_id:'j1',status:'running',config:{enabled:true,model_profile:'api_sol'},reviews:[]},{id:'j1',kind:'job'});
  assert.equal(projected.nodes.find(n=>n.kind==='supervisor').status,'waiting');
  assert.equal(projected.edges.some(e=>e.kind==='supervision_snapshot'),false);
  assert.equal(projected.edges[0].label,'已配置，尚未检查');
  assert.equal(C.withSupervisionGraph(graph,{config:{enabled:false},reviews:[]}).nodes.length,1);
});
test('advisory supervision renders its actual review and snapshot read without guidance delivery',()=>{
  const graph={nodes:[{id:'job:j1',kind:'coordinator',status:'running'},{id:'agent:r1',kind:'agent',status:'running'}],edges:[]};
  const frozen=JSON.stringify(graph);
  const review={review_id:'review_1',status:'running',model_profile:'api_astra',selected_binding:{id:'j1',run_id:'r1',attempt_id:'a1',node_id:'a'},operational_snapshot:{id:'j1',kind:'job',status:'running'}};
  const result=C.withSupervisionGraph(graph,{config:{enabled:true,mode:'advisory'},status:'running',reviews:[review]},{id:'j1',kind:'job'});
  const supervisor=result.nodes.find(n=>n.kind==='supervisor');assert.equal(supervisor.status,'running');assert.equal(supervisor.detail.selected_binding.run_id,'r1');
  assert.equal(result.edges[0].source,'job:j1');assert.equal(result.edges[0].label,'读取运行快照');
  assert.equal(result.edges.some(e=>e.kind==='supervision_guidance'),false);
  assert.equal(JSON.stringify(graph),frozen);
});
test('supervision guidance delivery follows the real control receipt, not just model advice',()=>{
  const graph={nodes:[{id:'exp1',kind:'coordinator',status:'running'}],edges:[]};
  const review={review_id:'review_2',status:'completed',guidance:'Check source',guidance_status:'advisory_only_or_target_changed',operational_snapshot:{id:'exp1',kind:'exploration'}};
  const state={config:{enabled:true,mode:'advisory'},reviews:[review]};
  assert.equal(C.withSupervisionGraph(graph,state,{id:'exp1',kind:'exploration'}).edges.length,1);
  const submitted={...state,reviews:[{...review,applied_command_id:'control1',guidance_status:'submitted_pending_safe_point'}]};
  const pending=C.withSupervisionGraph(graph,submitted,{id:'exp1',kind:'exploration',control:{commands:[]}}).edges.find(e=>e.kind==='supervision_guidance');
  assert.equal(pending.status,'pending');assert.match(pending.label,/待生效/);
  const applied=C.withSupervisionGraph(graph,submitted,{id:'exp1',kind:'exploration',control:{commands:[{command_id:'control1',status:'applied'}]}}).edges.find(e=>e.kind==='supervision_guidance');
  assert.equal(applied.status,'applied');assert.equal(applied.label,'引导已生效');
});
test('supervision overlay preserves last review status when disabled and ignores absent graph bindings',()=>{
  const graph={nodes:[{id:'job:new',kind:'coordinator'}],edges:[]};
  const result=C.withSupervisionGraph(graph,{status:'disabled',config:{enabled:false},reviews:[{review_id:'review_old',status:'interrupted',selected_binding:{run_id:'old'},operational_snapshot:{id:'old',kind:'job'}}]},{id:'new',kind:'job'});
  assert.equal(result.nodes.find(n=>n.kind==='supervisor').status,'interrupted');assert.equal(result.edges.length,0);
  assert.ok(result.nodes.find(n=>n.kind==='supervisor').detail.selected_binding);
});
