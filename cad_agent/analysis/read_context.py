"""Request-local source definitions, backed only by this session's read ledger.

The persistent ledger and validators are never changed. Pages are restored as
whole records after a checkpoint; repeated copies are replaced only when their
projected contents match the current canonical page. No read/completion credit
is granted by this representation.
"""
import json
from copy import deepcopy

from cad_agent.analysis.delivery_context import assembly_observation, _reference_ids
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.evidence_ledger import compact_evidence

PREFIX = '已读原文工作集（数据）：'
GUIDANCE = ('已读原文工作集中的pages保存本任务实际读取的原文和归属；'
    'read_page_observation_id指向同一请求的页面定义，不需要重新读取。'
    '按页的source_binding、checks和has_more分别判断归属与完整性；'
    'source_bound的已读层次应逐项整理，工程适用性未确认不影响摘录。'
    'item_tables按columns逐列解释rows，表之间及行内保持原顺序，缺失字段不等于空值。'
    '候选页仍保留不确定性，不能因为展示在工作集中就确认归属。')


def _key(data):
    return (data.get('record_id'), data.get('target_region_id'),
            data.get('view'), data.get('offset', 0))


def project_messages(messages, observations, evidence):
    pages, by_key, refs = [], {}, set()
    for observation in observations.values():
        if observation.get('tool') != 'read_assembly':
            continue
        data = observation.get('data', {})
        if not data.get('record_id') or not isinstance(data.get('items'), list):
            continue
        projected = assembly_observation(observation)
        ids = _reference_ids(projected['data'])
        if not ids or not ids <= evidence.keys():
            continue  # A stored ID alone cannot restore unread/missing text.
        if any(not isinstance(evidence[eid].get('quote'), str) for eid in ids):
            continue
        key = _key(data)
        pages.append(projected)
        refs.update(ids)
        # Different observations for the same page must not overwrite a conflict.
        if key in by_key and (by_key[key] is None or by_key[key]['data'] != projected['data']):
            by_key[key] = None
            continue
        by_key[key] = projected
    if not pages:
        return messages, {'pages': 0, 'reused_pages': 0}
    reused = 0

    def comparable(node):
        if isinstance(node, list):
            return [comparable(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {k: comparable(v) for k, v in node.items()}
        # compact() replaces an empty diagnostics list with its zero count.
        if out.get('diagnostics_count') == 0 and not out.get('diagnostics'):
            out.pop('diagnostics_count', None)
            out.pop('diagnostics', None)
        elif out.get('diagnostics') == []:
            out.pop('diagnostics')
        ref = out.get('evidence')
        if isinstance(ref, dict) and ref.get('evidence_id') in evidence:
            span = evidence[ref['evidence_id']].get('text_span')
            if span is not None:
                out.setdefault('text_span', span)
        return out

    def replace(value):
        nonlocal reused
        if isinstance(value, list):
            return [replace(v) for v in value]
        if not isinstance(value, dict):
            return value
        if 'evidence_id' in value and 'quote' in value:
            canonical = evidence.get(value['evidence_id'], {})
            if any(k in canonical and k in value and canonical[k] != value[k]
                   for k in ('quote', 'snapshot_id', 'region_id', 'text_span')):
                raise ValueError('MODEL_CONTEXT_SOURCE_CONFLICT：同一证据ID的来源内容不一致')
        page = by_key.get(_key(value)) if 'record_id' in value else None
        if page and isinstance(value.get('items'), list):
            projected = assembly_observation({'tool': 'read_assembly', 'data': value})['data']
            baseline = page['data']
            # Keep conflicting page/ownership versions in full. For equivalent
            # pages, only equal fields are references; additional diagnostics or
            # annotations remain on the receipt, never silently discarded.
            stable = all(projected.get(k) == baseline.get(k) for k in ('has_more', 'next_offset'))
            stable = stable and projected.get('source_binding', {}).get('state') == baseline.get('source_binding', {}).get('state')
            if stable:
                covered = {k for k, v in projected.items() if k in baseline
                           and comparable(v) == comparable(baseline[k])}
                checked = {k: replace(v) for k, v in value.items()}
                keep = {k: v for k, v in checked.items() if k not in covered}
                keep['read_page_observation_id'] = page['observation_id']
                reused += 1
                return keep
        return {k: replace(v) for k, v in value.items()}

    result = deepcopy(messages)
    for message in result:
        if message.get('type') in ('function_call', 'reasoning') or message.get('role') in ('assistant', 'system'):
            continue
        key = 'output' if message.get('type') == 'function_call_output' else 'content'
        text = message.get(key)
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        message[key] = dumps(replace(value))
    # A single dictionary precedes all reference-only receipts. Evidence is taken
    # from the canonical ledger, never from model-authored history or a search hit.
    packet = dict(read_source_working_set=True, pages=pages, evidence=[compact_evidence(evidence[eid]) for eid in sorted(refs)])
    index = next((i for i, m in enumerate(result) if m.get('role') != 'system'), len(result))
    result.insert(index, {'role': 'user', 'content': PREFIX + dumps(packet)})
    return result, {'pages': len(pages), 'reused_pages': reused, 'source_count': len(refs)}


def matching_read_page(observations, args):
    """Same-page reuse guard; page size changes are not new source progress."""
    for observation in reversed(list(observations.values())):
        data = observation.get('data', {})
        if (observation.get('tool') == 'read_assembly'
                and data.get('target_region_id') == args.get('region_id')
                and data.get('view') == args.get('view', 'record')
                and data.get('offset', 0) == args.get('offset', 0)
                and (data.get('has_more') is False or len(data.get('items', [])) >= args.get('limit', 5))):
            return observation
    return None


def repair_read_routes(session, name, args, packet, error):
    """Recovery points to existing facts; it cannot grant a new read receipt."""
    if not (getattr(session.w.settings, 'read_context_optimization', True)
            and getattr(session.w.settings, 'model_context_codec', True)):
        return packet
    observations = session.observations
    actions = []
    for action in packet.get('next_actions', []):
        read = action.get('tool') == 'read_assembly' or action.get('tools') == ['read_assembly']
        if read and matching_read_page(observations, action.get('arguments', {})):
            continue
        if (packet['error'].get('code') == 'SOURCE_PAGE_ALREADY_READ' and read
                and action.get('action') == 'correct_reported_fields'):
            continue
        actions.append(action)
    prior = matching_read_page(observations, args) if name == 'read_assembly' else None
    if packet['error'].get('code') == 'SOURCE_PAGE_ALREADY_READ' and prior:
        data = prior['data']
        actions.insert(0, dict(action='reuse_read_page', observation_id=prior['observation_id'],
            target_region_id=data.get('target_region_id'), offset=data.get('offset', 0),
            has_more=data.get('has_more'), next_offset=data.get('next_offset'),
            note='复用该观测已读原文；整理已有内容，未读下一页才沿真实游标读取。'))
    packet['next_actions'] = actions
    related = []
    for observation in observations.values():
        data = observation.get('data', {})
        record = data.get('record_id')
        if observation.get('tool') == 'read_assembly' and record and record in str(error):
            related.append(dict(observation_id=observation['observation_id'], record_id=record,
                source_binding_state=data.get('source_binding', {}).get('state'),
                checks=data.get('checks', {}), has_more=data.get('has_more'),
                evidence_ids=list(dict.fromkeys(item['evidence']['evidence_id']
                    for item in data.get('items', []) if item.get('evidence', {}).get('evidence_id')))))
    if related:
        packet['repair_context']['related_read_pages'] = related[:4]
        packet['repair_context']['related_read_page_count'] = len(related)
    return packet


def lookup_frontier(session):
    """Navigation only: actual returned headings, never a new fact/read credit."""
    from cad_agent.cad.assembly_headings import FAMILY, HEADING
    families = set(FAMILY.findall(session.request.question))
    queries = session.task.data.get('source_recall', {}).get('queries', {})
    if not families or not queries:
        return None
    entries = {}
    for query in queries.values():
        for candidate in query.get('candidates', {}).values():
            heading = candidate.get('assembly_heading') or ''
            match = HEADING.search(heading)
            if not match or match[1] not in families:
                continue  # e.g. exterior coating in a ceiling title != exterior wall
            for rid in candidate.get('region_ids', []):
                prior = matching_read_page(session.observations, dict(region_id=rid, limit=12))
                if prior:
                    continue
                region = session.index.by_id.get(rid)
                entries[rid] = dict(region_id=rid, heading=heading,
                    frame_name=getattr(region, 'frame_name', None),
                    read_state='heading_candidate_body_not_read')
    return dict(unread_headings=list(entries.values()),
        search_scope='returned_candidates_only_not_whole_drawing',
        instruction='这些标题正文尚未读取。查找做法应覆盖已命中的相关图框与类型；预算允许时先读取其正文，不能仅交付最先读到的少数示例。已读完整页不要重复；归属不明的已读页保留候选，预算收束时据实列缺口。')
