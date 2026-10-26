"""Bounded title discovery for an explicit query contract. Never reads body pages."""
import re
from typing import Literal
from pydantic import Field
from cad_agent.contracts import Record, stable_id
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.query_intent import normalize

ALIASES = {'内墙': ('内墙', '内墙面', '内墙饰面'), '外墙': ('外墙', '外墙面', '外墙饰面'),
           '顶棚': ('顶棚', '天棚', '天花板'), '踢脚': ('踢脚', '踢脚线'), '雨篷': ('雨篷', '雨蓬')}


class SearchSourceRecords(Record):
    target_id: str
    snapshot_id: str = ''
    frame_id: str = ''
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=8)
    alternatives: list[str] = Field(default_factory=list, max_length=4)
    expansion_reason: str = Field(default='', max_length=300)


def names(subject):
    return next((values for values in ALIASES.values() if subject in values), (subject,))


def title_match(text, subject, identifier):
    """Match numbered title tokens, not ordinal substrings or material quantities."""
    text = normalize(text)
    if re.search(r'参见|详见|参照|见图|采用|同内墙|同外墙', text):
        return False
    variants = [normalize(x) for x in names(subject)]
    for name in sorted(variants, key=len, reverse=True):
        if identifier:
            code = normalize(identifier)
            for variant in variants:
                if code.startswith(variant):
                    code = code[len(variant):]
                    break
            # A title may have a scope prefix; a following digit/letter belongs
            # to the code and must not turn 内墙40 into 内墙4.
            # Models may bind subject='内墙4', identifier='4', or bind the
            # class and complete identifier separately. Do not append the same
            # identifier twice; retain token boundaries (14 is not 4).
            suffix = re.search(r'[a-z0-9]+$', name)
            needle = name if code and suffix and suffix.group() == code else name + code
            hit = re.search(re.escape(needle) + r'(?![0-9a-z])', text)
            if hit and len(text[:hit.start()]) <= 24:
                return True
        elif re.match(r'.{0,20}?' + re.escape(name) + r'(?:[a-z]?\d+[a-z]?|$|[（(])', text):
            return True
    return False


def scope_assessment(index, region, title, target):
    labels = [('title', title), ('frame', region.frame_name)]
    labels += [('parser_parent', index.by_id[rid].text[:400]) for rid in region.context_region_ids if rid in index.by_id]
    checks = []
    for condition in target['conditions']:
        if condition['kind'] != 'scope':
            checks.append(dict(condition=condition, state='unresolved', basis=[]))
            continue
        term = normalize(condition['value'])
        hits = [dict(kind=kind, text=text) for kind, text in labels if term in normalize(text)
                and not re.search(r'(?:非|不属于|除|不含|不包括|不包含)' + re.escape(term), normalize(text))]
        state = ('supported' if condition['operator'] == 'include' else 'conflicting') if hits else 'unresolved'
        checks.append(dict(condition=condition, state=state, basis=hits))
    state = 'conflicting' if any(c['state'] == 'conflicting' for c in checks) else (
        'unresolved' if any(c['state'] == 'unresolved' for c in checks) else 'supported')
    return state, checks


def search(session, args):
    intent = session.task.data.get('query_intent')
    if not intent:
        raise ValueError('QUERY_INTENT_REQUIRED: register the original query first')
    target = next((t for t in intent['intent']['targets'] if t['target_id'] == args.target_id), None)
    if not target:
        raise ValueError('QUERY_TARGET_UNKNOWN')
    if args.alternatives and (not args.expansion_reason.strip() or any(not t.strip() or len(t)>120 for t in args.alternatives)):
        raise ValueError('QUERY_EXPANSION_REASON: bounded alternatives require a specific retrieval gap')
    snapshots = [args.snapshot_id] if args.snapshot_id else [session.request.snapshot_id] + [d.snapshot_id for d in session.request.related_drawings]
    if not set(snapshots) <= session.allowed_snapshots:
        raise ValueError('QUERY_SNAPSHOT_DENIED')
    candidates, conflicts = [], []
    signatures = {}
    subject = target['subject']['value']
    identifier = (target.get('identifier') or {}).get('value', '')
    for sid in snapshots:
        session._remaining()
        index = session.index if sid == session.request.snapshot_id else DrawingIndex(session.w, sid)
        signatures[sid] = list(index.signature)
        for region in index.scope(frame_id=args.frame_id):
            start = 0
            for line in region.text.splitlines(keepends=True):
                title = line.strip()
                exact = title_match(title, subject, identifier)
                alternative = not exact and any(title_match(title, x, identifier) for x in args.alternatives)
                if title and len(title) <= 240 and (exact or alternative):
                    state, checks = scope_assessment(index, region, title, target)
                    cid = stable_id('candidate', intent['intent_id'], args.target_id, sid, region.region_id, str(start))
                    item = dict(candidate_id=cid, target_id=args.target_id, snapshot_id=sid,
                        region_id=region.region_id, title=title, heading_start=start,
                        layout_id=region.layout_id, frame_id=region.frame_id, frame_name=region.frame_name,
                        match_kind='alternative' if alternative else 'exact', scope_state=state,
                        scope_checks=checks, body_read=False, signature=list(index.signature),
                        next_action=dict(tool='read_assembly', arguments=dict(snapshot_id=sid,
                            region_id=region.region_id, heading_start=start, offset=0, limit=12,
                            task_ids=[target['requirement_id']])))
                    (conflicts if state == 'conflicting' else candidates).append(item)
                start += len(line)
    candidates.sort(key=lambda c: (c['match_kind'] != 'exact', c['scope_state'] != 'supported',
                                   c['snapshot_id'], c['frame_id'] or '', c['region_id'], c['heading_start']))
    page = candidates[args.offset:args.offset+args.limit]
    ledger = session.task.data.setdefault('source_candidates', {})
    for item in page:
        ledger[item['candidate_id']] = item
    receipt = dict(intent_id=intent['intent_id'], target_id=args.target_id, offset=args.offset,
        returned_ids=[i['candidate_id'] for i in page], total=len(candidates),
        has_more=args.offset+len(page)<len(candidates), excluded_count=len(conflicts),
        alternatives=args.alternatives, expansion_reason=args.expansion_reason, signatures=signatures)
    session.task.data.setdefault('query_searches', []).append(receipt)
    return dict(items=[{k:v for k,v in c.items() if k!='signature'} for c in page],
        **{k:v for k,v in receipt.items() if k!='signatures'},
        next_offset=args.offset+len(page) if receipt['has_more'] else None,
        excluded=[dict(candidate_id=c['candidate_id'], title=c['title'], scope_checks=c['scope_checks']) for c in conflicts[:5]],
        coverage='indexed_title_candidates_only', scope_exhaustive=False,
        note='标题候选不是正文读取或工程归属确认。范围未明候选保留；零命中不证明全图不存在。')


