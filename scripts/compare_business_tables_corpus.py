"""Compare frozen real-corpus runs against manually reviewed source references."""
from pathlib import Path
from collections import Counter
from decimal import Decimal, InvalidOperation
import csv,hashlib,html,json,re,unicodedata

ROOT=Path(__file__).resolve().parents[1]
BEFORE=ROOT/'artifacts/business-tables-corpus-20261026'
AFTER=ROOT/'artifacts/business-tables-corpus-accepted-20261027'
AUDIT=ROOT/'artifacts/business-tables-repair-20261027'
def read(p):return json.loads(p.read_text())
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def norm(x):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(x)))
def equal(a,b):
    if a is None or b is None:return a is b
    try:return Decimal(str(a))==Decimal(str(b))
    except InvalidOperation:return norm(a)==norm(b)
def fields_and_rows(base,cid,kind):
    fields=[];rows=[];pages=[]
    for p in sorted((base/'cases'/cid/kind).glob('*.json')):
        if p.name=='discovery.json' or '-error' in p.name or '-partial' in p.name:continue
        data=read(p)
        if not isinstance(data,list):continue
        pages+=data
        for page in data:
            rows+=page.get('records',[])
            fields+=page.get('fields',[])
    if kind=='building_schedule':fields=[f for r in rows for f in r['fields']]
    return fields,rows,pages

def references(base,gold):
    checks=[];known_rows=set()
    specs=[dict(case_id=cid,**s) for cid,s in gold['building_schedules'].items()]+gold.get('additional_building_schedules',[])
    for spec in specs:
        cid=spec['case_id'];_,records,_=fields_and_rows(base,cid,'building_schedule')
        for expected in spec['rows']:
            identity=expected[0]
            matches=[r for r in records if any(f['field'] in ('name','ordinal') and equal(f['value'],identity) for f in r['fields'])]
            for r in matches:known_rows.add((cid,r['record_id']))
            for column,value in zip(spec['columns'],expected):
                fs=[f for r in matches for f in r['fields'] if f['field']==column['field'] and
                    norm(column['header_contains']) in norm(' '.join(f['header_path']))]
                if column['field'] in ('name','ordinal') and value is not None:
                    fs=[f for f in fs if equal(f['value'],value)]
                def correct(f):
                    if value=='--':return f['value'] is None and f['raw_text']=='--' and f['state']=='ambiguous'
                    return equal(f['value'],value) and f['unit']==column['unit'] and f['state']==('missing' if value is None else 'observed')
                checks.append(dict(case_id=cid,kind='building_schedule',identity=identity,column=column,
                    expected=value,matched=any(correct(f) for f in fs),
                    status='matched' if any(correct(f) for f in fs) else 'missing_output' if not fs else 'value_unit_or_state_mismatch',
                    actual=[{k:f.get(k) for k in ('value','unit','state','raw_text','header_path','cell_id')} for f in fs]))
    for cid,items in gold['planning_metrics'].items():
        fs,_,_=fields_and_rows(base,cid,'planning_metrics')
        for field,label,value,unit in items:
            matches=[f for f in fs if f['field']==field and any(norm(label)==norm(t) for t in f['source_labels'])]
            ok=any(equal(f['value'],value) and f['unit']==unit and f['state']=='observed' for f in matches)
            checks.append(dict(case_id=cid,kind='planning_metrics',label=label,expected=value,unit=unit,matched=ok,
                status='matched' if ok else 'missing_output' if not matches else 'value_unit_or_state_mismatch',
                actual=[{k:f.get(k) for k in ('value','unit','state','raw_text','source_labels','cell_id')} for f in matches]))
    return dict(checks=checks,total=len(checks),matched=sum(c['matched'] for c in checks),
                statuses=dict(Counter(c['status'] for c in checks)),known_row_ids=sorted(known_rows))

