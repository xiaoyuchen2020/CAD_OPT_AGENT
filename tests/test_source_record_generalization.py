"""General grammar/topology/validation regressions; no drawing-name exceptions."""
from copy import deepcopy
from types import SimpleNamespace as NS
import json
import pytest

from cad_agent.analysis.source_record_query import route, matches
from cad_agent.analysis.source_record_answer import compile_answer, expected, literal_claim, permitted
from cad_agent.analysis.source_records import fixed_page_progress
from cad_agent.cad.assembly_headings import HEADING, source_titles
from cad_agent.cad.source_vocabulary import expand, normalized_code
from cad_agent.analysis.tool_registry import Claim, ReadAssembly
from test_source_record_workflow import routing, example
from test_unified_numbers import source


@pytest.mark.parametrize('term',['楼面','地坪','天棚','吊顶','踢脚线','地下室底板','女儿墙','门窗','幕墙','墙体','防水','找平层','保护层'])
def test_new_families_and_literal_facets_have_routes(term):
    query=route(routing('帮忙找'+term+'作法'))
    assert query['route']=='source_records',query


def test_expansion_is_directed_and_negation_is_preserved():
    assert {'楼面','地面'} <= expand('楼地面')
    assert '楼地面' not in expand('楼面') and '地面' not in expand('楼面')
    assert '吊顶' in expand('天棚') and '顶棚' not in expand('吊顶')
    assert '非上人屋面' not in expand('上人屋面')
    query=route(routing('查一下不上人屋面做法'))
    assert '上人屋面' not in query['parts'][0]['families']


@pytest.mark.parametrize('word',['规范','适用','计算','内墙'])
def test_quoted_literals_are_not_judgment_requests(word):
    assert route(routing(f'列出含“{word}”的外墙做法'))['route']=='source_records'
    assert route(routing('判断外墙适用本工程是否合理'))['route']=='agent'


@pytest.mark.parametrize('title,code', [('顶棚-P1','顶棚P1'),('外墙－１','外墙1'),('外墙面-2','外墙2'),('抛光砖楼面3（详表）','楼面3')])
def test_offset_preserving_title_codes(title,code):
    m=HEADING.search(title)
    assert m and normalized_code(m[1]+m['code'])==code
    assert title[m.start():m.end()]==m[0]


@pytest.mark.parametrize('title',['材料选用外墙1（详图）','型号为楼地面2（样品）','基层同楼板3（材料）','用于外墙1（做法）'])
def test_prose_is_never_an_identity(title):
    assert not HEADING.search(title)


def test_body_and_usage_mentions_cannot_override_record_identity():
    record=dict(title_id='t',display_title='踢脚1',identity_text='踢脚1',body_ids=['b'],source_scope={})
    evidence={'t':dict(quote='踢脚1'),'b':dict(quote='基层随内墙做法；用于楼板边缘')}
    for family in ('内墙','楼板'):
        assert not matches(record,evidence,dict(families=[family],title_codes=[],filters=[]))
    assert matches(record,evidence,dict(families=['踢脚'],title_codes=[],filters=[]))


def test_local_codes_are_exact_predicates_without_global_family_guess():
    p=route(routing('找屋4做法'))['parts'][0]
    assert p['families']==['*'] and p['title_codes']==['屋4']
    r=dict(title_id='t',identity_text='屋5',body_ids=['t'],source_scope={})
    assert not matches(r,{'t':dict(quote='屋5')},p)
    r['identity_text']='屋4'
    assert matches(r,{'t':dict(quote='屋5')},p)


