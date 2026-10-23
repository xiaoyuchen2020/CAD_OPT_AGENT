"""Numbered prose must not invent decimals or weaken source-bound quantities."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import cad_agent.analysis.delivery as delivery
from cad_agent.analysis.grounding import numbers, without_claim_list_markers
from test_siwc import make_session


FIXTURE = json.loads((Path(__file__).parent/'fixtures/inline-numbered-claims.json').read_text())


@pytest.mark.parametrize('text,expected', [
    ('层次：1.饰面；2.腻子；3.5厚砂浆；4.20厚岩棉', ['5', '20']),
    ('1. 饰面\n2. 底涂\n3. 1.5厚涂膜', ['1.5']),
    ('1.饰面;2.底涂;3.1.5厚涂膜;4.外墙NYS1:1.5净浆;5.35厚岩棉', ['1.5', '1', '1.5', '35']),
    ('1.-5厚砂浆\n2.底涂\n3.1.5厚涂膜', ['-5', '1.5']),
    ('1.饰面；2.底涂；3.-3.000m标高', ['-3.000']),
    ('1.饰面；2.底涂；3.A-05材料', ['-05']),
    ('1）外墙饰面；2）腻子；3）5厚砂浆；4）20厚岩棉', ['5', '20']),
    ('（1）3.5厚涂膜；（2）1.5厚砂浆；（3）-5mm偏移', ['3.5', '1.5', '-5']),
    ('(1)3.5厚涂膜;(2)1.5厚砂浆;(3)-5mm偏移', ['3.5', '1.5', '-5']),
    ('1、3.5厚涂膜；2、1.5厚砂浆；3、20厚保护层', ['3.5', '1.5', '20']),
])
def test_consecutive_list_separates_markers_and_preserves_body(text, expected):
    projected = without_claim_list_markers(text)
    assert len(projected) == len(text)
    assert numbers(projected) == expected


@pytest.mark.parametrize('text', [
    '3.5厚砂浆', '厚度：3.5mm；标高：-3.000m',
    '1.5厚涂膜；2.0厚砂浆；3.5厚保护层',
    '1.5厚涂膜；2.底涂；3.5厚保护层',
    '1.饰面；2.0厚砂浆；3.5厚保护层',
    '1.饰面；2.底涂；4.20厚岩棉',
    '6.5厚砂浆；7.35厚岩棉；8.3厚粘接砂浆',
    'GB50016第3.5条；A-05材料；比例1:1.5',
    '1.2.3条；1.2.4条；1.2.5条',
    '(1.5厚涂膜；(2.0厚砂浆；(3.5厚保护层',
])
def test_ambiguous_lists_decimal_values_and_codes_stay_literal(text):
    projected = without_claim_list_markers(text)
    from cad_agent.cad.numbers import readings
    assert [t["value"] for t in readings(text) if t["kind"]=="ambiguous"] == [t["value"] for t in readings(projected) if t["kind"]=="ambiguous"]
    for value in ("3.5", "4.20", "1.5", "2.0", "6.5", "7.35", "8.3"):
        if value in text: assert value in projected


@pytest.mark.parametrize('index', range(5))
def test_real_rejected_claim_passes_without_changing_answer_or_quotes(workspace, demo_bytes, monkeypatch, index):
    session = make_session(workspace, demo_bytes)
    session.evidence = deepcopy(FIXTURE['evidence'])
    claim = deepcopy(FIXTURE['claims'][index])
    content = json.dumps({'claims':[claim]}, ensure_ascii=False)
    result = session.validate_answer(content)
    assert result['claims'][0]['text'] == claim['text']
    assert '第3项：' in result['claims'][0]['display_text']
    assert {c['evidence_id']:c['quote'] for c in result['citations']} == {
        eid:FIXTURE['evidence'][eid]['quote'] for eid in claim['evidence_ids']}
    # Low-level lexical API stays compatible; semantic source readers now split ordinals.
    assert '3.5' in numbers('1.饰面；2.腻子；3.5厚砂浆')


@pytest.mark.parametrize('old,new,missing', [
    ('3.5厚', '3.999厚', '999'),
    ('3.5厚', '3.5.25厚', '5.25'),
    ('3.5厚', '3.-5厚', '-5'),
    ('4.20厚', '4.20.5厚', '20.5'),
    ('145g', '999g', '999'),
    ('DPM20', 'DPM99', '99'),
])
def test_list_format_does_not_admit_wrong_values(workspace, demo_bytes, old, new, missing):
    session = make_session(workspace, demo_bytes)
    session.evidence = deepcopy(FIXTURE['evidence'])
    claim = deepcopy(FIXTURE['claims'][0])
    assert old in claim['text']
    claim['text'] = claim['text'].replace(old,new)
    with pytest.raises(delivery.AnswerValidationError) as error:
        session.validate_answer(json.dumps({'claims':[claim]}))
    assert missing in str(error.value)


def test_standalone_decimal_cannot_borrow_integer_source(workspace, demo_bytes):
    session = make_session(workspace, demo_bytes)
    session.evidence = {'a':{'evidence_id':'a','quote':'5厚抗裂砂浆'}}
    with pytest.raises(delivery.AnswerValidationError, match='3.5'):
        session.validate_answer(json.dumps({'claims':[dict(text='3.5厚抗裂砂浆',state='observed',evidence_ids=['a'])]}))


def test_parenthesized_ordinals_are_not_source_values(workspace, demo_bytes):
    session = make_session(workspace, demo_bytes)
    session.evidence = {'a':{'evidence_id':'a','quote':'面层、底涂、5厚砂浆'}}
    claim = dict(text='1）面层；2）底涂；3）5厚砂浆',state='observed',evidence_ids=['a'])
    assert session.validate_answer(json.dumps({'claims':[claim]}))['claims']
    claim['text'] = claim['text'].replace('3）5','3）3.5')
    with pytest.raises(delivery.AnswerValidationError, match='3.5'):
        session.validate_answer(json.dumps({'claims':[claim]}))


def test_numbered_tool_observation_still_checks_body_values(workspace, demo_bytes):
    session = make_session(workspace, demo_bytes)
    session.observations = {'o':dict(observation_id='o',tool='query_entities',data={'length':20})}
    claim = dict(text='1.定位结果；2.仅记录工具返回；3.20长',state='derived',
                 evidence_basis='tool_observation',observation_ids=['o'])
    assert session.validate_answer(json.dumps({'claims':[claim]}))['claims']
    claim['text'] = claim['text'].replace('3.20','3.999')
    with pytest.raises(delivery.AnswerValidationError, match='999'):
        session.validate_answer(json.dumps({'claims':[claim]}))
