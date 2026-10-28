"""Evidence review gates must not convert candidate retrieval into sufficiency."""
import json
import pytest
from cad_agent.analysis.tasks import TaskPlan,PlanStep
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from test_task_orchestration import session,plan
from test_workflow_contract import complete_room_operations,room_choices
from test_siwc import connected_lease
from test_siwc_stopping import tool


def names(s):return {t['function']['name'] for t in s.offered_tools(planning_only=True)}


def batch_and_title(workspace):
    s,q=session(workspace);s.task.plan(plan(s))
    before=names(s)
    s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2','q3']})
    assert 'analyze_room_batch' in before and 'prepare_room_boundaries' in names(s)
    assert not s.focus.review_ready()
    complete_room_operations(s)
    rid=next(r.region_id for r in workspace.regions(s.request.snapshot_id) if r.text=='三层平面图')
    return s,rid


def final_for(s,rid):
    eid=next(e['evidence_id'] for e in s.evidence.values() if e['region_id']==rid)
    return {'claims':[{'text':'标题为三层平面图。','state':'observed','evidence_ids':[eid],'question_part_ids':['q4']}]}


def test_search_does_not_open_delivery_gate_or_stop_independent_work(workspace):
    s,rid=batch_and_title(workspace)
    s.invoke_tool('search_sources',{'query':'三层平面图','task_ids':['q4']})
    assert not s.focus.review_ready() and 'search_sources' in names(s)
    assert not s.policy.stop_reason and s.task.data['subtasks'][1]['status']=='waiting_input'
    s.invoke_tool('read_source_context',{'region_id':rid,'neighbors':0,'task_ids':['q4']})
    assert s.focus.review_ready() and names(s)=={'finish_analysis','request_toolset','read_evidence_record'}
    assert not s.policy.stop_reason  # Reader coverage requests review, not automatic success.


def test_accepted_delivery_returns_without_another_model_call(workspace,monkeypatch):
    s,q=session(workspace);connected_lease(workspace,monkeypatch);payloads=[]
    rid=next(r.region_id for r in workspace.regions(s.request.snapshot_id) if r.text=='三层平面图')
    def invoke(self,runner,payload):
        payloads.append(payload)
        if len(payloads)==1:return tool('plan_task',plan(s).model_dump(),'p')
        if len(payloads)==2:return tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2','q3']},'b')
        if len(payloads)==3:return tool('prepare_room_boundaries',{'batch_id':s.room_batch['batch_id'],'task_ids':['q2','q3']},'geo')
        if len(payloads)==4:return tool('select_room_boundaries',room_choices(s)|{'task_ids':['q2','q3']},'select')
        if len(payloads)==5:return tool('read_source_context',{'region_id':rid,'neighbors':0,'task_ids':['q4']},'r')
        if len(payloads)==6:return tool('finish_analysis',final_for(s,rid),'done')
        pytest.fail('accepted answer must not request another summary')
    monkeypatch.setattr(ResponsesTransport,'request',invoke)
    result=s.run()
    assert len(payloads)==6 and result['task']['status']=='waiting_input'
    assert result['efficiency']['stop_reason']=='partial_delivery'
    assert result['citations'] and result['trace'][-1]['status']=='completed'
    assert all(t['function']['name'] not in ('render_region','measure_geometry') for t in s.offered_tools(planning_only=True))


def test_delivery_rejects_omissions_bad_citations_and_fake_area(workspace):
    s,rid=batch_and_title(workspace)
    assert json.loads(s.invoke_tool('finish_analysis',{'claims':[{'text':'等待确认','state':'unknown','question_part_ids':['q2']}]}))['error']
    assert s.prepared_answer is None
    s.invoke_tool('read_source_context',{'region_id':rid,'task_ids':['q4']})
    f=final_for(s,rid);f['claims'][0]['evidence_ids']=['ev_foreign']
    rejected=json.loads(s.invoke_tool('finish_analysis',f))
    assert rejected['error'] and s.validation_issue and not s.policy.stop_reason
    f=final_for(s,rid);f['claims'].append({'text':'面积24平方米','state':'unknown','question_part_ids':['q2']})
    assert json.loads(s.invoke_tool('finish_analysis',f))['error']
    assert s.prepared_answer is None


def test_explicit_gap_reopens_whitelisted_capability_only(workspace):
    s,rid=batch_and_title(workspace);s.invoke_tool('read_source_context',{'region_id':rid,'task_ids':['q4']})
    assert s.focus.review_ready()
    args={'family':'semantics','gap':'尚缺表头与单元格的对应关系，需要核对合并表头。','task_ids':['q4']}
    assert not json.loads(s.invoke_tool('request_toolset',args)).get('error')
    assert not s.focus.review_ready() and 'read_table_semantics' in names(s)
    assert not {'exec','shell','python'}&names(s)
    assert json.loads(s.invoke_tool('request_toolset',args|{'family':'shell'}))['error']
    s.invoke_tool('request_toolset',args)
    capped=json.loads(s.invoke_tool('request_toolset',args))
    assert capped['enabled'] is False and capped['reason']=='optional_expansion_limit'
    assert 'read_table_semantics' in capped['available_tools']