def test_multi_heading_read_has_distinct_deterministic_ids():
    from cad_agent.analysis.assembly import read
    from cad_agent.evidence import bind_evidence
    text='外墙1——甲\n1. 5厚砂浆\n外墙2——乙\n1. 20厚抹灰\n外墙3——丙'
    r=source(text)
    idx=NS(snapshot='s',by_id={r.region_id:r},regions=[r],scene={'texts':[],'lines':[]},
           tables=[],drawing={'unit':'unknown'},scope=lambda *args:[r],
           evidence=lambda r,a=0,b=None:bind_evidence('s',r,a,len(r.text) if b is None else b,'unknown').model_dump())
    a=read(idx,ReadAssembly(region_id=r.region_id,heading_start=0))
    b=read(idx,ReadAssembly(region_id=r.region_id,heading_start=text.index('外墙2')))
    assert a['record_id']!=b['record_id']
    assert a['record_id']==read(idx,ReadAssembly(region_id=r.region_id,heading_start=0))['record_id']
    assert '外墙2' not in ''.join(i['evidence']['quote'] for i in a['items'])


def test_compiled_literal_spans_cover_numbers_and_source_references(example):
    s,p=example
    s.evidence['b1']['quote']='1.60mm厚C20混凝土\n2.300mm厚碎石\n上述留缝均用防水油膏嵌填密实'
    draft,groups,_=compile_answer(s,p,{r['key']:dict(action=expected(r)) for r in p['records']})
    assert permitted(s,json.dumps(draft))
    claim=Claim.model_validate(draft['claims'][0])
    assert literal_claim(s,claim,0)
    assert groups[0]['source_segments']
    assert not literal_claim(s,claim.model_copy(update={'text':claim.text.replace('60mm','61mm')}),0)
    s.evidence['b1']['quote']=s.evidence['b1']['quote'].replace('60mm','61mm')
    assert not literal_claim(s,claim,0)


def test_fresh_source_pages_do_not_credit_repeats_or_wrong_targets():
    active=dict(snapshot_id='s',region_id='r',heading_start=0,offset=12)
    s=NS(_source_record_active_page=active)
    page=dict(snapshot_id='s',target_region_id='r',heading_start=0,record_id='a',offset=12,items=[{'order':12}])
    assert fixed_page_progress(s,'read_assembly',page)
    assert not fixed_page_progress(s,'read_assembly',page)
    assert not fixed_page_progress(s,'read_assembly',page|{'offset':24})
    assert not fixed_page_progress(s,'read_source_context',page)


def test_explicit_chapter_is_a_section_not_a_fake_layer_assembly():
    from cad_agent.analysis.assembly import _bounded_section,_section_binding
    from cad_agent.evidence import bind_evidence
    r=source('五、门窗\n门窗材料应符合图示要求。\n六、幕墙\n另见专项设计。')
    idx=NS(evidence=lambda r,a,b:bind_evidence('s',r,a,b,'unknown').model_dump())
    sources,stop=_bounded_section([r],r,0)
    b=_section_binding(idx,r,sources,stop,0)
    assert b['record_type']=='source_section' and b['state']=='source_bound'
    assert '另见' not in ''.join(r.text[a:b] for r,a,b,_,_ in sources)
    assert not source_titles('材料采用门窗详图')


