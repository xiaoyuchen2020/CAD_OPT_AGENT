"""Conservative source lookup grammar; unknown constraints retain the Agent route."""
import re
from cad_agent.cad.assembly_headings import FAMILY, HEADING, CODE
from cad_agent.cad.source_vocabulary import expand, canonical, identities, FACET, normalized_code

VERSION = 'source-record-query-v2'
COMPLEX = re.compile(r'计算|合计|总厚|总计|求和|面积|体积|净宽|距离|几何|合规|规范|合理|优化|适不适|适用|能否|是否满足|减薄|替换|建议|比较.*(?:大于|小于)|超过|不超过|不少于|大于|小于|至少|至多')
LITERAL_FILTER = re.compile(r'(包含|含有|含|不含|排除|标题为|编号为|图框为)[“"「]([^”"」]{1,100})[”"」]')
ALLOWED = re.compile(r'帮我|帮忙|麻烦|请|再|同时|另外|并且|并|分别|逐项|这张图纸|这张图|当前图纸|当前图|本图|选定图纸|这些图纸|跨图纸|跨图|所有图纸|所有|全部|各个|各类|各|的|中|有哪些|有什么|是什么|哪些|多少种|找一下|找下|找出|找(?!平层)|查一下|查下|查找|查询|搜索|列出|列|提取|读取|摘录|整理|汇总|展示|看看|查看|建筑做法表|工程做法表|装修做法表|材料做法表|做法表|建筑做法|工程做法|装修做法|材料做法|做法|构造|层次|原文|备注|编号|材料|单位|及|和|与|以及|只要|只保留|仅|保留|筛选|比较|对比|差异|按|图框|分组|表格|呈现|回答|结果|并列|对照|上次|刚才|上述|那些|它们|继续|一下|一遍')


def _families(text):
    return sorted({f for m in FAMILY.finditer(LITERAL_FILTER.sub('', text)) for f in expand(m[0])})


def route(session, history=None):
    question = session.request.question
    plan = dict(version=VERSION, route='agent', reason='', question=question, parts=[],
                scope='selected_drawings' if session.request.related_drawings else 'current_drawing',
                snapshot_ids=[session.request.snapshot_id]+[d.snapshot_id for d in session.request.related_drawings],
                all_results=True, action='extract')
    if not session.w.settings.source_record_workflow or session.request.source_record_mode == 'agent':
        plan['reason']='disabled_or_explicit_agent'; return plan
    if session.request.resume_task_id or session.request.selected_practice_id:
        plan['reason']='existing_task_or_selected_fact'; return plan
    if re.search('当前图纸|当前图|本图|这张图',question):
        plan.update(scope='current_drawing',snapshot_ids=[session.request.snapshot_id])
    from cad_agent.analysis.business_table_routing import business_route
    business = business_route(plan, session)
    if business is not None:
        return business
    previous = None
    if session.request.parent_run_id:
        try:
            prior = session.w.store.get('chat_run', session.request.parent_run_id)
            previous = prior.get('source_records', {}).get('query') or prior.get('result', {}).get('source_records', {}).get('query')
        except KeyError:
            pass
    if COMPLEX.search(LITERAL_FILTER.sub('', question)):
        plan['reason']='geometry_arithmetic_or_engineering_judgment'; return plan
    all_families = _families(question)
    if not all_families and previous and len(previous['parts'])==1 and set(previous['snapshot_ids']) == set(plan['snapshot_ids']):
        all_families = sorted({f for p in previous['parts'] for f in p['families']})
        plan['inherited_from']=session.request.parent_run_id
    elif (history or session.request.parent_run_id) and not all_families:
        plan['reason']='ambiguous_followup'; return plan
    for part in session.task.parts:
        text = part['text']
        grammar = LITERAL_FILTER.sub('', text).replace('作法', '做法')
        filters = [dict(operator=m[1], text=m[2]) for m in LITERAL_FILTER.finditer(text)]
        if plan.get('inherited_from'):
            filters = [f for p in previous['parts'] for f in p['filters']]+filters
        families = _families(grammar) or all_families
        # Explicit family codes are handled as exact title predicates, not decimals.
        code_pattern = re.compile('(?:'+FAMILY.pattern+r')[ \t]*[—–－-]?[ \t]*'+CODE)
        title_codes = [normalized_code(m[0]) for m in code_pattern.finditer(grammar)]
        if plan.get('inherited_from'):
            title_codes=previous['parts'][0]['title_codes']
        # Remove only codes attached to a family; unattached quantities remain
        # unparsed constraints and therefore cannot silently disappear.
        remainder = code_pattern.sub('', grammar)
        remainder = FAMILY.sub('', remainder)
        remainder = ALLOWED.sub('', remainder)
        facets = list(FACET.finditer(remainder)) if not families else []
        if facets:
            filters += [dict(operator='含', text=m[0]) for m in facets]
            remainder = FACET.sub('', remainder)
        # A local code is an exact identity predicate, never a global family alias.
        local = remainder.strip()
        if not families and re.fullmatch(r'(?:[A-Za-zＡ-Ｚａ-ｚ]{1,4}|[\u4e00-\u9fff]{1,3})[0-9０-９]+', local):
            title_codes = [normalized_code(local)]
            families = ['*']
            remainder = ''
        remainder = re.sub(r'[\s，,。？?！!；;：:、（）()\-—]+', '', remainder)
        if remainder or (not families and not filters):
            plan['reason']='unparsed_constraint:'+remainder[:80]; return plan
        if not families:
            # A literal material filter explicitly asks for construction records.
            if not re.search('做法|作法|构造', question):
                plan['reason']='missing_source_family'; return plan
            families=['*']
        plan['parts'].append(dict(id=part['id'], text=text, families=families,
            filters=filters, title_codes=title_codes, action='compare' if re.search('比较|对比|差异', text) else 'filter' if filters else 'extract'))
    if not plan['parts']:
        plan['reason']='no_source_requirement'; return plan
    plan.update(route='source_records', reason='explicit_source_lookup',
                action='compare' if any(p['action']=='compare' for p in plan['parts']) else 'extract')
    return plan


def matches(record, evidence, part):
    """Only lexical source predicates; no semantic or numeric inference."""
    title = record.get('display_title') or evidence[record['title_id']]['quote']
    body = '\n'.join(evidence[e]['quote'] for e in record['body_ids'])
    # Identity comes from explicit code/name columns or the title line, never
    # a material layer or applicability cell that merely mentions the family.
    identity = record.get('identity_text', title.splitlines()[0])
    requested = {canonical(f) for f in part.get('families', [])}
    primary=HEADING.search(identity)
    actual=({canonical(primary[1])}|set(record.get('context_families',[]))) if primary else identities(identity)|set(record.get('identity_families',[]))
    if requested and '*' not in requested and not actual.intersection(requested):
        return False
    found = HEADING.search(identity)
    codes = {normalized_code(found[1]+found['code'])} if found else set()
    codes.update(normalized_code(t) for t in identity.splitlines() if len(t.strip()) < 40)
    if part['title_codes'] and not codes.intersection(map(normalized_code, part['title_codes'])):
        return False
    for f in part['filters']:
        op, value = f['operator'], f['text']
        if op == '图框为':
            hit = value == record['source_scope'].get('frame_name')
        elif op in ('标题为', '编号为'):
            heading=HEADING.search(title)
            hit = value == title.strip() if op == '标题为' else (normalized_code(value) in codes or bool(heading and normalized_code(value) == normalized_code(heading['code'])))
        else:
            hit = value in title+'\n'+body
        if hit == (op in ('不含','排除')):
            return False
    return True
