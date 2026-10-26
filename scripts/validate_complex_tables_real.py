"""Isolated source + subscription validation; no production record mutations."""
import argparse
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.test_source_record_workflow import seed,save
from cad_agent.service import Workspace
from cad_agent.config import Settings
from cad_agent.tables.service import TableService
from cad_agent.tables.contracts import ReadTableContent
from cad_agent.tables.reading import read


def audit_fire(document):
    """Gold transcribed from the original crop before this implementation."""
    expected=[('1','533','2.20','2','60'),('2','86','1.10','1','25'),
              ('3','3291','2.20','2','60'),('4','684.6','2.20','2','60'),
              ('5','76','1.10','1','25'),('6','3896.84','3.30','3','60')]
    rows=[]
    for ordinal,area,width,exits,distance in expected:
        group=next(n for n in document['nodes'] if n['kind']=='group' and n['title'].startswith('防火分区'+ordinal+'（'))
        fields=[f for f in document['fields'] if group['node_id'] in [n['node_id'] for n in f['lineage']]]
        checks={label:any(f['label']==label and f['value']==value and f['binding_state']=='source_bound' for f in fields)
                for label,value in [('防火分区面积',area),('设计疏散宽度',width),('出入口个数',exits),('最远一点至疏散门的直线距离均',distance)]}
        rows.append(dict(group=group['title'],checks=checks))
    sub=next(n for n in document['nodes'] if n['kind']=='subtable')
    cells=[c for c in document['cells'] if c['cell_id'] in sub['cell_ids']]
    nested=[]
    for ordinal,area in [('1','933'),('2','830')]:
        a=next(c for c in cells if c['text']==ordinal)
        b=next(c for c in cells if area in [f['raw_text'] for f in c['fragments']])
        nested.append(dict(ordinal=ordinal,area=area,source_row_match=a['row']==b['row'],separate_cells=a['cell_id']!=b['cell_id'],
            area_exact_source=any(e['quote']==area for f in b['fragments'] for e in f['evidence'])))
    basic={label:any(f['label']==label and f['value']==value and f['binding_state']=='source_bound' for f in document['fields'])
           for label,value in [('地下','2'),('埋深','8.90'),('共设','6')]}
    basic['full_smoke_partition_clause']=any('达到任何一防烟分区面积小于2000平米' in c['text'] for c in document['cells'])
    passed=all(all(r['checks'].values()) for r in rows) and all(all(v for k,v in r.items() if k not in ('ordinal','area')) for r in nested) and all(basic.values())
    return dict(passed=passed,manual_reference='original_native_crop_20261026_before_implementation',groups=rows,nested=nested,basic=basic,
                source_inventory=len(document['source_inventory']),unassigned=document['unassigned_entity_ids'],diagnostics=document['diagnostics'])