@pytest.mark.parametrize('scale,shift,reverse',[(1,0,False),(3,170,True)])
def test_merged_header_and_identity_row_survive_auxiliary_grid(workspace,tmp_path,scale,shift,reverse):
    from conftest import dxf_bytes
    from cad_agent.analysis.general import DrawingIndex
    from cad_agent.analysis.assembly import read
    def build(doc):
        m=doc.modelspace();doc.layers.new('structure');doc.layers.new('guides')
        def point(x,y):return (shift+scale*x,shift+scale*y)
        lines=[((0,y),(100,y),'structure') for y in (0,100,120)]
        lines += [((x,0),(x,120),'structure') for x in (0,20,100)]
        lines += [((70,0),(70,100),'structure')]
        lines += [((0,y),(100,y),'guides') for y in range(10,100,10)]
        for a,b,layer in reversed(lines) if reverse else lines:
            m.add_line(point(*a),point(*b),dxfattribs={'layer':layer})
        texts=[('名称',4,110),('做法及用料',35,110),('外墙7',4,55),
            ('1. 10厚面层',25,85),('2. 20厚砂浆',25,65),('3. 结构基层',25,45),
            ('第2条改为',73,55),('2. 30厚砂浆',73,35)]
        for text,x,y in texts:m.add_text(text,dxfattribs={'height':3*scale,'insert':point(x,y)})
    drawing=workspace.ingest('varied.dxf',dxf_bytes(tmp_path,build))
    idx=DrawingIndex(workspace,drawing['id']);target=next(r for r in idx.regions if '外墙7' in r.text)
    result=read(idx,ReadAssembly(region_id=target.region_id,limit=12))
    binding=result['source_binding']
    assert binding['state']=='source_bound',result['diagnostics']
    assert result['row_structure']['bbox'][1]==pytest.approx(shift)
    quotes='\n'.join(i['evidence']['quote'] for i in result['items'])
    assert all(text in quotes for text in ['10厚面层','20厚砂浆','结构基层','第2条改为','30厚砂浆'])
    assert any(f.get('branch_state')=='explicit_nonadditive_alternative' for f in binding['fields'])


def test_page_reuse_distinguishes_anchors_in_one_parser_region():
    from cad_agent.analysis.read_context import matching_read_page
    page=dict(snapshot_id='s',target_region_id='r',heading_start=None,anchor_entity_id='cell-a',view='record',offset=0,has_more=False)
    observations={'o':dict(tool='read_assembly',data=page)}
    assert matching_read_page(observations,dict(snapshot_id='s',region_id='r',anchor_entity_id='cell-a'))
    assert not matching_read_page(observations,dict(snapshot_id='s',region_id='r',anchor_entity_id='cell-b'))


