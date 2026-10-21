"""Bounded, task-owned lexical reading across clauses in the selected drawing.

Object and property need not occur in one paragraph. Ranked text remains a
candidate: reading a general clause never proves its applicability to an object.
"""
import re
from pydantic import Field
from cad_agent.contracts import Record, stable_id
from cad_agent.analysis.retrieval import lexical_rank, source_context_path


class RequirementSources(Record):
    requirement_id: str
    layout_id: str = ''
    frame_id: str = ''
    section_id: str = ''
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4, ge=1, le=6)


def read(session, args):
    step = next((s for s in session.task.data['subtasks'] if s['id'] == args.requirement_id), None)
    if step is None:
        raise ValueError('requirement_id 不属于当前问题。')
    trace = getattr(session, 'trace', [])
    owners = trace[-1].get('task_ids', []) if trace else []
    if owners and args.requirement_id not in owners:
        raise ValueError('原文读取须关联该 requirement_id 的 task_ids。')
    from cad_agent.analysis.typed_requirements import accepts_source_prerequisites
    if not accepts_source_prerequisites(step):
        raise ValueError('用户条件或交付格式不能作为原图检索目标。')
    index = session.index
    regions = index.scope(args.layout_id, args.frame_id, args.section_id)
    # Rank sentences so a short, relevant condition is not diluted by the long
    # standards preamble in its paragraph. Normalization is ranking-only.
    chunks = []
    for region in regions:
        for sentence in re.finditer(r'[^。；]+[。；]?', region.text):
            for offset in range(0, len(sentence[0]), 480):
                start = sentence.start()+offset
                text = re.sub(r'\s+', '', sentence[0][offset:offset+600])
                if text:
                    chunks.append(dict(chunk_id=f'{region.region_id}:{start}',
                        region_id=region.region_id, start=start, text=text, index_text=text))
    literal_objects = list(dict.fromkeys(c['object'] for c in step.get('coverage', [])
        if c.get('requirement_type') == 'SOURCE_FACT' and c.get('object')
        and c.get('state') != 'addressed'))
    queries = focused_queries(step, session.task.data) + literal_objects + [step['requirement']]
    queries += [c['object']+' '+c['field'] for c in step.get('coverage', [])
                if c.get('requirement_type') == 'SOURCE_FACT']
    focused = focused_queries(step, session.task.data)
    rankings = [rank_condition_chunks(chunks, q) if q in focused else
                [i for i, chunk in enumerate(chunks) if re.sub(r'\s+', '', q) in chunk['text']]
                if q in literal_objects else [i for i, _ in lexical_rank(chunks, q)]
                for q in dict.fromkeys(queries)]
    ranked = []
    starts = {}
    # Diversify explicit targets instead of letting a repeated object crowd out
    # the other objects/conditions requested in this same requirement.
    for position in range(max(map(len, rankings), default=0)):
        for ranking in rankings:
            if position < len(ranking):
                chunk = chunks[ranking[position]]
                if chunk['region_id'] not in starts:
                    ranked.append(chunk['region_id'])
                    starts[chunk['region_id']] = chunk['start']
    items = []
    actions = []
    for rid in ranked[args.offset:args.offset+args.limit]:
        region = index.by_id[rid]
        start = max(0, starts[rid]-100) if starts[rid] >= 1800 else 0
        end = min(len(region.text), start+1800)
        evidence = index.evidence(region, start, end)
        items.append(dict(region_id=rid, evidence=evidence,
                          source_context_path=source_context_path(index, region),
                          relationship='requirement_lexical_candidate', attribution_state='unverified'))
        if start:
            actions.append(dict(tool='read_source_context',
                                arguments=dict(region_id=rid, start=0, neighbors=0)))
        if len(region.text) > end:
            actions.append(dict(tool='read_source_context',
                                arguments=dict(region_id=rid, start=end, neighbors=0)))
        if region.text.rstrip().endswith(('：', ':')):
            from cad_agent.analysis.evidence_ledger import numbered_continuations
            peers = [r for r in regions if (r.layout_id, r.frame_id, r.column_key)
                     == (region.layout_id, region.frame_id, region.column_key)]
            actions.extend(dict(tool='read_source_context',
                arguments=dict(region_id=r.region_id, neighbors=0))
                for r in numbered_continuations(peers, region))
    more = args.offset+args.limit < len(ranked)
    if more:
        actions.append(dict(tool='read_requirement_sources',
                            arguments=args.model_dump() | {'offset': args.offset+args.limit}))
    return dict(requirement_id=step['id'], snapshot_id=index.snapshot,
                query_id=stable_id('requirement-read', index.snapshot, step['requirement'],
                                   args.layout_id, args.frame_id, args.section_id),
                scope=dict(layout_id=args.layout_id, frame_id=args.frame_id, section_id=args.section_id),
                offset=args.offset, total=len(ranked), has_more=more, items=items,
                next_actions=actions, enumeration_complete=not more and args.offset == 0,
                note='按当前问题分项跨条款补读；只读取当前所选图纸。排名及同文件不证明同对象或适用范围；原文缺值、未读分页、外部转引分别保留。')


