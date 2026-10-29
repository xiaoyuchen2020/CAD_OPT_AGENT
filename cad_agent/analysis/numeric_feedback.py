"""Explain semantic numeric mismatches without changing acceptance checks."""
import json
from cad_agent.cad.numbers import readings, evidence_readings


def ordinal_failure_hint(text, evidence, missing):
    rejected = sorted({t['value'] for t in readings(text)
                       if t['kind'] == 'ordinal_reference' and t['value'] in missing})
    if not rejected:
        return ''
    kinds = {value: sorted({t['kind'] for e in evidence for t in evidence_readings(e)
                           if t['value'] == value}) for value in rejected}
    return ('序号语义核对：' + json.dumps(kinds, ensure_ascii=False)
            + '。被拒绝的是“第…项/条”等序号关系，不是单纯查找相同数字。'
            'quantity/ambiguous/identifier 不证明 ordinal_reference；独立数字、图元邻近或同一区域也不能证明条文从属。'
            '不要只把含同值数字的来源加入 evidence_ids 后原样重试，也不要把它拼接进完整原文。'
            '若用户未要求条文号且正文已有完整原生片段，请交付该片段逐字原文及其证据定位，删除额外且未证实的序号关系；'
            '若用户确实要求序号，则须读取并验证序号关系，不能确认时明确该项缺口。')