def audit_reference(document,reference):
    """Independently reviewed business-table source IDs, not parser expectations."""
    entity_cells={f['entity_id']:c for c in document['cells'] for f in c['fragments']}
    checks=[]
    if 'rows' in reference:
        for row in reference['rows']:
            matched={key:next((entity_cells[r['entity_id']] for r in refs if r['entity_id'] in entity_cells),None)
                     for key,refs in row['source_refs'].items()}
            rows={c['row'] for c in matched.values() if c}
            checks.append(dict(ordinal=row['values']['ordinal'],present=all(matched.values()),same_row=len(rows)==1,
                separate_cells=len({c['cell_id'] for c in matched.values() if c})==len(matched)))
    else:
        for f in reference['fields']:
            source_ids={r['entity_id'] for r in f['source_refs']}
            matched=[field for field in document['fields'] if source_ids.intersection(field['source_entity_ids'])]
            checks.append(dict(label=f['label'],source_present=bool(source_ids&entity_cells.keys()),
                value_and_unit=any(v['value']==f['value'] and v['unit']==f['unit'] and v['binding_state']=='source_bound' for v in matched),
                qualifiers=[n['text'] for v in matched for n in v.get('notes',[])]))
    return dict(passed=all(all(v for k,v in c.items() if k not in ('ordinal','label','qualifiers')) for c in checks),checks=checks,
                reference_method=reference['method'])


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source-data',type=Path,required=True)
    ap.add_argument('--snapshot',required=True);ap.add_argument('--title',required=True)
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--account-data',type=Path)
    ap.add_argument('--live',action='store_true');ap.add_argument('--fire-gold',action='store_true')
    ap.add_argument('--reference',type=Path)
    ap.add_argument('--publish',action='store_true',help='Use the real background chat publication path')
    args=ap.parse_args()
    if args.live and not args.account_data:ap.error('--live needs --account-data')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    w=Workspace(Settings(data_dir=out/'data',provider='responses_subscription'))
    start=time.monotonic()
    try:
        save(out,'inputs.json',seed(args.source_data,w,args.snapshot))
        svc=TableService(w,args.snapshot);data=svc.documents()
        save(out,'discovery.json',data)
        docs=[d for d in data['documents'] if args.title in d['title']]
        if len(docs)!=1:raise ValueError(f'Target not unique: {len(docs)}')
        doc=docs[0];pages=[];cursor='';plan=''
        while True:
            p=read(svc,ReadTableContent(table_id=doc['table_id'],limit=8,cursor=cursor,read_plan_id=plan),'real-audit')
            pages.append(p);plan=p['read_plan_id'];cursor=p['next_cursor']
            if not cursor:break
        save(out,'pages.json',pages)
        gold=audit_fire(doc) if args.fire_gold else audit_reference(doc,json.loads(args.reference.read_text())) if args.reference else None
        if gold:save(out,'gold-audit.json',gold)
        summary=dict(source_regression_passed=not doc['diagnostics'] and pages[-1]['coverage']['table_read_complete'] and (gold is None or gold['passed']),
            table_title=doc['title'],cells=len(doc['cells']),coverage=pages[-1]['coverage'],model_e2e='not_run')
        if args.live:
            from cad_agent.adapters.siwc import SiwcAccount
            from cad_agent.analysis.session import CadAgentSession
            from cad_agent.contracts import ChatRequest
            from cad_agent.runtimes.registry import run_agent
            w.siwc_account.close();w.siwc_account=SiwcAccount(args.account_data)
            if not w.siwc_account.status()['connected']:raise ValueError('Subscription disconnected')
            request=ChatRequest(snapshot_id=args.snapshot,question='这张图中的'+doc['title']+'内有哪些内容',mode='agent',
                runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False,source_read_batching=False)
            session=None
            try:
                if args.publish:
                    run=w.start_chat(request)
                    while run['status'] in ('queued','running'):
                        time.sleep(.25);run=w.store.get('chat_run',run['id'])
                    save(out,'chat-run.json',run)
                    if run['status']!='completed':raise ValueError('Published chat failed: '+str(run.get('error')))
                    result=run['result'];usage=run['usage'];summary['chat_run_id']=run['id']
                else:
                    session=CadAgentSession(w,request);session.journal.bind('complex_table_real_'+out.name)
                    result=run_agent(session);usage=session.usage
                save(out,'result.json',result)
                (out/'answer.md').write_text(result['answer'])
                td=result.get('table_documents',{})
                required={f['entity_id'] for c in doc['cells'] for f in c['fragments']}
                delivered={f['entity_id'] for c in td.get('items',[]) for f in c['fragments']}
                summary.update(model_e2e='passed' if td.get('coverage',{}).get('table_read_complete') and required==delivered and td.get('model_requests',0)>0 and result['task']['status']=='completed' else 'failed',
                    model_requests=td.get('model_requests',0),delivered_sources=len(delivered),required_sources=len(required),
                    task_status=result['task']['status'],fallback_used=result['fallback_used'],usage=usage)
            finally:
                if session:
                    for name,value in [('trace',session.trace),('events',session.journal.snapshot()),('task',session.task.data),('runtime',session.runtime_diagnostics)]:save(out,name+'.json',value)
        summary['elapsed_seconds']=round(time.monotonic()-start,3)
        save(out,'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
        if not summary['source_regression_passed'] or summary['model_e2e']=='failed':raise SystemExit(1)
    finally:w.close()

if __name__=='__main__':main()
