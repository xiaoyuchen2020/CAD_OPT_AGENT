"""Source/list ambiguity and arithmetic identity, including real failed drafts."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cad_agent.cad.numbers import readings, ordinals, evidence_readings, format_numbered_text, unsupported_values
from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts
from cad_agent.analysis.grounding import verify_calculations
from cad_agent.analysis.tool_registry import Claim
from cad_agent.analysis.assembly import _fragments
from cad_agent.contracts import SourceRegion, SourcePart
from cad_agent.evidence import bind_evidence


def source(text):
    return SourceRegion(region_id='r',frame_name='f',layout_id='l',frame_id='f',kind='paragraph',
        text=text,bbox=[0,0,20,10],entity_ids=['t'],parts=[SourcePart(entity_id='t',entity_text_span=[0,len(text)],text_span=[0,len(text)],bbox=[0,0,20,10])])


def compute(evidence, value, **kw):
    session=SimpleNamespace(evidence={'e':evidence},observations={},request=SimpleNamespace(question='计算原文数值'))
    return calculate_facts(session,CalculateFacts(operation='sum',quantities=[
        dict(origin='source',evidence_id='e',value=value,**kw)]))


def test_source_number_splitting_reaches_parser_validation_and_calculation():
    text='1.饰面\n2.底涂\n3.5mm厚砂浆\n4.20mm厚岩棉'
    region=source(text)
    index=SimpleNamespace(evidence=lambda r,a,b:bind_evidence('s',r,a,b,'unknown').model_dump())
    fragments=_fragments(index,region,0,len(text))
    third=fragments[2]; e=third['evidence']; t=third['thickness_candidates'][0]
    assert third['layer_number']==3 and t['value']=='5'
    assert third['text']=='3.5mm厚砂浆' and third['display_text']=='第3项：5mm厚砂浆'
    assert e['quote']=='3.5mm厚砂浆'
    assert not unsupported_values('第3项：5mm厚砂浆',[e])
    assert not unsupported_values(e['quote'],[e])
    from cad_agent.cad.numbers import format_claim_text
    assert format_claim_text(e['quote'],[e])=='第3项：5mm厚砂浆'
    assert unsupported_values('厚度3.5mm',[e])==['3.5']
    result=compute(e,'5',unit='mm',quantity_id=t['quantity_id'])
    assert result['value']=='5.000'
    assert result['inputs'][0]['quantity_ids']==[t['quantity_id']]
    for wrong in ('3','3.5'):
        with pytest.raises(ValueError):compute(e,wrong,unit='mm')
    raw=source(text)
    assert raw.text==text


@pytest.mark.parametrize('text', ['3.5厚砂浆','３．５厚砂浆','1.5厚涂膜；2.0厚砂浆；3.5厚保护层'])
def test_ambiguous_decimal_is_preserved_and_cannot_be_used_in_either_calculator(text):
    e={'quote':text}
    ambiguous=[t for t in readings(text) if t['kind']=='ambiguous']
    assert ambiguous and format_numbered_text(text)==text
    for t in ambiguous:
        with pytest.raises(ValueError):compute(e,t['value'],quantity_id=t['number_id'])
        claim=Claim(text='计算结果为7',state='derived',evidence_ids=['e'],calculations=[dict(
            operation='add',operands=[dict(evidence_id='e',value=t['value'])]*2,result='7')])
        with pytest.raises(ValueError):verify_calculations(claim,{'e':e})


@pytest.mark.parametrize('text,value,kind', [
    ('厚度3.5mm','3.5','quantity'),('标高-0.100m','-0.100','quantity'),
    ('M7.5砂浆','7.5','identifier'),('外墙1','1','identifier'),
    ('J03','03','identifier'),('第3.5.1条','3.5.1','identifier'),
    ('比例1:1.5','1.5','expression_component'),('厚度3-5mm','3','expression_component'),
])
def test_true_numbers_codes_signs_ranges_and_chapters(text,value,kind):
    assert next(t for t in readings(text) if t['value']==value)['kind']==kind
    if kind!='quantity':
        with pytest.raises(ValueError):compute({'quote':text},value)


def test_fullwidth_numbering_offsets_and_formatting():
    text='１．饰面；２．底涂；３．５厚砂浆；４．２０厚岩棉'
    atoms=readings(text)
    assert [t['value'] for t in atoms if t['kind']=='ordinal']==['1','2','3','4']
    assert [t['value'] for t in atoms if t['kind']=='quantity']==['5','20']
    assert all(text[slice(*t['text_span'])]==t['raw'] for t in atoms)
    assert format_numbered_text(text)=='第1项：饰面；第2项：底涂；第3项：５厚砂浆；第4项：２０厚岩棉'


@pytest.mark.parametrize('text', ['（3）5厚砂浆','③5厚砂浆','3、5厚砂浆','3. 5厚砂浆'])
def test_explicit_partial_lists_need_no_invented_previous_items(text):
    assert [t['value'] for t in ordinals(text)]==['3']
    assert format_numbered_text(text)=='第3项：5厚砂浆'


def test_duplicate_quantities_require_identity_and_cannot_borrow_units():
    e={'quote':'厚度5mm，长度5m','evidence_id':'e'}
    with pytest.raises(ValueError,match='重复'):compute(e,'5',unit='mm')
    first=evidence_readings(e)[0]
    assert compute(e,'5',unit='mm',quantity_id=first['number_id'])['value']=='5.000'
    with pytest.raises(ValueError,match='单位'):compute(e,'5',unit='m',quantity_id=first['number_id'])
    with pytest.raises(ValueError):compute(e,'5',quantity_id='invented')


def test_source_slicing_keeps_context_and_identity_not_a_new_decimal():
    text='1.饰面\n2.底涂\n3.5厚砂浆'
    region=source(text);start=text.index('3.5')
    full=bind_evidence('s',region,0,len(text),'unknown').model_dump()
    piece=bind_evidence('s',region,start,len(text),'unknown').model_dump()
    quantity=next(t for t in evidence_readings(piece) if t['kind']=='quantity')
    whole=next(t for t in evidence_readings(full) if t['number_id']==quantity['number_id'])
    assert quantity['value']=='5' and quantity['source_span']==whole['source_span']
    assert piece['quote'][slice(*quantity['text_span'])]=='5'
    assert compute(piece,'5',quantity_id=quantity['number_id'])['value']=='5.000'
    # A stale catalog cannot give a new quote a former value or classification.
    stale=deepcopy(piece);stale['quote']='3.5厚另一材料'
    assert next(t for t in evidence_readings(stale) if t['value']=='3.5')['kind']=='ambiguous'


def test_source_ordinal_and_grade_cannot_back_quantity_claim():
    assert unsupported_values('厚度3mm',[{'quote':'3、基层'}])==['3']
    assert unsupported_values('厚度7.5mm',[{'quote':'M7.5砂浆'}])==['7.5']
    assert unsupported_values('DPM99砂浆',[{'quote':'DPM20砂浆，厚99mm'}])==['99']
    assert not unsupported_values('第3项为基层',[{'quote':'3、基层'}])
    assert unsupported_values('第9项为基层',[{'quote':'3、基层'}])==['9']


def test_original_rejected_claims_keep_exact_quotes_and_get_safe_display():
    fixture=json.loads((Path(__file__).parent/'fixtures/inline-numbered-claims.json').read_text())
    for claim in fixture['claims']:
        evidence=[fixture['evidence'][eid] for eid in claim['evidence_ids']]
        assert not unsupported_values(claim['text'],evidence)
        assert '第3项：' in format_numbered_text(claim['text'])
        assert '3.5厚' not in format_numbered_text(claim['text'])


def test_separate_aligned_number_entity_resolves_without_guessing_from_digits():
    r=source('3.5厚砂浆')
    r.parts=[SourcePart(entity_id='ordinal',entity_text_span=[0,2],text_span=[0,2],bbox=[0,0,2,2]),
             SourcePart(entity_id='body',entity_text_span=[0,4],text_span=[2,6],bbox=[2.5,0,8,2])]
    e=bind_evidence('s',r,0,len(r.text),'unknown').model_dump()
    assert [(t['kind'],t['value']) for t in evidence_readings(e)]==[('ordinal','3'),('quantity','5')]
    assert compute(e,'5')['value']=='5.000'
    # Adjacent columns or mismatched baselines must not grant that interpretation.
    r.parts[1].bbox=[20,5,28,7]
    other=bind_evidence('s',r,0,len(r.text),'unknown').model_dump()
    assert evidence_readings(other)[0]['kind']=='ambiguous'
    with pytest.raises(ValueError):compute(other,'5')