def test_pruned_context_keeps_complete_scope_quotes_and_call_pair(workspace):
    s,rid=batch_and_title(workspace)
    s.invoke_tool('read_source_context',{'region_id':rid,'neighbors':1,'task_ids':['q4']})
    call={'type':'function_call','name':'read_source_context','call_id':'last','arguments':'{}'}
    output={'type':'function_call_output','call_id':'last','output':'last result'}
    question={'role':'user','content':'保留全部材料原文和适用范围。'}
    messages=[question,dict(call,call_id='old'),dict(output,call_id='old',output='old miss '*10000),call,output,{'role':'developer','content':'citation correction'}]
    pruned=s.focus.messages(messages)
    assert len(json.dumps(pruned))<len(json.dumps(messages))/2
    assert pruned[-3:]==messages[-3:]
    assert question in pruned
    context=next(json.loads(m['content']) for m in pruned if 'context_projection' in str(m.get('content','')))
    assert context['reads'] and all(e['quote'] in json.dumps(context,ensure_ascii=False) for e in s.evidence.values())
    assert any(i.get('relation_basis') for r in context['reads'] for i in r['items'])
    s.focus.has_history=True
    historical=s.focus.messages(messages)
    assert question in historical and historical[-3:]==messages[-3:]
    s.focus.has_history=False;s.visual_images={'image':{}}
    # Image payloads are attached from the session registry, outside text history.
    pending=s.focus.messages(messages)
    assert pending[-3:]==messages[-3:]
    assert len(json.dumps(pending))<len(json.dumps(messages))/2
    assert 'image' in s.visual_images


def test_unread_long_page_does_not_count_as_completed_read(workspace):
    s,rid=batch_and_title(workspace)
    s.focus.record('read_source_context',['q4'],{'items':[{'relationship':'target','next_start':600,'evidence':{'evidence_id':'e','region_id':rid,'quote':'only a part'}}]})
    assert not s.focus.review_ready()


def test_plan_and_expansion_are_not_source_evidence(workspace):
    s,q=session(workspace);s.invoke_tool('plan_task',plan(s).model_dump())
    s.invoke_tool('request_toolset',{'family':'sources','gap':'尚未检索当前图纸，需要查找原始条款。','task_ids':['q4']})
    out=json.loads(s.invoke_tool('finish_analysis',{'claims':[{'text':'没有依据','state':'unknown','question_part_ids':['q1','q2','q3','q4']}]}))
    assert out['error'] and s.prepared_answer is None


def test_complete_source_answer_ends_with_evidence_reason(workspace,monkeypatch):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    base,_=session(workspace)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=base.request.snapshot_id,question='读取标题原文。核对标题是否包含层号。',mode='agent',runtime='responses_subscription',model_profile='siwc_sol'))
    connected_lease(workspace,monkeypatch)
    rid=next(r.region_id for r in workspace.regions(s.request.snapshot_id) if r.text=='三层平面图')
    calls=[]
    def invoke(self,runner,payload):
        calls.append(payload)
        if len(calls)==1:return tool('plan_task',{'steps':[{'requirement_id':p['id'],'kind':'lookup','depends_on':[],'acceptance':'读取标题原文。'} for p in s.task.parts]},'p')
        if len(calls)==2:return tool('read_source_context',{'region_id':rid,'neighbors':0,'task_ids':['q1','q2']},'read')
        if len(calls)==3:
            eid=next(iter(s.evidence))
            return tool('finish_analysis',{'claims':[{'text':'标题原文为三层平面图，包含三层。','state':'observed','evidence_ids':[eid],'question_part_ids':['q1','q2']}]},'done')
        pytest.fail('must stop at accepted delivery')
    monkeypatch.setattr(ResponsesTransport,'request',invoke)
    result=s.run()
    assert result['task']['status']=='completed' and result['efficiency']['stop_reason']=='evidence_sufficient'
    assert len(calls)==3
    assert len({r['tools_sha256'] for r in result['model_requests']}) == 1
    assert 'finish_analysis' in names(s)
    assert not result['fallback_used']


