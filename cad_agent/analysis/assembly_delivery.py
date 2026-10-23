"""Deliver source-bound construction fields without inventing instance scope."""
import re

from cad_agent.analysis.assembly import HEADING
from cad_agent.contracts import stable_id


def _span_overlap(a, b):
    return (a.get('snapshot_id') == b.get('snapshot_id')
            and a.get('region_id') == b.get('region_id')
            and a.get('text_span') and b.get('text_span')
            and a['text_span'][0] < b['text_span'][1]
            and b['text_span'][0] < a['text_span'][1])


def ownership_issues(claim, evidence, observations=None):
    """Detect affirmative reassignment of an explicitly pre-heading source line.

    A citation proves text, not ownership. Use only this run's read character
    spans, including a containing read when the claim cites a smaller fragment.
    Literal excerpts, negated attribution and independent source facts survive.
    This is a deterministic boundary guard, not a general paraphrase verifier.
    """
    if claim.evidence_basis != 'source':
        return []
    cited = [evidence[eid] for eid in claim.evidence_ids if eid in evidence]
    if not cited or any(claim.text in e.get('quote', '') for e in cited):
        return []
    issues = []
    # A field read from a differently named construction cannot be reassigned
    # simply by citing the desired title beside it. Keep quotations/negations.
    from cad_agent.cad.assembly_headings import FAMILY
    wanted = set(FAMILY.findall(claim.text))
    if len(wanted) == 1 and claim.state in ('observed', 'derived') and not re.search('不属于|不能归|归属.*未|候选|未确认', claim.text):
        for observation in (observations or {}).values():
            if observation.get('tool') != 'read_assembly':
                continue
            data = observation.get('data', {})
            binding = data.get('source_binding') or {}
            if binding.get('state') != 'source_bound':
                continue
            heading = HEADING.search((data.get('title_evidence') or {}).get('quote', ''))
            if not heading or heading[1] in wanted:
                continue
            for item in data.get('items', []):
                ref = item.get('evidence') or {}
                if not any(_span_overlap(ref, e) for e in cited):
                    continue
                body = re.sub(r'^\s*\d+[、．.]\s*', '', ref.get('quote', '')).strip()
                if len(body) >= 4 and body in claim.text:
                    issues.append('来源字段归属冲突：引用原文属于' + heading[1] + heading['code'] + '，不能按共同材料词转归其他对象。')
    for whole in evidence.values():
        quote = whole.get('quote', '')
        if not whole.get('text_span'):
            continue
        for heading in HEADING.finditer(quote):
            # Only the preceding logical line is known to be before this title;
            # do not guess which earlier construction owns it.
            prefix = quote[:heading.start()].rstrip().split('\n')[-1]
            body = re.sub(r'^\s*\d+[、．.]\s*', '', prefix).strip()
            if len(body) < 4:
                continue
            start = whole['text_span'][0] + quote.rfind(prefix, 0, heading.start())
            excluded = {**whole, 'text_span': [start, whole['text_span'][0] + heading.start()]}
            if not any(_span_overlap(e, excluded) for e in cited):
                continue
            code = heading[1] + heading['code']
            code_pattern = re.compile(re.escape(code) + r'(?![A-Za-z0-9])')
            short_pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(heading['code']) + r'(?![A-Za-z0-9])')
            for sentence in re.split(r'[。；;\n]', claim.text):
                if not (code_pattern.search(sentence) or short_pattern.search(sentence)):
                    continue
                # Exact full layer content, or an explicitly quoted subphrase,
                # is a bound lexical witness; generic shared words are not.
                witnesses = [body] + [m[1] for m in re.finditer(r'[“「"]([^”」"]{4,})[”」"]', sentence)
                                      if m[1] in body]
                for witness in witnesses:
                    at = sentence.find(witness)
                    if at < 0:
                        continue
                    # A repeated material may independently occur inside this
                    # construction too. An explicit in-section citation wins;
                    # the mere presence of a previous-layer quote is no error.
                    supported = False
                    for source in cited:
                        text = source.get('quote', '')
                        headings = list(HEADING.finditer(text))
                        for pos, own in enumerate(headings):
                            if own[1] + own['code'] == code:
                                end = headings[pos+1].start() if pos+1 < len(headings) else len(text)
                                supported |= witness in text[own.end():end]
                    if supported:
                        continue
                    context = sentence[max(0, at-28):at+len(witness)+28]
                    if re.search(r'不属于|不能.{0,8}(?:并入|归|认定)|不得|并非|不是|标题前|上一(?:项|段|做法)|归属.{0,5}(?:未|不明)|未(?:能|确认|查明)', context):
                        continue
                    issues.append(f'来源标题边界冲突：{code} 标题前的“{witness}”不属于该标题后的做法；'
                                  f'来源 {whole["evidence_id"]} 字符范围 {excluded["text_span"]}。'
                                  '请拆分标题事实和前项原文，读取该做法自身层次，不得按合并区域归属。')
                    break
                if issues:
                    break
    return list(dict.fromkeys(issues))


def delivery_fields(data, evidence, candidate_requirement_ids=()):
    """Build executable claim payloads only for exact, source-bound read fields.

    These are selectable source facts, never requirement or operation receipts.
    Requirement IDs must be selected for the actual question; read ownership is
    exposed separately and cannot mechanically fulfil every attached requirement.
    """
    binding = data.get('source_binding') or {}
    if data.get('view') != 'record' or binding.get('state') != 'source_bound':
        return []
    title = evidence.get((data.get('title_evidence') or {}).get('evidence_id'))
    subjects = ([title] if binding.get('code') and title else
                [evidence[e['evidence_id']] for field in binding.get('fields', [])
                 if set(field.get('roles', [])) & {'code', 'name', 'applicability'}
                 for e in field.get('evidence', []) if e.get('evidence_id') in evidence])
    if not subjects:
        return []
    subject = '；'.join(dict.fromkeys(e['quote'].strip() for e in subjects))
    subject_ids = list(dict.fromkeys(e['evidence_id'] for e in subjects))
    result = []
    for item in data.get('items', []):
        if item.get('source_binding_state') != 'source_bound' or item.get('truncated'):
            continue
        ref = item.get('evidence') or {}
        value = evidence.get(ref.get('evidence_id'))
        if not value or not value.get('quote', '').strip():
            continue
        text = '来源构造“' + subject + '”的已读字段原文：' + value['quote'].strip()
        if len(text) > 1500:
            continue  # Never truncate a source clause to make a claim fit.
        ids = list(dict.fromkeys(subject_ids + [value['evidence_id']]))
        result.append(dict(
            field_id=stable_id('assembly-field', data['record_id'], value['evidence_id']),
            record_id=data['record_id'], subject=subject,
            field_roles=item.get('source_field_roles') or ['source_section'],
            source_span=value.get('text_span'),
            candidate_requirement_ids=list(candidate_requirement_ids),
            object_instance_state='unverified', engineering_applicability_state='unverified',
            layer_completeness=binding.get('layer_completeness', 'unverified'),
            claim_template=dict(text=text, state='observed', evidence_basis='source',
                                evidence_ids=ids, evidence_roles={'subject': subject_ids, 'property': [value['evidence_id']]},
                                question_part_ids=[])))
    return result
