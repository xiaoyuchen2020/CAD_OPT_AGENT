import json
import pytest
from pydantic import ValidationError
from cad_agent.analysis.final_contract import contract_message
from cad_agent.analysis.tool_registry import FinalAnswer

def test_contract_is_actual_schema():
    msg=contract_message()
    assert json.loads(msg['content'].split('最终答案JSON Schema：')[1])==FinalAnswer.model_json_schema()

@pytest.mark.parametrize('roles',[{'source_text':['e1']},{'location':['e1']},['e1']])
def test_invalid_model_roles_remain_rejected(roles):
    with pytest.raises(ValidationError):
        FinalAnswer.model_validate({'claims':[{'text':'年限为50年','state':'observed','evidence_ids':['e1'],'evidence_roles':roles}]})

def test_missing_info_objects_still_rejected():
    with pytest.raises(ValidationError):
        FinalAnswer.model_validate({'claims':[{'text':'未明确','state':'unknown'}],'missing_information':[{'reason':'unknown'}]})


def test_internal_reference_reports_reference_error_not_fake_quantity():
    from cad_agent.analysis.reference_prose import check_reference_prose
    text, invalid = check_reference_prose('区域r1，厚度99mm', {'r1':'region_real'})
    assert invalid == ['r1']  # still rejected, never made acceptable
    assert '99mm' in text and 'r1' not in text
    assert check_reference_prose('区域r999', {'r1':'region_real'}) == ('区域r999', [])
    assert check_reference_prose('型号r1', {'r1':'region_real'}, True) == ('型号r1', [])


def test_native_reapplies_contract_after_each_context_projection(workspace,demo_bytes,monkeypatch):
    from cad_agent.analysis.agent import AgentRunner
    from cad_agent.contracts import ChatRequest
    from test_agent import call
    d=workspace.ingest('contract.dxf',demo_bytes)
    runner=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='查找门窗说明',mode='agent'))
    original=runner.model_messages
    def project(messages,final_only=False):
        projected=original(messages,final_only)
        return [m for i,m in enumerate(projected) if i==0 or m.get('role')!='system']
    monkeypatch.setattr(runner,'model_messages',project)
    count=[]
    def turn(messages,tools,timeout):
        assert sum('最终答案JSON Schema：' in str(m.get('content','')) for m in messages)==1
        count.append(1)
        if len(count)==1:return call('search_sources',{'query':'不存在的门窗说明'},1),{}
        return {'content':json.dumps({'claims':[{'text':'当前已检查范围没有读到支持该问题的原文。','state':'unknown','question_part_ids':['q1']}],'requirement_outcomes':[{'requirement_id':'q1','status':'unresolved','gap_kind':'unread_source','reason':'未读到支持原文'}]})},{}
    monkeypatch.setattr(workspace.gateway,'chat_turn',turn)
    result=runner.run()
    assert len(count)==2 and result['claims'][0]['state']=='unknown'
