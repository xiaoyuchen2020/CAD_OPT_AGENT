"""Frozen, resumable all-file business extraction audit; no model or repairs.

Every candidate/page is exercised. Exact source verification is not a semantic
accuracy score: source titles and unbound fields create review items, not gold.
"""
import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path('/Users/vigorchen/Documents/Work/规则及数据/建筑/专项测试图纸/建筑设计说明')
OUT=Path(os.environ.get('BUSINESS_CORPUS_OUT',str(ROOT/'artifacts/business-tables-corpus-20261026'))).resolve()
sys.path.insert(0,str(ROOT))
from cad_agent.config import Settings
BASE=Settings.load()


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)


def prepare():
    OUT.mkdir(exist_ok=True)
    manifest=[]
    copied={Path(r['destination']).name:r for r in read(SOURCE/'复制清单.json')['copied']}
    for i,path in enumerate(sorted(SOURCE.rglob('*.dwg')),1):
        digest=sha(path)
        assert copied[path.name]['sha256']==digest,('source_changed',path.name)
        manifest.append(dict(case_id=f'{i:02d}-{digest[:8]}',name=path.name,path=str(path),
                             sha256=digest,bytes=path.stat().st_size))
    assert len(manifest)==len(copied)
    if (OUT/'manifest.json').exists():assert read(OUT/'manifest.json')==manifest
    else:save(OUT/'manifest.json',manifest)
    frozen=OUT/'product'
    if not frozen.exists():
        shutil.copytree(ROOT/'cad_agent',frozen/'cad_agent',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        save(OUT/'code-manifest.json',{str(p.relative_to(frozen)):sha(p) for p in frozen.rglob('*') if p.is_file()})
    save(OUT/'scope.json',dict(files=len(manifest),methods=['extract_planning_metrics','extract_building_schedule'],
        test_type='full native conversion/import/discovery/pagination/source-binding audit',
        real_model_runs=False,semantic_gold_available=False,source_modified=False,
        note='No candidate is not proof of absence. A source-verification pass is not field accuracy acceptance.'))
    return manifest


def worker(case,local_only=False):
    folder=OUT/'cases'/case['case_id'];folder.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();result=dict(case=case,issues=[],methods={},status='running')
    def stage(name,**kw):
        save(folder/'progress.json',dict(stage=name,elapsed_seconds=round(time.monotonic()-started,2),**kw))
    def issue(code,level='review',**kw):result['issues'].append(dict(code=code,level=level,**kw))
    for key in list(sys.modules):
        if key=='cad_agent' or key.startswith('cad_agent.'):del sys.modules[key]
    sys.path.insert(0,str(OUT/'product'));os.environ['PYTHONPATH']=str(OUT/'product')
    from cad_agent.service import Workspace
    from cad_agent.adapters.oda import ConvertedDrawing
    from cad_agent.analysis.general import DrawingIndex, intersects
    from cad_agent.analysis.business_tables import extract_planning_metrics,extract_building_schedule,field_key,METRICS,BUILDING,TITLE
    from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest,BuildingScheduleRequest
    from cad_agent.analysis.source_record_store import verify_sources
    w=Workspace(replace(BASE,data_dir=folder/'data'))
    if local_only:
        def no_upload(filename,content):raise RuntimeError('LOCAL_ONLY_NO_VERIFIED_CONVERSION')
        w.converter.convert=no_upload
    try:
        stage('import')
        path=Path(case['path']);assert sha(path)==case['sha256']
        cache=read(OUT/'local-cache-candidates.json').get(case['sha256']) if (OUT/'local-cache-candidates.json').exists() else None
        if cache:
            prior=cache['drawing'];cf=Path(cache['database']).parent/'drawings'/prior['id']
            expected={'source.dwg':case['sha256'],'source.dxf':prior['sha256'],'oda.json':prior['conversion']['json_sha256']}
            if all((cf/k).is_file() and sha(cf/k)==v for k,v in expected.items()):
                def cached_convert(filename,content):
                    assert hashlib.sha256(content).hexdigest()==case['sha256']
                    return ConvertedDrawing((cf/'source.dxf').read_bytes(),(cf/'oda.json').read_bytes(),prior['conversion'])
                w.converter.convert=cached_convert
                result['conversion_route']='hash_verified_cached_conversion_reparsed'
            else:result['conversion_route']='fresh_lan_conversion_cache_invalid'
        else:result['conversion_route']='fresh_lan_conversion'
        replay=os.environ.get('BUSINESS_CORPUS_REPLAY')
        if replay:
            prior_folder=Path(replay)/'cases'/case['case_id']
            prior_result=read(prior_folder/'result.json')
            if not prior_result.get('snapshot_id'):raise RuntimeError('REPLAY_NO_SUCCESSFUL_IMPORT')
            w.close()
            shutil.copy2(prior_folder/'data/index.sqlite3',folder/'data/index.sqlite3')
            drawings=folder/'data/drawings'
            drawings.mkdir(exist_ok=True)
            for source_folder in (prior_folder/'data/drawings').iterdir():
                target=drawings/source_folder.name
                if not target.exists():target.symlink_to(source_folder.resolve(),target_is_directory=True)
            w=Workspace(replace(BASE,data_dir=folder/'data'))
            drawing=prior_result['drawing'];result['conversion_route']='development_native_scene_replay'
        else:
            drawing=w.ingest(case['name'],path.read_bytes())
        sid=drawing['id']
        result.update(drawing=drawing,snapshot_id=sid)
        save(folder/'import.json',dict(drawing=drawing,conversion_route=result['conversion_route']))
        if drawing.get('diagnostics'):issue('IMPORT_DIAGNOSTICS',details=drawing['diagnostics'])
        if not drawing.get('index_coverage',{}).get('traversal_complete',True):issue('SOURCE_TRAVERSAL_INCOMPLETE')
        stage('index',snapshot_id=sid)
        index=DrawingIndex(w,sid);tables=index.tables
        save(folder/'table-index.json',tables)
        result['table_count']=len(tables)
        signals=[]
        for t in index.scene['texts']:
            text=t['text'];compact=re.sub(r'\s+','',text)
            kinds=[kind for kind,pattern in TITLE.items() if len(compact)<=90 and pattern.search(compact)]
            keys=sorted({field_key([text],METRICS),field_key([text],BUILDING)}-{None})
            if kinds or keys:
                signals.append({k:t.get(k) for k in ('entity_id','text','layout_id','bbox','origin','height','entity_path')}|
                               dict(title_kinds=kinds,field_keys=keys))
        save(folder/'source-signals.json',signals)
        # Index-native text/line geometry is independent of the business output.
        save(folder/'scene-summary.json',dict(text_count=len(index.scene['texts']),line_count=len(index.scene['lines']),
             parse_coverage=index.scene.get('parse_coverage'),signal_count=len(signals)))
        source_fields=defaultdict(list)
        for kind,method,schema in [('planning_metrics',extract_planning_metrics,PlanningMetricsRequest),
                                  ('building_schedule',extract_building_schedule,BuildingScheduleRequest)]:
            stage('discover',method=kind)
            mr=dict(candidates=0,pages=0,field_count=0,record_count=0,summary_count=0,
                    field_states={},tables=[],status='completed');result['methods'][kind]=mr
            candidates=[];discovery=[];args=schema(limit=12)
            try:
                while True:
                    d=method(w,sid,args,index);verify_sources(d,index);discovery.append(d);candidates+=d['candidates']
                    if not d['has_more']:break
                    assert d['next_offset']>args.offset
                    args=schema(**d['next_actions'][0]['arguments'])
                assert len(candidates)==d['total']
                save(folder/kind/'discovery.json',discovery);mr['candidates']=len(candidates)
                if not candidates:mr['status']='no_candidate'
                for signal in signals:
                    if kind not in signal['title_kinds']:continue
                    covered=[c['table_id'] for c in candidates if c['source_scope']['layout_id']==signal['layout_id'] and
                             intersects(c['source_scope']['bbox'],signal['bbox'])]
                    if not covered:issue('TITLE_OUTSIDE_DISCOVERED_TABLE',method=kind,source=signal,
                                         note='Caption may sit outside its table or be a reference; inspect native layout before confirming a miss.')
                states=Counter()
                for c in candidates:
                    tid=c['table_id'];stage('extract',method=kind,table_id=tid)
                    pages=[];args=schema(table_id=tid);tm=dict(table_id=tid,scope=c['source_scope'],pages=0,
                            field_count=0,record_count=0,summary_count=0,status='completed')
                    mr['tables'].append(tm)
                    try:
                        while True:
                            page=method(w,sid,args,index);verify_sources(page,index);pages.append(page)
                            if page.get('page_state')=='unread':
                                tm['status']='unresolved';issue('CANDIDATE_UNRESOLVED',method=kind,table_id=tid,details=page['diagnostics']);break
                            assert page['row_offset']==args.row_offset
                            assert page['row_end']>args.row_offset
                            if not page.get('has_more'):
                                assert page['row_end']==page['row_count'];break
                            assert page['next_offset']==page['row_end']
                            args=schema(**page['next_actions'][0]['arguments'])
                            if len(pages)>1000:raise RuntimeError('PAGINATION_SAFETY_LIMIT')
                        save(folder/kind/(tid+'.json'),pages)
                        fields=[f for p in pages for f in p.get('fields',[])]+[f for p in pages for r in p.get('records',[]) for f in r['fields']]
                        tm.update(pages=len(pages),field_count=len(fields),record_count=sum(len(p.get('records',[])) for p in pages),
                                  summary_count=sum(len(p.get('summary_rows',[])) for p in pages),
                                  field_states=dict(Counter(f['state'] for f in fields)),
                                  source_verified=True,pagination_complete=tm['status']=='completed',
                                  missing_fields=sorted({f for p in pages for f in p.get('missing_fields',[])}))
                        for f in fields:
                            if f['state'] in ('ambiguous','conflict'):
                                issue('FIELD_'+f['state'].upper(),method=kind,table_id=tid,field=f)
                            ids=tuple(sorted(e['evidence_id'] for e in f.get('evidence',[])))
                            if ids:source_fields[(kind,f['field'],ids,f.get('value'))].append(tid)
                        if not fields and tm['status']=='completed':issue('CANDIDATE_ZERO_FIELDS',method=kind,table_id=tid)
                        for key in ('pages','field_count','record_count','summary_count'):mr[key]+=tm[key]
                        states.update(tm['field_states'])
                    except Exception as e:
                        tm.update(status='error',error_type=type(e).__name__,error=str(e)[:1500])
                        save(folder/kind/(tid+'-error.json'),tm)
                        if pages:save(folder/kind/(tid+'-partial.json'),pages)
                        issue('EXTRACTION_EXCEPTION','confirmed',method=kind,table_id=tid,error=tm['error'])
                mr['field_states']=dict(states)
            except Exception as e:
                mr.update(status='error',error_type=type(e).__name__,error=str(e)[:1500])
                issue('DISCOVERY_EXCEPTION','confirmed',method=kind,error=mr['error'])
        for (kind,field,ids,value),tids in source_fields.items():
            if len(set(tids))>1:issue('SOURCE_REPEATED_ACROSS_TABLES',method=kind,field=field,value=value,evidence_ids=ids,table_ids=sorted(set(tids)))
        result['status']='completed'
    except Exception as e:
        result.update(status='error',error_type=type(e).__name__,error=str(e)[:1500],error_code=getattr(e,'code',None))
        issue('PIPELINE_EXCEPTION','confirmed',stage=read(folder/'progress.json')['stage'],error=result['error'],error_code=result['error_code'])
        (folder/'traceback.txt').write_text(traceback.format_exc())
    finally:
        result['elapsed_seconds']=round(time.monotonic()-started,2)
        save(folder/'result.json',result);stage('finished',status=result['status'])
        w.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--worker');parser.add_argument('--case-timeout',type=int,default=1200)
    parser.add_argument('--local-only',action='store_true')
    parser.add_argument('--pending-conversions',action='store_true')
    args=parser.parse_args();manifest=prepare()
    if args.worker:return worker(next(c for c in manifest if c['case_id']==args.worker),args.local_only)
    if args.local_only:
        verified={c['case_id'] for c in read(OUT/'conversion-plan.json')['verified_local_cache']}
        manifest=[c for c in manifest if c['case_id'] in verified]
    elif args.pending_conversions:
        pending={c['case_id'] for c in read(OUT/'conversion-plan.json')['needs_conversion']}
        manifest=[c for c in manifest if c['case_id'] in pending]
    for case in manifest:
        folder=OUT/'cases'/case['case_id'];folder.mkdir(parents=True,exist_ok=True)
        if (folder/'result.json').exists():continue
        print('START',case['case_id'],case['name'],flush=True)
        with (folder/'worker.log').open('w') as log:
            command=[sys.executable,str(Path(__file__).resolve()),'--worker',case['case_id']]
            if args.local_only:command.append('--local-only')
            p=subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True)
            try:p.wait(timeout=args.case_timeout)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
        if not (folder/'result.json').exists():
            progress=read(folder/'progress.json') if (folder/'progress.json').exists() else {}
            save(folder/'result.json',dict(case=case,status='worker_failed',methods={},issues=[dict(code='WORKER_FAILED',level='confirmed',exit_code=p.returncode,progress=progress)]))
        r=read(folder/'result.json')
        print('DONE',case['case_id'],r['status'],{k:(v['candidates'],v['field_count'],v['record_count']) for k,v in r['methods'].items()},'issues',len(r['issues']),flush=True)
        save(OUT/'progress.json',dict(completed=sum((OUT/'cases'/c['case_id']/'result.json').exists() for c in manifest),total=len(manifest),last=case['case_id']))
    print('ALL FILES ATTEMPTED',len(manifest),flush=True)


if __name__=='__main__':main()
