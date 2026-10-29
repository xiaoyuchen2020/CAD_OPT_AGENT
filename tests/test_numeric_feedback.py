import pytest
from cad_agent.analysis.numeric_feedback import ordinal_failure_hint
from cad_agent.cad.numbers import unsupported_values


@pytest.mark.parametrize('ordinal', ['6', '12'])
def test_same_digit_does_not_prove_clause_ordinal(ordinal):
    evidence = [{'quote': ordinal}, {'quote': '主体合理使用年限为50年。'}]
    claim = f'原文为“主体合理使用年限为50年。”位于第{ordinal}项。'
    missing = unsupported_values(claim, evidence)
    assert missing == [ordinal]
    hint = ordinal_failure_hint(claim, evidence, missing)
    assert '序号关系' in hint and 'quantity' in hint
    assert '用户确实要求序号' in hint and '缺口' in hint
    assert unsupported_values(claim, evidence) == [ordinal]
    assert unsupported_values(evidence[1]['quote'], [evidence[1]]) == []


def test_confirmed_ordinal_is_accepted_by_existing_check():
    evidence = [{'quote': '6. 主体合理使用年限为50年。'}]
    missing = unsupported_values('位于第6项，主体合理使用年限为50年。', evidence)
    assert missing == []
    assert ordinal_failure_hint('位于第6项', evidence, missing) == ''


def test_quantity_failure_is_not_misreported_as_ordinal():
    evidence = [{'quote': '主体合理使用年限为50年。'}]
    assert unsupported_values('主体合理使用年限为51年。', evidence) == ['51']
    assert ordinal_failure_hint('主体合理使用年限为51年。', evidence, ['51']) == ''