def check_read(session, name, args):
    if name != 'read_assembly':
        return
    sid = args.get('snapshot_id') or session.request.snapshot_id
    for item in session.task.data.get('source_candidates', {}).values():
        if (item['snapshot_id'] == sid and item['region_id'] == args.get('region_id')
                and item['heading_start'] == (args.get('heading_start') or 0)):
            from cad_agent.analysis.general import INDEX_VERSION
            from cad_agent.cache import file_stamp
            current = (INDEX_VERSION, sid, *(file_stamp(session.w.store.artifact(sid, n)) for n in ('regions.json', 'scene.json')))
            # JSON storage turns nested stamp tuples into lists.
            import json
            if json.dumps(current) != json.dumps(item['signature']):
                raise ValueError('QUERY_CANDIDATE_STALE: reload the snapshot in a new run after source changes')


def record_read(session, name, args, result):
    if name != 'read_assembly' or not result.get('observation_id'):
        return
    sid = args.get('snapshot_id') or session.request.snapshot_id
    updates = []
    for item in session.task.data.get('source_candidates', {}).values():
        if (item['snapshot_id'] != sid or item['region_id'] != args.get('region_id')
                or item['heading_start'] != (args.get('heading_start') or 0)):
            continue
        pages = item.setdefault('read_pages', {})
        pages[str(result.get('offset', 0))] = dict(observation_id=result['observation_id'],
            returned=len(result.get('items', [])), has_more=result.get('has_more'),
            next_offset=result.get('next_offset'), total=result.get('total'),
            truncated=any(i.get('truncated') or i.get('source_region_partial') for i in result.get('items', [])))
        item['body_read'] = True
        updates.append(dict(candidate_id=item['candidate_id'], target_id=item['target_id'],
            scope_state=item['scope_state'], read_pages=pages,
            source_binding_state=(result.get('source_binding') or {}).get('state', 'unresolved'),
            note='读取状态不等于范围或工程适用性确认；范围结论须引用scope角色证据。'))
    if updates:
        result['query_matches'] = updates