def test_compiled_source_delivery_retains_literal_numbers_but_rejects_tampering(workspace,demo_bytes):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from cad_agent.evidence import bind_evidence
    drawing=workspace.ingest('literal.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='台阶做法'))
    quotes=['台阶1','1.饰面\n2.基层\n3.60mm厚C20混凝土','上述留缝均用防水油膏嵌填密实']
    ids=[]
    for i,quote in enumerate(quotes):
        r=source(quote).model_copy(update={'region_id':'literal'+str(i)})
        e=bind_evidence(drawing['id'],r,0,len(quote),'unknown').model_dump()
        s.evidence[e['evidence_id']]=e;ids.append(e['evidence_id'])
    r=dict(key='a1',record_id='a',snapshot_id=drawing['id'],title_id=ids[0],body_ids=ids,source_ids=ids,
        source_scope={},ready=True,selected_parts=['q1'],page_state='complete',binding_state='source_bound',reference_state='unresolved',
        engineering_applicability_state='unverified',reference_targets=[],reference_only=False,diagnostics=[],next_actions=[])
    p=dict(records=[r],scope='已索引来源',query=dict(parts=[dict(id='q1')]),unresolved_hits=[])
    draft,_,_=compile_answer(s,p,{'a1':dict(action='deliver')})
    s.validate_answer(json.dumps(draft,ensure_ascii=False))
    changed=deepcopy(draft);changed['claims'][0]['text']=changed['claims'][0]['text'].replace('60mm','61mm')
    with pytest.raises(ValueError):s.validate_answer(json.dumps(changed,ensure_ascii=False))


def test_compound_board_scope_is_not_split_into_independent_or_queries():
    from cad_agent.cad.source_vocabulary import canonical
    p=route(routing('地下室保温楼板做法'))['parts'][0]
    assert p['families'] and all('地下室' in f and '保温' in f and f.endswith('楼板') for f in p['families'])
    assert '地下室外墙' not in p['families'] and '楼板' not in p['families']
    assert canonical('保温地下室楼板')==canonical('地下室保温楼板')
    assert HEADING.match('地下室保温楼板1——示例')[1]=='地下室保温楼板'


def test_row_discovery_keeps_the_native_anchor_that_established_ownership(monkeypatch,example):
    from cad_agent.analysis import source_records as mod, assembly
    s,p=example;r=deepcopy(p['records'][0]);region=source('内墙').model_copy(update={'region_id':'body'})
    s.w=NS(settings=NS(source_record_max_records=64));s._remaining=lambda:None
    idx=NS(snapshot='s',signature=('frozen',),by_id={'body':region})
    target=dict(snapshot_id='s',region_id='body',heading_start=None,anchor_entity_id='t',task_ids=['q1'],entry='text_match')
    monkeypatch.setattr(mod,'DrawingIndex',lambda *a:idx)
    monkeypatch.setattr(mod,'discover',lambda *a:([],[target]))
    band=dict(bbox=[0,0,20,10])
    monkeypatch.setattr(assembly,'ruled_band',lambda *a:(band,[]))
    monkeypatch.setattr(assembly,'_source_spans',lambda *a:[])
    monkeypatch.setattr(assembly,'_row_binding',lambda *a:dict(state='source_bound',identity_columns=[0],headers=dict(state='source_bound'),fields=[dict(column_index=0,evidence=[dict(region_id='another-merged-region',entity_ids=['another-cell'])])]))
    seen=[]
    def read(*args):
        seen.append(args[2]);return r|dict(items=[],row_structure=band,references=[],target_region_id='body')
    monkeypatch.setattr(mod,'read_record',read)
    mod.build(s,dict(snapshot_ids=['s'],parts=[dict(id='q1',families=['外墙'],title_codes=[],filters=[])]))
    assert seen[0]['region_id']=='body' and seen[0]['anchor_entity_id']=='t'


def test_chapter_qualifies_parent_labels_without_global_reverse_expansion():
    from cad_agent.analysis.source_sections import context, narrowed_families
    from cad_agent.analysis.source_records import discover
    from cad_agent.evidence import bind_evidence
    texts=['五、楼面','楼地面1——木地板','地面1——土层','六、地面','楼地面2——混凝土']
    rows=[source(t).model_copy(update={'region_id':str(i),'bbox':[0,100-i*10,20,105-i*10],'column_key':'column'}) for i,t in enumerate(texts)]
    idx=NS(snapshot='s',regions=rows,by_id={r.region_id:r for r in rows},evidence=lambda r,a,b:bind_evidence('s',r,a,b,'unknown').model_dump())
    c=context(idx,rows[1]);assert narrowed_families(c,rows[1].text)==['楼面']
    assert narrowed_families(c,rows[2].text)==[]
    assert narrowed_families(context(idx,rows[4]),rows[4].text)==['地面']
    plan=dict(parts=[dict(id='q1',families=['楼面'],filters=[],title_codes=[])])
    targets,_=discover(idx,plan)
    assert {t['region_id'] for t in targets}=={'0','1'}


def test_host_receipt_requires_active_exact_target_and_retains_archive():
    from cad_agent.analysis.source_records import fixed_read_receipt
    active=dict(snapshot_id='s',region_id='r',heading_start=0,anchor_entity_id='cell',offset=0,view='record')
    page=dict(snapshot_id='s',target_region_id='r',heading_start=0,anchor_entity_id='cell',offset=0,view='record',
              record_id='record',items=[dict(order=0,text='原文')],evidence_record={'record_id':'archive'})
    assert fixed_read_receipt(NS(),'read_assembly',page) is None
    session=NS(_source_record_active_page=active)
    assert fixed_read_receipt(session,'read_assembly',page|dict(anchor_entity_id='other')) is None
    assert fixed_read_receipt(session,'read_source_context',page) is None
    receipt=fixed_read_receipt(session,'read_assembly',page)
    assert receipt['fixed_source_receipt'] and receipt['evidence_record']['record_id']=='archive'
    assert 'items' not in receipt and page['items'][0]['text']=='原文'


def test_large_source_page_host_receipt_keeps_canonical_data_and_normal_wire_limit(workspace,demo_bytes):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.source_records import page_read
    drawing=workspace.ingest('large-source-page.dxf',demo_bytes)
    def session():
        s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='读取外墙做法'))
        s._tool_response_limit=2000
        return s
    s=session();region=s.index.regions[0]
    target=dict(snapshot_id=drawing['id'],region_id=region.region_id,heading_start=None,anchor_entity_id=None,task_ids=['q1'])
    page=page_read(s,target,0)
    assert page['items'] and s.evidence and s.observations and s.calls==1
    assert s.trace[-1]['status']=='completed' and s.trace[-1]['efficiency']['response_chars']<=2000
    other=session()
    response=json.loads(other.invoke_tool('read_assembly',dict(snapshot_id=drawing['id'],region_id=region.region_id,view='record',offset=0,limit=12,result_view='full')))
    assert response['error']['code']=='RESULT_TOO_LARGE'
    assert other.calls==1 and not other.observations