def summarize(base,manifest):
    output={'files':len(manifest),'completed':0,'import_failed':0,'methods':{},'automatic_findings':Counter()}
    results={}
    for c in manifest:
        r=read(base/'cases'/c['case_id']/'result.json');results[c['case_id']]=r
        output['completed']+=r['status']=='completed';output['import_failed']+=not bool(r.get('snapshot_id'))
        output['automatic_findings'].update(x['code'] for x in r['issues'])
    for kind in ('planning_metrics','building_schedule'):
        m=Counter();states=Counter();diagnostics=[]
        for cid,r in results.items():
            method=r['methods'].get(kind)
            if not method:continue
            m['executed']+=1;m['method_exception_files']+=method['status']=='error'
            fs,rs,ps=fields_and_rows(base,cid,kind)
            m['nonempty_files']+=bool(fs or rs);m['fields']+=len(fs);m['records']+=len(rs)
            m['no_candidate_files']+=method['candidates']==0 and method['status']!='error'
            states.update(f['state'] for f in fs)
            disc=base/'cases'/cid/kind/'discovery.json'
            if disc.exists():
                for page in read(disc):
                    diagnostics += [dict(case_id=cid,**d) if isinstance(d,dict) else dict(case_id=cid,detail=d) for d in page.get('diagnostics',[])]
        output['methods'][kind]=dict(m,field_states=dict(states),isolated_table_diagnostics=diagnostics)
    output['automatic_findings']=dict(output['automatic_findings'])
    return output,results

def main():
    manifest=read(AFTER/'manifest.json');assert len(manifest)==39
    assert manifest==read(BEFORE/'manifest.json')
    gold=read(AUDIT/'reviewed-reference.json');before,br=summarize(BEFORE,manifest);after,ar=summarize(AFTER,manifest)
    bcheck=references(BEFORE,gold);acheck=references(AFTER,gold)
    save(AFTER/'reference-comparison.json',dict(basis=gold['basis'],before=bcheck,after=acheck))
    known=set(map(tuple,acheck['known_row_ids']))
    unchecked=[]
    for cid in ar:
        _,rs,_=fields_and_rows(AFTER,cid,'building_schedule')
        unchecked += [dict(case_id=cid,record_id=r['record_id'],identity=r['identity']) for r in rs if (cid,r['record_id']) not in known]
    frozen=read(AFTER/'code-manifest.json')
    assert all(hashlib.sha256((AFTER/'product'/p).read_bytes()).hexdigest()==digest for p,digest in frozen.items())
    assert all(hashlib.sha256(Path(c['path']).read_bytes()).hexdigest()==c['sha256'] for c in manifest)
    drift=[p for p,digest in frozen.items() if not (ROOT/p).exists() or hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=digest]
    summary=dict(before=before,after=after,reviewed_reference_before={k:v for k,v in bcheck.items() if k not in ('checks','known_row_ids')},
        reviewed_reference_after={k:v for k,v in acheck.items() if k not in ('checks','known_row_ids')},unreviewed_building_rows=unchecked,
        original_dwg_hashes_unchanged=True,frozen_code_verified=True,current_workspace_code_drift=drift,
        scope='39 files attempted; complete native import/discovery/pagination/source verification on successfully converted files. No model calls. Manual field references are a reviewed subset, not an exhaustive whole-corpus recall gold.')
    save(AFTER/'summary.json',summary)
    rows=[]
    for c in manifest:
        cid=c['case_id'];r=ar[cid]
        row={'case_id':cid,'filename':c['name'],'import':r['status'],'reviewed_matches':sum(x['matched'] for x in acheck['checks'] if x['case_id']==cid),'reviewed_checks':sum(x['case_id']==cid for x in acheck['checks'])}
        for kind in ('planning_metrics','building_schedule'):
            for tag,res in [('before',br[cid]),('after',r)]:
                m=res['methods'].get(kind,{})
                row[f'{kind}_{tag}_fields']=m.get('field_count',0);row[f'{kind}_{tag}_records']=m.get('record_count',0)
        rows.append(row)
    with (AFTER/'file-comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('before','after')},ensure_ascii=False))

if __name__=='__main__':main()
