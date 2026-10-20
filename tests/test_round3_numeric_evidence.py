"""Frozen real-source numeric replay; no model or external service calls."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cad_agent.analysis.fact_tools import CalculateFacts, calculate_facts, calculation_tokens
from cad_agent.analysis.grounding import contains_number, contains_numeric_phrase, numbers, numeric_tokens, verify_calculations
from cad_agent.analysis.session import Claim


FROZEN = Path(__file__).resolve().parents[1] / 'artifacts/bottleneck-round3-20261020/numeric-frozen-sources.json'


def session(evidence):
    return SimpleNamespace(evidence=evidence, observations={}, request=SimpleNamespace(question='计算原文数值的小计，保留单位及归属缺失。'))


@pytest.mark.parametrize('quote,value', [
    ('-4厚SBS改性沥青耐根穿刺防水卷材', '4'),
    ('-5厚抗裂砂浆面', '5'),
    ('-10厚DWM15预拌水泥防水砂浆打底扫毛（内掺5%防水剂）；', '10'),
    ('做法\n\t-12.50厚聚合物砂浆', '12.5'),
    ('-8mm厚水泥砂浆', '8'),
])
def test_layer_marker_reading_preserves_original_spans(quote, value):
    assert contains_number(quote, value)
    assert not contains_number(quote, '-' + value)
    t = next(t for t in numeric_tokens(quote) if t['interpretation'] == 'thickness_list_marker')
    assert quote[slice(*t['text_span'])] == t['raw']
    assert t['value'] == t['raw'][1:]
    assert contains_numeric_phrase(quote, t['value'] + quote[t['text_span'][1]:])
    assert not contains_numeric_phrase(quote, t['value'] + '厚另一种材料')


@pytest.mark.parametrize('quote,value', [
    ('结构标高-5.000', '5'), ('-5.000m', '5'), ('偏移-4mm', '4'),
    ('厚度-4厚砂浆', '4'), ('-4', '4'), ('−4厚砂浆', '4'),
    ('3-5厚砂浆', '5'), ('3～-5厚砂浆', '5'), ('A-4厚板', '4'),
    ('编号-4厚板', '4'), ('-4.0型砂浆', '4'), ('-4.0/5厚砂浆', '4'),
    ('-5 至 10厚砂浆', '5'), ('-5厚度偏差', '5'),
])
def test_sign_ranges_and_identifiers_are_not_list_markers(quote, value):
    assert not contains_number(quote, value)
    assert all(t['interpretation'] == 'literal' for t in numeric_tokens(quote))


def test_mixed_material_codes_keep_literal_precision_and_cannot_be_operands():
    quote = '-10厚DWM15砂浆，材料B05，型号3.0A'
    assert numbers(quote) == ['10', '15', '05', '3.0']
    assert not contains_number(quote, '15.0')
    assert not contains_number(quote, '5')
    assert not contains_numeric_phrase(quote, '10厚DWM15.0砂浆')
    for value in ('15', '05', '3.0'):
        with pytest.raises(ValueError, match='编号|型号'):
            calculate_facts(session({'a': {'quote': quote}}),
                CalculateFacts(operation='sum', quantities=[dict(origin='source', evidence_id='a', value=value)]))


def test_actual_negative_elevation_calculates_with_sign_and_unit():
    evidence = {'a': {'quote': '结构标高-5.000m'}, 'b': {'quote': '结构标高-3.000m'}}
    r = calculate_facts(session(evidence), CalculateFacts(operation='subtract', quantities=[
        dict(origin='source', evidence_id='a', value='-5', unit='m'),
        dict(origin='source', evidence_id='b', value='-3', unit='m')]))
    assert r['value'] == '-2.000'
    assert r['inputs'][0]['number_evidence'][0]['raw'] == '-5.000'
    assert numbers('标高+3.000m / -5.000m') == ['3.000', '-5.000']
    assert not contains_number('标高+3.000m', '-3')


def test_only_layer_leading_number_changes_reading():
    quote = '-5厚DWM15砂浆，130g/平方米网格布，内掺5%防水剂'
    tokens = list(numeric_tokens(quote))
    assert [t['value'] for t in tokens] == ['5', '15', '130', '5']
    assert [t['interpretation'] for t in tokens] == ['thickness_list_marker', 'literal', 'literal', 'literal']
    assert next(t for t in calculation_tokens(quote) if t['unit'] == 'percent')['interpretation'] == 'literal'


def test_calculation_binds_source_quote_and_full_original_number_span():
    original = '-10厚DWM15砂浆（内掺5%防水剂）'
    evidence = {'a': {'quote': original}, 'b': {'quote': '-5厚抗裂砂浆'}}
    args = dict(operation='sum', quantities=[
        dict(origin='source', evidence_id='a', value='10', source_quote='10厚DWM15砂浆'),
        dict(origin='source', evidence_id='b', value='5')])
    r = calculate_facts(session(evidence), CalculateFacts(**args))
    assert r['value'] == '15.000' and r['unit'] == ''
    assert r['inputs'][0]['quote'] == original
    assert r['inputs'][0]['unit_status'] == 'numeric_only_unit_unconfirmed'
    assert r['inputs'][0]['number_evidence'] == [dict(raw='-10', value='10', text_span=[0, 3], interpretation='thickness_list_marker')]
    assert list(calculation_tokens(original))[0]['value'] == '10'
    wrong = deepcopy(args)
    wrong['quantities'][0]['evidence_id'] = 'b'
    with pytest.raises(ValueError, match='不在已读原文'):
        calculate_facts(session(evidence), CalculateFacts(**wrong))
    wrong = deepcopy(args)
    wrong['quantities'][0]['source_quote'] = '10厚另一种砂浆'
    with pytest.raises(ValueError, match='source_quote'):
        calculate_facts(session(evidence), CalculateFacts(**wrong))
    wrong = deepcopy(args)
    wrong['quantities'][0]['unit'] = 'mm'
    with pytest.raises(ValueError, match='直接绑定'):
        calculate_facts(session(evidence), CalculateFacts(**wrong))


def test_inline_arithmetic_uses_same_reading_and_keeps_evidence_binding():
    evidence = {'a': {'quote': '-10厚DWM15砂浆'}, 'b': {'quote': '-5厚抗裂砂浆'}}
    claim = Claim(text='已读数值小计为15，单位未明确。', state='derived', evidence_ids=['a', 'b'],
        calculations=[dict(operation='add', operands=[dict(value='10', evidence_id='a'),
            dict(value='5', evidence_id='b')], result='15', decimal_places=0)])
    assert verify_calculations(claim, evidence) == {'15'}
    claim.calculations[0].operands[0].evidence_id = 'b'
    with pytest.raises(ValueError, match='所指原文'):
        verify_calculations(claim, evidence)


@pytest.mark.parametrize('case',
    json.loads(FROZEN.read_text()) if FROZEN.exists() else [
        pytest.param(None, marks=pytest.mark.skip(reason='Frozen real-source fixture is not distributed'))],
    ids=lambda c: c['case_id'] if c else 'unavailable')
def test_frozen_three_profile_q05_q06_sources(case):
    checked = 0
    for e in case['evidence'].values():
        for t in numeric_tokens(e['quote']):
            if t['interpretation'] != 'thickness_list_marker':
                continue
            assert contains_number(e['quote'], t['value'])
            assert e['quote'][slice(*t['text_span'])] == t['raw']
            assert e.get('entity_ids') and e.get('source_parts')
            checked += 1
    assert checked
    if case['case_id'] == 'Q06-B-luna-high':
        first, second = case['calculate_calls']
        # The first failed call additionally guessed mm: that remains rejected.
        with pytest.raises(ValueError, match='直接绑定'):
            calculate_facts(session(case['evidence']), CalculateFacts(**first['arguments']))
        r = calculate_facts(session(case['evidence']), CalculateFacts(**second['arguments']))
        assert r['status'] == 'missing_operands' and 'value' not in r
        assert [i['value'] for i in r['inputs']] == ['5', '10']
        assert all(i['number_evidence'][0]['interpretation'] == 'thickness_list_marker' for i in r['inputs'])
        assert not any(receipt['fulfilled'] for receipt in r['operation_receipts'])
