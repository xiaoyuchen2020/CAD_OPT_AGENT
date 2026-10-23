"""Deterministic source predicate execution; a receipt never proves global recall."""
from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.analysis.source_record_query import matches


class SelectSourceRecords(Record):
    requirement_ids: list[str] = Field(min_length=1,max_length=8)


def select(session,args):
    packet=getattr(session,'source_record_packet',None)
    if not packet:
        raise ValueError('先读取本轮完整来源记录。')
    parts={p['id']:p for p in packet['query']['parts']}
    if len(set(args.requirement_ids))!=len(args.requirement_ids) or not set(args.requirement_ids)<=parts.keys():
        raise ValueError('筛选要求不属于当前问题。')
    selections=[]
    for qid in args.requirement_ids:
        selected,excluded=[],[]
        for record in packet['records']:
            if qid not in record['task_ids'] or record['reference_only']:
                continue
            if record['page_state']!='complete':
                raise ValueError('不得基于未读完的记录判定排除。')
            hit=matches(record,session.evidence,parts[qid])
            if hit != (qid in record['selected_parts']):
                raise ValueError('原文筛选状态改变，须重新核查。')
            (selected if hit else excluded).append(record['record_id'])
        selections.append(dict(requirement_id=qid,filters=parts[qid]['filters'],selected=selected,excluded=excluded))
    return dict(status='selected',selections=selections,scope=packet['scope'],
        source_record_ids=[r['record_id'] for r in packet['records']],
        operation_receipts=[dict(operation='selection',fulfilled=True,basis='source_literal_predicates',scope='read_source_records')])