def pending(data):
    """One optional first page per unfinished source requirement, no quota reset."""
    from cad_agent.analysis.typed_requirements import accepts_source_prerequisites
    from cad_agent.analysis.scheduling import execution_state
    reads = data.get('requirement_source_reads', {})
    if not data.get('source_recall', {}).get('read_spans'):
        return []
    return [dict(operation='source_recall_next', tool='read_requirement_sources',
                 arguments=dict(requirement_id=s['id'], limit=6), task_ids=[s['id']],
                 blocking=False, priority=2 if focused_queries(s, data) else 4, coverage_gaps=[],
                 reason='cross_clause_requirement_read')
            for s in data['subtasks']
            if s['status'] in ('pending', 'running') and execution_state(data, s['id'])['executable']
            and accepts_source_prerequisites(s)
            and 'SOURCE_FACT' in s.get('requirement_types', [])
            and any(c.get('state') != 'addressed' for c in s.get('coverage', []))
            and s['id'] not in reads]


def focused_queries(step, data):
    """Literal subject/field queries for cross-clause conditions, not row joins.

    Table titles and short isolated field labels must not crowd full clauses
    out of a first page. Condition wording is a retrieval aid, not acceptance.
    """
    text = step['requirement']
    if not re.search(r'适用条件|上翻|防水等级|层数', text):
        return []
    objects = list(dict.fromkeys(c['object'] for s in data.get('subtasks', [])
                   for c in s.get('coverage', []) if c.get('requirement_type') == 'SOURCE_FACT'
                   and c.get('object') and c['object'] in data.get('question', '')
                   and c['object'] not in ('地域', '等级', '防水等级', '层数')))
    # The question itself supplies the topical qualifier. No source identity
    # or applicability is inferred from sharing these words.
    topic = '防水' if '防水' in data.get('question', '') else ''
    fields = [field for field in ('层数', '上翻', '适用条件', '防水等级') if field in text]
    return [obj + ' ' + topic + ' ' + field for field in fields for obj in objects[:2]][:4]


def required_reviews(data):
    """Reserve at most one first page for each explicit condition requirement."""
    # Allow the normal initial object reads before crossing general clauses.
    if len(data.get('source_recall', {}).get('read_spans', {})) < 4:
        return []
    return [call | dict(blocking=True, reason='unread_explicit_condition_requirement')
            for call in pending(data) if focused_queries(
                next(s for s in data['subtasks'] if s['id'] == call['task_ids'][0]), data)]


def rank_condition_chunks(chunks, query):
    obj, topic, field = query.rsplit(' ', 2)
    subjects = [part for part in re.split(r'[、，及和]', obj) if len(part) >= 2]
    patterns = {'层数': r'[一二三四五六两双多\d]+道|双层|各设置|层数',
                '上翻': r'上翻|墙面防水层高度|楼板底|顶板',
                '防水等级': r'等级', '适用条件': r'当|时|有|采用|适用'}
    ranked = [i for i, _ in lexical_rank(chunks, query)]
    # This is query ranking only. Requiring both the explicit subject and its
    # topic prevents isolated table labels from outranking a full clause.
    return [i for i in ranked if len(chunks[i]['text']) > 25
            and any(subject in chunks[i]['text'] for subject in subjects)
            and (not topic or topic in chunks[i]['text'])
            and re.search(patterns[field], chunks[i]['text'])]
