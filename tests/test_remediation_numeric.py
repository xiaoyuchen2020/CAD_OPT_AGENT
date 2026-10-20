"""Deterministic remediation checks; no model calls or frozen artifact mutation."""
import json

import pytest

from cad_agent.analysis.fact_tools import UserInputRef, UserInputValidationError, user_values
from cad_agent.analysis.grounding import contains_number, contains_numeric_phrase, numbers, verify_calculations
from cad_agent.analysis.session import CadAgentSession, Claim
from cad_agent.contracts import ChatRequest


@pytest.mark.parametrize('quote,value', [
    ('耐火极限≥3.0H', '3'), ('耐火极限≥3H', '3.000'),
    ('不大于2.0', '2'), ('不大于2', '2.0'),
    ('厚度100.00mm', '100'), ('标高-3.000m', '-3'),
    ('厚度0.50m', '0.5'), ('强度3.00MPa', '3'),
    ('材料B05', '05'), ('强度C15', '15'), ('厚度1/3', '1/3'),
])
def test_precision_equivalence_keeps_literal_identifiers(quote, value):
    assert contains_number(quote, value)


@pytest.mark.parametrize('quote,value', [
    ('耐火极限≥3.1H', '3'), ('厚度130mm', '30'),
    ('厚度-3.0mm', '3'), ('厚度3.0mm', '-3'), ('偏移-0.0', '0'),
    ('材料B05', '5'), ('材料B05', '05.0'), ('强度C15', '15.0'),
    ('材料C3.0', '3'), ('型号3.0A', '3'), ('型号3.0H0', '3'), ('编号03.0', '3'),
    ('偏移−3.0', '3'),
    ('系数1/3.0', '3'), ('编号A_3.0', '3'), ('比例1/2', '0.5'),
    ('系数1e3', '3.0'), ('面积3.0mm²', '0.000003'),
])
def test_equivalence_does_not_create_new_numeric_or_identifier_meaning(quote, value):
    assert not contains_number(quote, value)


@pytest.mark.parametrize('value', ['耐火极限≥3H', '≥3.000H', '3.00H', '3'])
def test_phrase_changes_precision_only(value):
    quote = '用户给定耐火极限≥3.0H'
    ref = UserInputRef(quote=quote, value=value)
    assert user_values([ref], quote, 'derived', '用户给定条件')
    assert ref.quote == quote and ref.value == value


@pytest.mark.parametrize('value', [
    '耐火极限≤3H', '耐火极限>3H', '耐火极限3H',
    '3h', '3mm', '3H材料C15', '3H参考100mm',
])
def test_phrase_does_not_rewrite_unit_comparator_or_material(value):
    assert not contains_numeric_phrase('参考100mm，耐火极限≥3.0H，材料C15', value)


@pytest.mark.parametrize('quote,value', [('参考100mm', '100m'), ('材料C15', 'C15.0'),
                                        ('材料BC15', 'C15'), ('材料B05', 'B5')])
def test_phrase_preserves_whole_units_and_material_codes(quote, value):
    assert not contains_numeric_phrase(quote, value)


@pytest.mark.parametrize('ref,code,path', [
    (dict(quote='给定3mm', value='3'), 'QUOTE_NOT_IN_QUESTION', '.quote'),
    (dict(quote='参考100mm', value='毫米'), 'VALUE_HAS_NO_NUMBER', '.value'),
    (dict(quote='参考100mm', value='10'), 'NUMBER_NOT_IN_QUOTE', '.value'),
    (dict(quote='参考100mm', value='100m'), 'VALUE_NOT_IN_QUOTE', '.value'),
])
def test_failed_reference_identifies_local_field_and_reason(ref, code, path):
    valid = UserInputRef(quote='给定3.0H', value='3H')
    refs = [valid, UserInputRef(**ref)]
    with pytest.raises(UserInputValidationError) as failure:
        user_values(refs, '给定3.0H，参考100mm', 'derived', '用户给定条件')
    assert failure.value.path == 'user_inputs[1]' + path
    assert failure.value.code == code
    assert code in str(failure.value)
    assert refs[0] is valid and refs[0].quote == '给定3.0H'
    refs[1] = UserInputRef(quote='参考100mm', value='100mm')
    assert user_values(refs, '给定3.0H，参考100mm', 'derived', '用户给定条件') == {'3', '100'}


@pytest.mark.parametrize('state,text,code,path', [
    ('observed', '用户参考', 'USER_INPUT_REQUIRES_DERIVED', 'state'),
    ('derived', '图纸事实', 'USER_CONDITION_NOT_LABELED', 'text'),
])
def test_user_origin_and_label_are_still_required(state, text, code, path):
    with pytest.raises(UserInputValidationError) as failure:
        user_values([UserInputRef(quote='参考100mm', value='100')], '参考100mm', state, text)
    assert (failure.value.path, failure.value.code) == (path, code)


def test_equivalent_operand_remains_bound_to_its_own_evidence():
    claim = Claim(text='由引用数值计算为5.0。', state='derived', evidence_ids=['a', 'b'],
                  calculations=[dict(operation='add', operands=[dict(value='3', evidence_id='a'),
                      dict(value='2', evidence_id='b')], result='5', decimal_places=0)])
    evidence = {'a': {'quote': '长度3.0mm'}, 'b': {'quote': '长度2.00mm'}}
    assert verify_calculations(claim, evidence) == {'5'}
    claim.calculations[0].operands[0].evidence_id = 'b'
    with pytest.raises(ValueError, match='未出现在所指原文'):
        verify_calculations(claim, evidence)
    assert evidence['a']['quote'] == '长度3.0mm'


def test_published_precision_equivalence_retains_original_source(workspace, demo_bytes):
    drawing = workspace.ingest('numeric.dxf', demo_bytes)
    session = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'], question='耐火极限是多少？'))
    session.evidence = {'fire': {'evidence_id': 'fire', 'quote': '耐火极限≥3.0H'}}
    result = session.validate_answer(json.dumps({'claims': [dict(text='耐火极限≥3H', state='observed', evidence_ids=['fire'])]}))
    assert result['claims'][0]['text'] == '耐火极限≥3H'
    assert result['citations'][0]['quote'] == '耐火极限≥3.0H'


def test_question_quote_cannot_be_normalized_instead_of_preserved():
    with pytest.raises(UserInputValidationError) as failure:
        user_values([UserInputRef(quote='参考3H', value='3')], '参考3.0H', 'derived', '用户参考')
    assert failure.value.code == 'QUOTE_NOT_IN_QUESTION'


@pytest.mark.parametrize('quote,value', [('偏移−3', '3'), ('标高−3.0m', '3.0'), ('偏移−0', '0')])
def test_literal_match_cannot_drop_native_unicode_minus(quote, value):
    assert not contains_number(quote, value)


def test_native_unicode_minus_remains_citable_without_ascii_conversion():
    quote = '用户参考标高−3.0m'
    assert numbers(quote) == ['−3.0']
    assert contains_number(quote, '−3.0')
    assert contains_numeric_phrase(quote, '−3.0m')
    assert not contains_number(quote, '-3.0')
    assert user_values([UserInputRef(quote=quote, value='−3.0m')], quote,
                       'derived', '用户参考标高') == {'−3.0'}
