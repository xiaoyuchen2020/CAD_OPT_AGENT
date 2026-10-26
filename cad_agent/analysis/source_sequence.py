"""Requested numbered clauses, navigated through known parser chapters.

This is read coverage, never proof of engineering applicability or delivery.
No drawing vocabulary or query synonyms are inferred.
"""
import re
from cad_agent.cad.numbers import ordinals
from cad_agent.cad.regions import is_heading, clause_level
from cad_agent.contracts import stable_id


def requests(question):
    result = []
    for m in re.finditer(r'第\s*(\d+)\s*[—–－\-~～至到]\s*(\d+)\s*条', question):
        lo, hi = map(int, m.groups())
        if not 0 < lo <= hi or hi-lo >= 24:
            continue  # Do not invent a truncated reading obligation.
        prefix = question[:m.start()]
        hint = re.search(r'([^\s，。；：、》]{2,40})$', prefix)
        hint = hint[1] if hint else ''
        key = (hint, lo, hi)
        if not any((r['chapter_hint'], r['first'], r['last']) == key for r in result):
            result.append(dict(request_id=stable_id('sequence', *map(str, key)),
                chapter_hint=hint, first=lo, last=hi, quote=m[0], question_span=list(m.span())))
    return result


def _chapter_rows(index, chapter):
    return [r for r in index.scope(section_id=chapter.region_id)
            if (r.layout_id, r.frame_id, r.column_key) ==
               (chapter.layout_id, chapter.frame_id, chapter.column_key)]


def _clauses(index, chapter):
    rows = _chapter_rows(index, chapter)
    # Source parser order is retained; nested numbering cannot replace a parent.
    tokens = {r.region_id: ordinals(r.text) for r in rows if r != chapter}
    levels = [clause_level(r.text[t['text_span'][0]:]) for r in rows
              for t in tokens.get(r.region_id, [])]
    level = min((n for n in levels if n is not None), default=1)
    numbered_parents = {r.region_id for r in rows if tokens.get(r.region_id)}
    current = None
    clauses = {}
    for r in rows:
        if r == chapter:
            continue
        nested = any(p in numbered_parents for p in r.context_region_ids if p != chapter.region_id)
        markers = [] if nested else [t for t in tokens.get(r.region_id, [])
            if clause_level(r.text[t['text_span'][0]:]) in (level, None)]
        boundaries = [(t['text_span'][0], t['value']) for t in markers]
        if not boundaries or boundaries[0][0] > 0:
            boundaries.insert(0, (0, None))
        for i, (start, number) in enumerate(boundaries):
            end = boundaries[i+1][0] if i+1 < len(boundaries) else len(r.text)
            if number is not None:
                current = dict(number=int(number), spans=[])
                clauses.setdefault(int(number), []).append(current)
            if current is not None and start < end:
                current['spans'].append(dict(region_id=r.region_id, text_span=[start, end]))
    return clauses


def _unread(span, ledger):
    a, b = span['text_span']
    for lo, hi in sorted(ledger.get('read_spans', {}).get(span['region_id'], [])):
        if lo <= a < hi:
            a = hi
    return a if a < b else None


def refresh(session):
    data = session.task.data
    specs = requests(data.get('question', ''))
    if not specs:
        return
    ledger = data.get('source_recall', {})
    if not ledger:
        return
    index = session.index
    # Only chapters already encountered in successful search/read results are
    # navigation candidates. A unique lexical hint is not a source-read receipt.
    known = set(ledger.get('read_spans', {}))
    for q in ledger.get('queries', {}).values():
        for c in q.get('candidates', {}).values():
            known.update(c.get('region_ids', []))
            known.update(p['region_id'] for p in c.get('source_context_path', []))
    for record in ledger.get('source_records', {}).values():
        known.update(p['region_id'] for p in record.get('source_context_path', []))
    chapters = [index.by_id[rid] for rid in known if rid in index.by_id
                and is_heading(index.by_id[rid].text)]
    records = []
    for spec in specs:
        hint = spec['chapter_hint']
        words = {q['arguments'].get('query', '') for q in ledger.get('queries', {}).values()}
        anchors = [c for c in chapters if (hint and hint in c.text) or any(
            len(w) >= 2 and w in hint and w in c.text for w in words)]
        # A subsection repeating the chapter's subject is not a competing
        # chapter (for example a numbered clause ending in “设计要求”).
        if anchors:
            depth = min(clause_level(c.text) or 0 for c in anchors)
            anchors = [c for c in anchors if (clause_level(c.text) or 0) == depth]
        owners = [s['id'] for s in data['subtasks'] if spec['quote'] in s['requirement']
                  and (not hint or hint in s['requirement'])]
        record = dict(spec, task_ids=owners, chapter_candidates=[c.region_id for c in anchors],
            state='chapter_unresolved' if len(anchors) != 1 else 'chapter_candidate', items=[])
        if len(anchors) == 1:
            chapter = anchors[0]
            record.update(chapter_id=chapter.region_id, chapter_title=chapter.text,
                layout_id=chapter.layout_id, frame_id=chapter.frame_id,
                binding_basis='known_parser_chapter_navigation', attribution_state='unverified')
            clauses = _clauses(index, chapter)
            for number in range(spec['first'], spec['last']+1):
                matches = clauses.get(number, [])
                item = dict(number=number, state='not_located' if not matches else 'ambiguous' if len(matches)>1 else 'unread')
                if len(matches) == 1:
                    spans = matches[0]['spans']
                    missing = [(sp, _unread(sp, ledger)) for sp in spans]
                    item['source_spans'] = spans
                    item['next_reads'] = [dict(region_id=sp['region_id'], start=start, neighbors=0)
                                          for sp, start in missing if start is not None]
                    read = any(start is None or start > sp['text_span'][0] for sp, start in missing)
                    item['state'] = 'fully_read' if not item['next_reads'] else 'partially_read' if read else 'unread'
                record['items'].append(item)
            record['state'] = 'fully_read' if all(i['state']=='fully_read' for i in record['items']) else 'incomplete'
        records.append(record)
    data['source_sequences'] = dict(snapshot_id=index.snapshot, source_sha256=index.drawing['sha256'], records=records)


def pending(data, active):
    ledger = data.get('source_sequences', {})
    if (ledger.get('snapshot_id'), ledger.get('source_sha256')) != (data.get('snapshot_id'), data.get('source_sha256')):
        return []
    calls = []
    for record in ledger.get('records', []):
        ids = sorted(set(record['task_ids']) & active)
        if not ids:
            continue
        for item in record['items']:
            for args in item.get('next_reads', [])[:1]:
                calls.append(dict(operation='source_recall_next', tool='read_source_context', arguments=args,
                    task_ids=ids, blocking=True, priority=0, coverage_gaps=[],
                    reason='requested_clause_unread', clause_number=item['number'], chapter_id=record['chapter_id']))
    return calls[:12]


def summary(data):
    return [{k:v for k,v in r.items() if k != 'items'} | {'items':[
        {k:v for k,v in i.items() if k not in ('source_spans', 'next_reads')} for i in r['items']],
        'note':'这是指定章节条文的读取覆盖，未确认事实归属或完成交付。'}
        for r in data.get('source_sequences', {}).get('records', [])]