@pytest.mark.parametrize('ready',[False,True])
def test_exact_candidate_group_transcript_does_not_assert_preheading_ownership(workspace,demo_bytes,ready):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from cad_agent.evidence import bind_evidence
    drawing=workspace.ingest('candidate-transcript.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='踢脚做法'))
    quotes=['踢脚2','3-4层除楼梯间外房间\n踢脚3','1、20厚砂浆']
    ids=[]
    for i,quote in enumerate(quotes):
        region=source(quote).model_copy(update={'region_id':'group'+str(i)})
        e=bind_evidence(drawing['id'],region,0,len(quote),'unknown').model_dump();s.evidence[e['evidence_id']]=e;ids.append(e['evidence_id'])
    record=dict(key='a1',record_id='g',snapshot_id=drawing['id'],title_id=ids[0],body_ids=ids,source_ids=ids,
        source_scope={},ready=ready,selected_parts=['q1'],page_state='complete',binding_state='source_bound' if ready else 'candidate',
        reference_state='none',engineering_applicability_state='unverified',reference_targets=[],reference_only=False,
        diagnostics=['SUBRECORD_BOUNDARIES_REQUIRE_REVIEW'],next_actions=[],record_type='table_group')
    packet=dict(records=[record],scope='待拆分表格原文',query=dict(parts=[dict(id='q1')]),unresolved_hits=[])
    draft,groups,_=compile_answer(s,packet,{'a1':dict(action='deliver' if ready else 'uncertain')})
    if ready:
        with pytest.raises(ValueError,match='标题边界冲突'):s.validate_answer(json.dumps(draft,ensure_ascii=False))
    else:
        result=s.validate_answer(json.dumps(draft,ensure_ascii=False))
        assert groups[0]['record_type']=='table_group' and draft['claims'][0]['state']=='unknown'
        changed=deepcopy(draft);changed['claims'][0]['text']='踢脚3适用于3-4层除楼梯间外房间'
        with pytest.raises(ValueError):s.validate_answer(json.dumps(changed,ensure_ascii=False))


def test_unheaded_envelope_with_distinct_titles_is_a_group(monkeypatch):
    from cad_agent.analysis import assembly
    region=source('踢脚1\n1、基层\n踢脚2\n面层详装修')
    monkeypatch.setattr(assembly,'local_assembly_headers',lambda *a:dict(state='unresolved',fields=[],diagnostics=['HEADER_UNRESOLVED']))
    band=dict(column_edges=[0,10],maximum_endpoint_gap=0,bbox=[0,0,10,10])
    binding=assembly._row_binding(NS(),region,band,[(region,0,len(region.text),0,'row')])
    assert binding['record_type']=='table_group' and binding['state']=='candidate'
    assert 'SUBRECORD_BOUNDARIES_REQUIRE_REVIEW' in binding['diagnostics']
