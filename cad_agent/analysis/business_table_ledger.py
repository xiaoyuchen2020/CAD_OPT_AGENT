"""Aggregate only actual same-session pages; cache hits confer no unseen coverage."""
from copy import deepcopy
from cad_agent.analysis.business_tables import _completion, _conflicts


def attach_read_coverage(session, name, result):
    if result.get('page_state') != 'complete': return result
    keys=('snapshot_id','source_sha256','version','kind','table_id','header_rows','requested_fields','row_count')
    pages=[deepcopy(o['data']) for o in session.observations.values() if o['tool']==name and
           all(o['data'].get(k)==result.get(k) for k in keys) and o['data'].get('page_state')=='complete']
    pages.append(result)
    ranges=sorted((p['row_offset'],p['row_end']) for p in pages)
    intervals=[]
    for lo,hi in ranges:
        if hi<=lo: continue
        if intervals and lo<=intervals[-1][1]: intervals[-1][1]=max(intervals[-1][1],hi)
        else: intervals.append([lo,hi])
    full=intervals==[[0,result['row_count']]]
    records={r['record_id']:r for p in pages for r in p.get('records',[])}
    if result['kind']=='planning_metrics':
        fields={f['record_id']:f for p in pages for f in p.get('fields',[])}
        _conflicts(list(fields.values()))
        fields=list(fields.values())
    else:
        fields=[f for r in records.values() for f in r['fields']]
    status=_completion(fields,result['requested_fields'])
    if records: status['fields_complete']=all(r['fields_complete'] for r in records.values())
    unresolved={r['row_index'] for p in pages for r in p.get('unresolved_rows',[])}
    status['fields_complete']=bool(fields) and full and not unresolved and status['fields_complete']
    result['read_coverage']=dict(intervals=intervals, table_read_complete=full,
        coverage_scope='same_session_same_table_and_headers',unresolved_row_indexes=sorted(unresolved),
        conflicting_cells=sorted({f['cell_id'] for f in fields if f['state']=='conflict'}),**status)
    return result
