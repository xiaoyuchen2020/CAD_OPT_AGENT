"""Persisted, signature-bound reading manifests with union-of-delivered coverage."""
import json
from uuid import uuid4
from cad_agent.contracts import stable_id


def read(service, args, owner):
    d=service.get(args.table_id);store=service.w.store
    selection=dict(table_id=d['table_id'],node_id=args.node_id,fields=sorted(set(args.fields)))
    selected={c['cell_id'] for c in d['cells']}
    if args.node_id:
        nodes={n['node_id']:n for n in d['nodes']}
        if args.node_id not in nodes:raise ValueError('TABLE_NODE_NOT_FOUND')
        pending=[args.node_id];selected=set()
        while pending:
            node=nodes[pending.pop()];selected.update(node['cell_ids']);pending.extend(node['children'])
    if args.fields:
        found=[f for f in d['fields'] if f['field_id'] in args.fields or f['label'] in args.fields]
        matched={v for v in args.fields if any(v in (f['field_id'],f['label']) for f in found)}
        if matched!=set(args.fields):raise ValueError('TABLE_FIELD_NOT_FOUND：不能把未命中字段当作空表。')
        selected&={f['cell_id'] for f in found}
    ids=[c['cell_id'] for c in d['cells'] if c['cell_id'] in selected]
    if not ids:raise ValueError('TABLE_SELECTION_EMPTY')
    if args.read_plan_id:
        plan=store.get('table_read_plan',args.read_plan_id)
        if plan['owner_id']!=owner or plan['snapshot_id']!=service.snapshot or plan['selection']!=selection:
            raise ValueError('TABLE_READ_SCOPE_CHANGED')
        if plan['signature']!=service.signature:raise ValueError('TABLE_SOURCE_CHANGED')
    else:
        if args.cursor:raise ValueError('TABLE_CURSOR_REQUIRES_PLAN')
        plan=dict(id='tread_'+uuid4().hex,owner_id=owner,snapshot_id=service.snapshot,signature=service.signature,
                  selection=selection,cell_ids=ids,delivered=[],cursors={})
        store.put('table_read_plan',plan['id'],plan)
    offset=0
    if args.cursor:
        if args.cursor not in plan['cursors']:raise ValueError('TABLE_CURSOR_INVALID')
        offset=plan['cursors'][args.cursor]
    by_id={c['cell_id']:c for c in d['cells']};items=[];size=0
    for cid in ids[offset:offset+args.limit]:
        service.check();c=by_id[cid]
        ancestors=[];node=next(n for n in d['nodes'] if n['node_id']==cid)
        node_map={n['node_id']:n for n in d['nodes']}
        while node.get('parent_id'):
            node=node_map[node['parent_id']];ancestors.append({k:node[k] for k in ('node_id','kind','title')})
        item={k:c[k] for k in ('cell_id','row','column','row_span','column_span','text','binding_state','diagnostics')}
        item.update(lineage=list(reversed(ancestors)),fragments=[dict(entity_id=f['entity_id'],raw_text=f['raw_text'],display_text=f['display_text'],evidence=f['evidence']) for f in c['fragments']],
                    fields=[f for f in d['fields'] if f['cell_id']==cid],
                    evidence_url=f'/api/drawings/{service.snapshot}/table-documents/{d["table_id"]}/cells/{cid}')
        # Budget the actual model-facing representation. Full source parts are
        # registered separately by the session and must not force tiny pages.
        from cad_agent.analysis.efficiency import compact
        n=len(json.dumps(compact(item),ensure_ascii=False))
        if size+n>26000 and items:break
        if n>35000:raise ValueError('TABLE_CELL_PAYLOAD_LIMIT：单元格过大，需要局部来源读取。')
        size+=n;items.append(item)
    service.check()
    if service.signature!=__import__('cad_agent.tables.service',fromlist=['fingerprint']).fingerprint(service.index):
        raise ValueError('TABLE_SOURCE_CHANGED')
    end=offset+len(items);cursor=stable_id('tcursor',plan['id'],service.signature,str(end)) if end<len(ids) else None
    def update(current):
        current['delivered']=sorted(set(current['delivered'])|{i['cell_id'] for i in items})
        if cursor:current['cursors'][cursor]=end
        return current
    plan=store.update('table_read_plan',plan['id'],update)
    complete=set(ids)<=set(plan['delivered'])
    return dict(table_id=d['table_id'],snapshot_id=service.snapshot,title=d['title'],title_evidence=d['title_evidence'],
                read_plan_id=plan['id'],signature=service.signature,items=items,offset=offset,next_cursor=cursor,
                has_more=end<len(ids),coverage=dict(selected_cells=len(ids),read_cells=len(set(ids)&set(plan['delivered'])),
                    selection_complete=complete,whole_table_selected=len(ids)==len(d['cells']),
                    table_read_complete=complete and len(ids)==len(d['cells']) and not d['unassigned_entity_ids'] and d['structure_state']=='source_bound',scope_exhaustive=False),
                structure_state=d['structure_state'],binding_state=d['binding_state'],diagnostics=d['diagnostics'])
