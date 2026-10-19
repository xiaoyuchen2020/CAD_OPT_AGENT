import json
from copy import deepcopy
import pytest
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession, Claim
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts, user_values, UserInputRef
from cad_agent.analysis.delivery_context import bounded_packet
from cad_agent.analysis.efficiency import dumps
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime


def make(workspace,demo_bytes,question='按用户给定70mm计算厚度差。'):
 d=workspace.ingest('one.dxf',demo_bytes)
 return CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question=question,mode='agent'))


def calc(s,quantities,op='subtract'):
 return calculate_facts(s,CalculateFacts(operation=op,quantities=quantities))


def test_typed_user_baseline_can_be_subtracted_without_appearing_in_drawing(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.evidence={'e':{'quote':'汽车库96mm厚混凝土'}}
 r=calc(s,[dict(origin='source',value='96',unit='mm',evidence_id='e'),dict(origin='user_input',value='70',unit='mm',question_quote='给定70mm')])
 assert r['value']=='26.000' and r['unit']=='mm' and r['conditional']
 r=calculate_facts(s,CalculateFacts(operation='subtract',decimal_places=3,quantities=[dict(origin='source',value='96',unit='mm',evidence_id='e'),dict(origin='user_input',value='70',unit='mm',question_quote='给定70mm')]))
 assert r['value']=='26.000'
 assert r['inputs'][0]['basis']=='source_text' and r['inputs'][1]['basis']=='user_assumption'


@pytest.mark.parametrize('fault',['unread','wrong_quote','hidden_sign','missing_unit','foreign_user','mixed_units','zero'])
def test_factual_calculator_rejects_unbound_or_incompatible_inputs(workspace,demo_bytes,fault):
 s=make(workspace,demo_bytes);s.evidence={'e':{'quote':'厚度96mm，面积96m²'}}
 a=dict(origin='source',value='96',unit='mm',evidence_id='e');b=dict(origin='user_input',value='70',unit='mm',question_quote='给定70mm');op='subtract'
 if fault=='unread':a['evidence_id']='absent'
 if fault=='wrong_quote':a['value']='97'
 if fault=='hidden_sign':s.evidence['e']['quote']='-96mm'
 if fault=='missing_unit':s.evidence['e']['quote']='96厚'
 if fault=='foreign_user':b['question_quote']='70mm是图纸实测值'
 if fault=='mixed_units':a['unit']='m2'
 if fault=='zero':s.request.question='给定0mm';b.update(value='0',question_quote='给定0mm');op='divide'
 with pytest.raises(ValueError):calc(s,[a,b],op)


def test_area_per_count_is_weighted_input_not_average_of_ratios(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.evidence={'a':{'quote':'面积1000m²'},'n':{'quote':'40个车位'}}
 r=calc(s,[dict(origin='source',value='1000',unit='m2',evidence_id='a'),dict(origin='source',value='40',unit='count',evidence_id='n')],'divide')
 assert r['value']=='25.000' and r['unit']=='m2/count'


def test_user_numbers_cannot_be_claimed_as_observed_or_injected_from_other_question():
 refs=[UserInputRef(quote='参考B05',value='05')]
 assert user_values(refs,'内墙参考B05','derived','用户参考B05')=={'05'}
 for state,text,question in [('observed','用户参考B05','参考B05'),('derived','原图B05','参考B05'),('derived','用户参考B05','参考B06')]:
  with pytest.raises(ValueError):user_values(refs,question,state,text)


def test_large_final_packet_keeps_whole_quotes_balances_requirements_and_reports_omissions():
 es=[dict(evidence_id=str(i),quote=str(i)+'汉'*1200,region_id=str(i),text_span=[0,1201]) for i in range(30)]
 p=dict(question='逐项回答',evidence=es,observations=[dict(observation_id='huge',tool='query_entities',data={'items':['x'*1000]*50})],source_contexts=[],instruction='只依据证据')
 out=bounded_packet(p,9000,[{'evidence_ids':[str(i)]} for i in range(30)])
 assert len(dumps(out))<=9000 and out['context_omissions']['evidence_count']>0
 assert len(out['evidence'])>=3 and out['evidence'][0]==es[0]
 assert all(e in es for e in out['evidence'])
 assert out['context_omissions']['observation_count']==1
 assert p['evidence']==es


def test_read_assembly_keeps_evidence_and_pagination(workspace,demo_bytes):
 s=make(workspace,demo_bytes);r=s.index.regions[0]
 out=s.execute('read_assembly',dict(region_id=r.region_id,limit=1))
 assert out['items'] and out['completeness']=='candidate_sequence'
 e=out['items'][0]['evidence'];source=s.index.by_id[e['region_id']]
 assert source.text[slice(*e['text_span'])]==e['quote']
 assert e['evidence_id'] in s.evidence


def test_cross_drawing_requires_explicit_selection_and_keeps_snapshot(workspace,demo_bytes):
 s=make(workspace,demo_bytes);d2=workspace.ingest('two.dxf',demo_bytes.replace(b'1.4.3',b'1.4.4'))
 s2=CadAgentSession(workspace,ChatRequest(snapshot_id=s.request.snapshot_id,question='读取关联图纸',related_drawings=[dict(snapshot_id=d2['id'],project='test',floor='一层')]))
 cat=s2.execute('read_project_sources',dict(action='catalog'))
 assert len(cat['items'])==2 and cat['items'][1]['metadata']['floor']=='一层'
 rid=workspace.regions(d2['id'])[0].region_id
 with pytest.raises(ValueError):s.execute('read_project_sources',dict(action='read',snapshot_id=d2['id'],region_id=rid))
 result=s2.execute('read_project_sources',dict(action='read',snapshot_id=d2['id'],region_id=rid))
 assert result['snapshot_id']==d2['id'] and all(e['snapshot_id']==d2['id'] for e in s2.evidence.values())


def test_source_read_reserve_only_credits_owning_requirement(workspace,demo_bytes):
 s=make(workspace,demo_bytes,'请①查墙材料；②列出门窗。');s.task.auto_plan()
 rid=s.index.regions[0].region_id
 s.coverage.record('search_sources',{'items':[{'region_id':rid}]},task_ids=['q1'])
 s.policy.stop_reason='subscription_exploration_budget'
 s.final_context(CodexSubscriptionRuntime(),'subscription_exploration_budget')
 assert s.trace[-1]['task_ids']==['q1']
 assert s.task.data['subtasks'][0]['evidence_ids'] and not s.task.data['subtasks'][1]['evidence_ids']


def test_failure_checkpoint_and_resume_keep_sources_and_reset_exhausted_budget(workspace,demo_bytes):
 s=make(workspace,demo_bytes);rid=s.index.regions[0].region_id
 s.invoke_tool('read_source_context',dict(region_id=rid))
 step=s.task.data['subtasks'][0];step.update(status='blocked_capability',calls=12,stalled=2)
 s.task.publication_failed({'code':'TEST_TIMEOUT','message':'已到时间上限'})
 checkpoint=s.task.public()['checkpoint']
 assert checkpoint['not_a_final_answer'] and checkpoint['sources']
 old=deepcopy(s.evidence)
 resumed=CadAgentSession(workspace,s.request.model_copy(update={'resume_task_id':s.task.data['task_id']}))
 assert resumed.evidence==old
 assert resumed.task.data['subtasks'][0]['calls']==0 and resumed.task.runnable()


def test_related_manifest_cannot_change_on_resume(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.task.save()
 other=workspace.ingest('other.dxf',demo_bytes.replace(b'1.4.3',b'1.4.5'))
 with pytest.raises(ValueError,match='关联图纸'):
  CadAgentSession(workspace,ChatRequest(snapshot_id=s.request.snapshot_id,question=s.request.question,resume_task_id=s.task.data['task_id'],related_drawings=[dict(snapshot_id=other['id'])]))


def test_chained_totals_division_and_condition_provenance(workspace,demo_bytes):
 s=make(workspace,demo_bytes,'用户给定40个车位，60个车位。')
 s.evidence={'a':{'quote':'面积1000m²'},'b':{'quote':'面积2000m²'}}
 area=calc(s,[dict(origin='source',value='1000',unit='m2',evidence_id='a'),dict(origin='source',value='2000',unit='m2',evidence_id='b')],'sum')
 count=calc(s,[dict(origin='user_input',value='40',unit='count',question_quote='给定40个车位'),dict(origin='user_input',value='60',unit='count',question_quote='60个车位')],'sum')
 s.register_observation('calculate_facts',area);s.register_observation('calculate_facts',count)
 r=calc(s,[dict(origin='calculation',value=area['value'],unit='m2',observation_id=area['observation_id']),dict(origin='calculation',value=count['value'],unit='count',observation_id=count['observation_id'])],'divide')
 assert r['value']=='30.000' and r['unit']=='m2/count' and r['conditional']
 with pytest.raises(ValueError):calc(s,[dict(origin='calculation',value='1',unit='m2',observation_id=area['observation_id'])],'ceil')


def test_rank_mixed_length_units_uses_physical_values(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.evidence={'a':{'quote':'长度900mm'},'b':{'quote':'长度2m'}}
 r=calc(s,[dict(origin='source',value='900',unit='mm',evidence_id='a'),dict(origin='source',value='2',unit='m',evidence_id='b')],'rank')
 assert [v['input_index'] for v in r['ordered']]==[1,0]


def test_top_level_user_registry_is_claim_scoped_and_cannot_be_observed(workspace,demo_bytes):
 s=make(workspace,demo_bytes,'用户给定70mm');s.evidence={'e':{'evidence_id':'e','quote':'原图96mm'}}
 answer={'user_inputs':[{'quote':'用户给定70mm','value':'70'}],'claims':[dict(text='原图96mm，用户给定参考70mm，适用关系待核对。',state='derived',evidence_ids=['e'])]}
 r=s.validate_answer(json.dumps(answer));assert r['claims'][0]['user_inputs'][0]['value']=='70'
 answer['claims'][0]['state']='observed'
 with pytest.raises(ValueError):s.validate_answer(json.dumps(answer))
 answer['claims'][0]['state']='derived';answer['user_inputs'][0]['value']='71'
 with pytest.raises(ValueError):s.validate_answer(json.dumps(answer))


def test_complex_text_task_does_not_stop_merely_on_first_source(workspace,demo_bytes):
 s=make(workspace,demo_bytes,'请①读取墙体；②读取屋面。');s.task.auto_plan()
 s.invoke_tool('read_source_context',{'region_id':s.index.regions[0].region_id,'task_ids':['q1']})
 s.usage.append({'total_tokens':31000})
 assert CodexSubscriptionRuntime().stop_reason(s) is None


def test_user_reference_phrase_retains_exact_numbers_without_accepting_substrings():
 refs=[UserInputRef(quote='参考100厚C15垫层',value='100厚C15垫层')]
 assert user_values(refs,'参考100厚C15垫层','derived','用户参考')=={'100','15'}
 with pytest.raises(ValueError):user_values([UserInputRef(quote='参考100厚',value='10')],'参考100厚','derived','用户参考')


def test_partial_delivery_keeps_valid_fact_and_drops_unbound_number(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.invoke_tool('read_source_context',{'region_id':s.index.regions[0].region_id})
 eid=next(iter(s.evidence));text=s.evidence[eid]['quote']
 answer={'claims':[dict(text=text,state='observed',evidence_ids=[eid],question_part_ids=['q1']),dict(text='厚度987654mm',state='observed',evidence_ids=[eid],question_part_ids=['q1'])]}
 r=s.recover_valid_claims(json.dumps(answer))
 assert r['delivery_kind']=='validated_partial_recovery' and r['task']['status']=='partial'
 assert r['recovery']['kept_claims']==1 and r['recovery']['rejected_claims']==1
 assert '987654' not in r['answer'] and r['citations']
 answer['claims']=answer['claims'][1:]
 assert s.recover_valid_claims(json.dumps(answer)) is None


def test_bad_global_reference_does_not_poison_valid_source_clause(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.invoke_tool('read_source_context',{'region_id':s.index.regions[0].region_id})
 eid=next(iter(s.evidence));text=s.evidence[eid]['quote']
 r=s.recover_valid_claims(json.dumps({'user_inputs':[dict(quote='并不存在的70mm输入',value='70')],
   'claims':[dict(text=text,state='observed',evidence_ids=[eid],question_part_ids=['q1']),dict(text='用户给定987654mm',state='derived',evidence_ids=[eid],question_part_ids=['q1'])]}))
 assert r and r['recovery']['rejected_user_inputs']==1 and r['task']['status']=='partial'
 assert '987654' not in r['answer'] and r['citations']


def test_partial_recovery_does_not_leave_a_reference_to_a_dropped_calculation(workspace,demo_bytes):
 s=make(workspace,demo_bytes,'用户给定参考1.5m')
 s.invoke_tool('read_source_context',{'region_id':s.index.regions[0].region_id})
 eid=next(iter(s.evidence));text=s.evidence[eid]['quote']
 answer={'claims':[
  dict(text='局部标高差为987654m',state='derived',evidence_ids=[eid],question_part_ids=['q1']),
  dict(text='上述局部标高差与用户给定参考1.5m相同，但不能确认覆土厚度。',state='derived',evidence_ids=[eid],question_part_ids=['q1'],user_inputs=[dict(quote='用户给定参考1.5m',value='1.5m')]),
  dict(text=text,state='observed',evidence_ids=[eid],question_part_ids=['q1'])]}
 r=s.recover_valid_claims(json.dumps(answer))
 assert r and r['recovery']['kept_claims']==1 and r['recovery']['rejected_claims']==2
 assert '上述' not in r['answer'] and '相同' not in r['answer']


@pytest.mark.parametrize('text',[
 '按用户给定条件，可供设计复核的拟取消对象是吊杆、龙骨及板材。',
 '建议取消保护层，最终由人工确认。',
 '可以将地下室边界拉直。',
 '拟替换现有玻璃，尚待甲方确认。',
])
def test_fact_scope_rejects_conditional_design_proposals(workspace,demo_bytes,text):
 s=make(workspace,demo_bytes);s.evidence={'e':{'evidence_id':'e','quote':'原图要求保留保护层。'}}
 with pytest.raises(ValueError,match='超出图纸事实分析定位'):
  s.validate_answer(json.dumps({'claims':[dict(text=text,state='derived',evidence_ids=['e'])]}))


def test_fact_scope_preserves_literal_source_and_explicit_refusal(workspace,demo_bytes):
 s=make(workspace,demo_bytes);s.evidence={'e':{'evidence_id':'e','quote':'设计变更说明：建议取消该装饰层。'}}
 for text in ['设计变更说明：建议取消该装饰层。','当前不建议取消该层；仅记录原设计说明，尚缺适用部位依据。']:
  result=s.validate_answer(json.dumps({'claims':[dict(text=text,state='derived',evidence_ids=['e'])]}))
  assert result['claims'][0]['text']==text
