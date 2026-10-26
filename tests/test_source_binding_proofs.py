"""Alternative source proofs preserve text and refuse ambiguous ownership."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from cad_agent.analysis.assembly import _bounded_section, _section_binding
from cad_agent.analysis.source_binding_proofs import MISSING_HEADERS, closed_title_and_layers


def region(key, text, box):
    return NS(region_id=key, text=text, bbox=box,
              parts=[NS(entity_id=key, text_span=[0, len(text)], bbox=box)])


def evidence(r, a, b):
    return dict(region_id=r.region_id, text_span=[a, b], quote=r.text[a:b])


def row_fixture(scale=1, shift=0):
    def box(x, y, w, h):
        return [shift + scale * v for v in (x, y, x+w, y+h)]
    title=region('title', '踢脚', box(1, 2, 8, 2))
    body=region('body', '1、15厚石材\n2、10厚砂浆\n3、基层清理', box(11, 2, 18, 2))
    sources=[(title, 0, len(title.text), 0, 'row'), (body, 0, len(body.text), 1, 'row')]
    band=dict(bbox=box(0, 0, 30, 10), maximum_endpoint_gap=0,
              endpoint_tolerance=.1, source_lines=[dict(entity_id='rail')])
    binding=dict(state='candidate', diagnostics=sorted(MISSING_HEADERS), identity_columns=[], layer_columns=[])
    return NS(evidence=evidence), title, sources, band, binding


@pytest.mark.parametrize('scale,shift', [(1,0), (.01,-1234), (100,900000)])
def test_closed_title_layers_proof_is_translation_scale_independent(scale,shift):
    index,title,sources,band,binding=row_fixture(scale,shift)
    before=deepcopy(binding)
    proof=closed_title_and_layers(index,title,sources,band,binding,[])
    assert proof['layer_numbers']==[1,2,3]
    assert proof['title_evidence'][0]['quote']=='踢脚'
    assert proof['layer_evidence'][0]['quote']==sources[1][0].text
    assert proof['source_line_ids']==['rail']
    assert binding==before  # No header role or matching identity is invented.
    assert closed_title_and_layers(index,title,list(reversed(sources)),band,binding,[])==proof


@pytest.mark.parametrize('body', [
    '1、砂浆\n3、基层', '1、砂浆\n1、替代砂浆', '2、砂浆\n3、基层',
    '说明\n1、砂浆\n2、基层', '1、\n2、基层',
    '1、砂浆\n踢脚2\n2、基层', '1、砂浆',
    '1、砂浆\n1a.替代层次\n2、基层',
])
def test_missing_repeated_empty_or_mixed_layers_stay_candidates(body):
    index,title,sources,band,binding=row_fixture()
    r=sources[1][0]; r.text=body; r.parts[0].text_span=[0,len(body)]
    sources[1]=(r,0,len(body),1,'row')
    assert closed_title_and_layers(index,title,sources,band,binding,[]) is None


@pytest.mark.parametrize('problem', ['second_title_column','second_layer_column','mixed_title','wrong_target','clipped',
                                    'ambiguous','gap','other_blocker','unavailable_geometry'])
def test_ambiguous_or_incomplete_row_proof_is_refused(problem):
    index,title,sources,band,binding=row_fixture(); diagnostics=[]
    if problem in ('second_title_column','second_layer_column'):
        r=region('extra','外墙2' if problem=='second_title_column' else '1、另一个做法\n2、基层',[25,2,29,4]);sources.append((r,0,len(r.text),2,'row'))
    elif problem=='mixed_title':
        title.text='踢脚1\n踢脚2';title.parts[0].text_span=[0,len(title.text)];sources[0]=(title,0,len(title.text),0,'row')
    elif problem=='wrong_target': title=region('other','外墙',[1,2,9,4])
    elif problem=='clipped': sources[1][0].parts[0].bbox=[11,-1,29,4]
    elif problem=='ambiguous': diagnostics=['ROW_ENVELOPE_AMBIGUOUS']
    elif problem=='gap': band['maximum_endpoint_gap']=1
    elif problem=='other_blocker': binding['diagnostics'].append('SUBRECORD_BOUNDARIES_REQUIRE_REVIEW')
    elif problem=='unavailable_geometry': sources[1][0].parts=[]
    assert closed_title_and_layers(index,title,sources,band,binding,diagnostics) is None


def test_closed_row_additional_cell_keeps_untyped_original_text():
    index,title,sources,band,binding=row_fixture()
    r=region('extra','室外地面标高以下',[25,2,29,4]);sources.append((r,0,len(r.text),2,'row'))
    proof=closed_title_and_layers(index,title,sources,band,binding,[])
    assert proof['untyped_columns']==[2]
    assert proof['untyped_evidence'][0]['quote']==r.text
    assert binding['identity_columns']==[] and binding['layer_columns']==[]


def test_untyped_reference_cell_is_not_silently_resolved():
    index,title,sources,band,binding=row_fixture()
    r=region('extra','参照22J403',[25,2,29,4]);sources.append((r,0,len(r.text),2,'row'))
    assert closed_title_and_layers(index,title,sources,band,binding,[])['requires_reference_read']


def section(body, closed=True, gap=False):
    title=region('title','踢脚3：（住宅房间）',[0,10,100,11])
    content=region('body',body,[0,-100 if gap else 8,100,-99 if gap else 9])
    rows=[title,content]
    if closed: rows.append(region('next','踢脚4：（其它房间）',[0,-102 if gap else 6,100,-101 if gap else 7]))
    sources,stop=_bounded_section(rows,title)
    return _section_binding(NS(evidence=evidence),title,sources,stop)


@pytest.mark.parametrize('body', ['150高素水泥浆罩面','10厚抛光砖，4厚专用粘贴剂粘贴','做法详室内装修设计'])
def test_bounded_unnumbered_practice_is_source_paragraph_not_numbered_layers(body):
    result=section(body)
    assert result['state']=='source_bound'
    assert result['record_type']=='source_section' and result['layer_numbers']==[]
    assert result['layer_order']=='source_reading_order_only'
    assert result['basis']=='explicit_heading_bounded_source_paragraph'
    assert result['warnings']==['UNNUMBERED_SOURCE_PARAGRAPH']
    assert result['object_instance_state']=='unverified'
    if '详' in body:
        assert result['layer_completeness']=='requires_reference_read'
        assert result['diagnostics']==['EXTERNAL_ASSEMBLY_REFERENCE_REQUIRES_READ']


@pytest.mark.parametrize('body,closed,gap', [
    ('150高素水泥浆罩面',False,False),('150高素水泥浆罩面',True,True),
    ('（无保温）',True,False),('（详装修设计）',True,False),
    ('1、砂浆\n3、刷涂料',True,False),('②刷涂料',True,False),
    ('一、刷涂料',True,False),('',True,False),
    ('1a.装饰面层，详精装修图纸\n1b.10厚面砖',True,False),
    ('棚4A-外墙涂料顶棚1\n消防连廊',True,False),
    ('墙1（无机涂料）墙裙以上',True,False),
    ('建筑做法\n精装做法',True,False),
    ('105J909工程做法\n国家建筑标准图集914J936',True,False),
    ('商铺、物业经营等\n（铝板饰面）',True,False),
])
def test_unnumbered_recovery_does_not_hide_missing_boundaries_or_damaged_lists(body,closed,gap):
    assert section(body,closed,gap)['state']=='candidate'


@pytest.mark.parametrize('body',['1、砂浆\n2、刷涂料','①刷涂料'])
def test_existing_numbered_sequence_keeps_original_proof(body):
    result=section(body)
    assert result['state']=='source_bound'
    assert result['basis']=='explicit_heading_bounded_numbered_section'
    assert result['record_type']=='layer_assembly' and result['warnings']==[]