def claim_issues(session, claim):
    """Check explicit query selections without treating a lexical hit as proof."""
    intent = session.task.data.get('query_intent')
    if not intent or claim.state == 'unknown':
        return []
    issues = []
    targets = [t for t in intent['intent']['targets'] if t['requirement_id'] in claim.question_part_ids]
    selected = getattr(claim, 'query_target_ids', [])
    if selected:
        if not set(selected) <= {t['target_id'] for t in targets}:
            return ['QUERY_TARGET_UNKNOWN：结论引用了不属于该要求的目标。']
        targets = [t for t in targets if t['target_id'] in selected]
    elif len(targets)>1:
        return ['QUERY_TARGET_REQUIRED：同一要求包含多个对象，请用query_target_ids说明本条归属。']
    for target in targets:
        identifier = (target.get('identifier') or {}).get('value', '')
        quotes = [session.evidence[e]['quote'] for e in claim.evidence_ids if e in session.evidence]
        if identifier and not any(title_match(q, target['subject']['value'], identifier) for q in quotes):
            issues.append('QUERY_IDENTITY_EVIDENCE：编号目标结论须引用该对象的标题/编号原文，不能只引用材料层。')
        if target['conditions']:
            scopes = [session.evidence[e] for e in claim.evidence_roles.get('scope', []) if e in session.evidence]
            titles = [session.evidence[e] for e in claim.evidence_ids if e in session.evidence
                      and (not identifier or title_match(session.evidence[e]['quote'], target['subject']['value'], identifier))]
            for condition in target['conditions']:
                # This verifies source locality, not the model's semantic inference.
                # Unknown/contradictory scope must remain unknown in the answer.
                if condition['kind'] != 'scope':
                    continue
                local = [e for e in scopes if any(e.get('snapshot_id') == title.get('snapshot_id')
                         and e.get('frame_id') and e.get('frame_id') == title.get('frame_id') for title in titles)]
                if not local:
                    issues.append('QUERY_SCOPE_EVIDENCE：受范围约束的目标须引用同图框scope角色来源；未核实标unknown。')
                term = normalize(condition['value'])
                positive = [e for e in local if term in normalize(e['quote']) and not re.search(
                    r'(?:非|不属于|除|不含|不包括|不包含)' + re.escape(term), normalize(e['quote']))]
                if condition['operator'] == 'exclude' and positive and claim.state not in ('conflict', 'unknown'):
                    issues.append('QUERY_SCOPE_EXCLUDED：scope原文命中用户排除范围，不能作为满足条件的observed/derived结果。')
                if condition['operator'] == 'include' and local and not positive and claim.state != 'conflict':
                    issues.append('QUERY_SCOPE_UNRESOLVED：scope原文尚未直接支持指定范围；保留unknown，不能靠同图框位置确认。')
    return issues


def delivery_issues(session, parsed):
    record = session.task.data.get('query_intent')
    if not record:
        return []
    errors = []
    if record['intent']['unresolved_requirements'] and any(o.status == 'fulfilled' for o in parsed.requirement_outcomes):
        errors.append('QUERY_REQUIREMENTS_UNRESOLVED：问题仍有未解释条件，不能声明fulfilled。')
    targets = record['intent']['targets']
    for requirement in {t['requirement_id'] for t in targets}:
        expected = {t['target_id'] for t in targets if t['requirement_id'] == requirement}
        claims = [c for c in parsed.claims if requirement in c.question_part_ids]
        addressed = {tid for c in claims for tid in c.query_target_ids}
        if len(expected)>1 and expected-addressed:
            errors.append('QUERY_TARGET_COVERAGE：以下目标未分别回答或说明缺口：'+','.join(sorted(expected-addressed)))
        if any(o.requirement_id == requirement and o.status == 'fulfilled' for o in parsed.requirement_outcomes):
            candidates = [c for c in session.task.data.get('source_candidates', {}).values() if c['target_id'] in expected]
            for candidate in candidates:
                pages = candidate.get('read_pages', {})
                if candidate.get('body_read') and not pages_complete(pages):
                    errors.append('QUERY_PAGES_INCOMPLETE：候选正文仍有未读分页或截断，不能声明fulfilled。')
            for target in (t for t in targets if t['target_id'] in expected):
                target_claims = [c for c in claims if c.state != 'unknown' and (not c.query_target_ids or target['target_id'] in c.query_target_ids)]
                evidence = [session.evidence[eid] for c in target_claims for eid in c.evidence_ids if eid in session.evidence]
                cited_regions = {(e.get('snapshot_id'), e.get('region_id')) for e in evidence}
                selected = [c for c in candidates if c['target_id'] == target['target_id'] and (c['snapshot_id'],c['region_id']) in cited_regions]
                for condition in target['conditions']:
                    if condition['kind'] != 'content':
                        continue
                    term = normalize(condition['value'])
                    if condition['operator'] == 'include' and not any(term in normalize(e['quote']) for e in evidence):
                        errors.append('QUERY_CONTENT_EVIDENCE：内容筛选条件缺少引用原文，不能声明fulfilled。')
                    if condition['operator'] == 'exclude':
                        if not selected or any(not pages_complete(c.get('read_pages', {})) for c in selected):
                            errors.append('QUERY_EXCLUSION_UNVERIFIED：内容排除须读完所选记录，不能用未读或零命中证明不存在。')
                        for candidate in selected:
                            for page in candidate.get('read_pages', {}).values():
                                observation = session.observations.get(page['observation_id'], {})
                                if any(term in normalize(i.get('evidence', {}).get('quote', '')) for i in observation.get('data', {}).get('items', [])):
                                    errors.append('QUERY_EXCLUSION_CONFLICT：所选记录出现被排除内容；保留冲突并标partial/unresolved。')
            if record['intent']['action'] == 'enumerate':
                for target_id in expected:
                    searches = [s for s in session.task.data.get('query_searches', []) if s['target_id'] == target_id]
                    if any(s['has_more'] for s in searches) and not any(not s['has_more'] for s in searches):
                        errors.append('QUERY_SEARCH_INCOMPLETE：标题候选尚未翻页完成。')
    return errors


def pages_complete(pages):
    offset, seen = 0, set()
    while str(offset) in pages and offset not in seen:
        seen.add(offset)
        page = pages[str(offset)]
        if page.get('truncated'):
            return False
        if not page['has_more']:
            return offset + page['returned'] == page.get('total')
        offset = page.get('next_offset')
    return False