def test_unread_batch_page_rejects_early_delivery_and_can_continue(workspace):
    s,q=session(workspace)
    regions=workspace.regions(s.request.snapshot_id)
    label=next(r for r in regions if r.text=='试点房间')
    regions.append(label.model_copy(update={'region_id':'second_label','entity_ids':['second_entity'],'bbox':[8000,0,8500,500]}))
    workspace.store.artifact(s.request.snapshot_id,'regions.json').write_text(json.dumps([r.model_dump() for r in regions]))
    s.task.plan(plan(s));s.invoke_tool('analyze_room_batch',q.model_copy(update={'limit':1}).model_dump()|{'task_ids':['q1','q2','q3']})
    rid=next(r.region_id for r in regions if r.text=='三层平面图')
    s.invoke_tool('read_source_context',{'region_id':rid,'task_ids':['q4']})
    assert s.focus.pending_pages() and not s.focus.review_ready()
    assert 'analyze_room_batch' in names(s)
    blocked=json.loads(s.invoke_tool('finish_analysis',final_for(s,rid)))
    assert 'ROOM_BATCH_COVERAGE_PENDING' in blocked['error']['message']
    next_args=s.focus.pending_pages()[0]['arguments']|{'task_ids':['q1']}
    s.invoke_tool('analyze_room_batch',next_args)
    assert not s.focus.pending_pages() and not s.focus.review_ready()
    complete_room_operations(s)
    assert s.focus.review_ready()
    assert len(s.task.data['subtasks'][1]['batch_ids'])==2
    result=json.loads(s.invoke_tool('finish_analysis',final_for(s,rid)))
    assert result.get('accepted') and s.prepared_answer['task']['status']=='waiting_input',result
    assert s.prepared_answer['task']['subtasks'][0]['status']=='completed'
    assert sum(len(b['items']) for b in s.prepared_answer['room_batches'])==2


def test_room_batch_default_is_not_search_page_size(workspace):
    s,q=session(workspace);s.task.plan(plan(s))
    schema=next(t['function']['parameters'] for t in s.offered_tools(planning_only=True) if t['function']['name']=='analyze_room_batch')
    assert schema['properties']['limit']['default']==12
    args=q.model_dump();args.pop('limit')
    s.invoke_tool('analyze_room_batch',args|{'task_ids':['q1','q2','q3']})
    assert s.room_batch['request']['limit']==12


def test_compaction_preserves_multiple_calls_in_one_response(workspace):
    s,rid=batch_and_title(workspace)
    s.invoke_tool('read_source_context',{'region_id':rid,'task_ids':['q4']})
    exchange=[{'type':'reasoning','encrypted_content':'opaque'},
      {'type':'function_call','call_id':'a','name':'search_sources','arguments':'{}'},
      {'type':'function_call','call_id':'b','name':'read_source_context','arguments':'{}'},
      {'type':'function_call_output','call_id':'a','output':'candidates'},
      {'type':'function_call_output','call_id':'b','output':'source'}]
    question={'role':'user','content':'保留地下室除外的条件。'}
    messages=[question,dict(exchange[1],call_id='old'),dict(exchange[-1],call_id='old',output='old '*10000)]+exchange
    compacted=s.focus.messages(messages)
    assert len(json.dumps(compacted))<len(json.dumps(messages))/2
    assert question in compacted and compacted[-len(exchange):]==exchange


def test_legacy_scope_blocker_is_rechecked_locally_before_delivery(workspace):
    from cad_agent.geometry.room_batch import RoomBatchService
    s,q=session(workspace);s.task.plan(plan(s))
    s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':['q1','q2','q3']})
    old=s.room_batch
    # Reproduce an older saved scope failure while immutable source has a valid border.
    old.update(scope=None,status='needs_scope_confirmation',items=[],total=0,batch_id='rooms_legacy')
    old.pop('scope_diagnostics')
    s.room_batches={old['batch_id']:old};s.task.data['batches']=dict(s.room_batches)
    for step in s.task.data['subtasks'][:3]:step.update(batch_ids=[old['batch_id']],status='waiting_input')
    content={'claims':[{'text':'范围未确认','state':'unknown','question_part_ids':['q1','q2','q3','q4']}]}
    rejected=json.loads(s.invoke_tool('finish_analysis',content))
    assert 'ROOM_SCOPE_RECHECKED' in rejected['error']['message']
    # Incremental errors keep full recovery details in the addressable record.
    stored=json.loads(s.w.store.get('evidence_archive',rejected['error_record']['record_id'])['text'])
    assert stored['feedback']['recovered_batches'][0]['status']=='needs_unit_confirmation'
    assert stored['feedback']['recovered_batches'][0]['total']==1
    assert s.prepared_answer is None and not s.policy.stop_reason
    assert s.task.data['subtasks'][0]['status']=='completed'
    assert s.task.data['subtasks'][1]['status']=='pending'
    assert 'rooms_legacy' not in s.room_batches
    assert not s.recover_scope_before_delivery()  # Bounded, no repeated recovery.
