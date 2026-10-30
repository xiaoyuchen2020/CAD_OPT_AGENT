import json
import sqlite3
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cad_agent.fleet.store import FleetStore
from cad_agent.exploration.store import ExplorationStore
from scripts.source_overview_fixture import FixtureWorkflow as WorkflowStore
from cad_agent.fleet.source_overview import SourceRepository, Projection, SnapshotReader, build_overview, readonly, records_from_archive
from cad_agent.fleet.source_overview_service import create_app, LiveReader
from scripts.source_overview_fixture import fixture_package as export_package
from cad_agent.fleet.source_overview_viewer import viewer_script


def evidence(eid='e1', **kwargs):
    return dict(evidence_id=eid,snapshot_id='s',region_id='r',quote='厚150mm',text_span=[0,6],
                entity_ids=['entity'],layout_id='Model',bbox=[1,2,20,10],**kwargs)


def read_record(e=None):
    e=e or evidence()
    payload=dict(snapshot_id='s',target_region_id='r',items=[dict(evidence=e,region_id='r',text=e['quote'],text_span=e['text_span'])],
        source_coverage_receipt={'read_evidence_ids':[e['evidence_id']]})
    content=json.dumps(payload,ensure_ascii=False)
    return dict(ref=dict(record_id='rec-'+e['evidence_id'],complete_record=True,sha256=sha256(content.encode()).hexdigest()),content=content,sources={'s':'h'})


@pytest.fixture
def setup(tmp_path):
    fleet=FleetStore(tmp_path);exp=ExplorationStore(tmp_path/'exploration');wf=WorkflowStore(tmp_path/'rule-workflow')
    with exp.connection(True) as db:db.execute('INSERT INTO exploration_runs VALUES(?,?,?,?,?)',('exp','p','key','hash','{}'))
    upload=fleet.add_upload('a.dxf',b'fixture')
    def add(*,state='completed',node='a',source=None,records=None,project='exp'):
        source=source or evidence();job=fleet.create_job(dict(upload_ids=[upload['id']],question='read',mode='facts',exploration_run_id=project))
        active=fleet.claim(node);assert active['id']==job['id']
        drawings=[dict(id='s',sha256='h',name='图纸',unit='mm')]
        result=dict(citations=[source],claims=[dict(text=source['quote'],state='observed',evidence_ids=[source['evidence_id']])],task={'status':'partial'})
        if state=='completed':fleet.finish(job['id'],node,active['attempt_id'],result=result,delivery_status='partial',artifacts=dict(drawings=drawings,run={'status':'completed'},evidence_records=records or [read_record(source)]))
        return job['id'],active['attempt_id']
    return fleet,wf,SourceRepository(tmp_path),add


def test_readonly_does_not_expire_leases_or_initialize_missing_schema(setup):
    fleet,wf,repo,add=setup;j,a=add(state='running')
    with fleet.connection(True) as db:db.execute('UPDATE jobs SET lease_until=0 WHERE id=?',(j,))
    view=build_overview(repo,j)
    assert view['active'] and view['execution_status']=='running'
    with readonly(fleet.path) as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0]=='running'
        with pytest.raises(sqlite3.OperationalError):db.execute("UPDATE jobs SET status='failed'")


def test_stage_separation_missing_locations_and_deduplication(setup):
    fleet,wf,repo,add=setup;j,a=add(records=[read_record(),read_record(evidence('unpublished'))])
    view=build_overview(repo,j)
    assert view['counts']['sources']==1  # two references, one physical source
    row=view['items'][0]
    assert set(row['stages'])=={'read','delivered'} and not row['selected']
    assert len(row['origins'])==2
    assert view['delivery_status']=='partial' and not view['exhaustive']
    p=Projection();archive=repo.archive(j,a);src=evidence();src.pop('bbox')
    p.add(src,archive,'read');v=p.finish()
    assert v['counts']['sources']==1 and v['counts']['locatable']==0


def test_only_verified_host_reads_not_search_or_model_payloads(setup):
    fleet,wf,repo,add=setup;record=read_record()
    payload=json.loads(record['content']);payload['messages']=[{'role':'assistant','content':'claim'}]
    record['content']=json.dumps(payload);record['ref']['sha256']=sha256(record['content'].encode()).hexdigest()
    j,a=add(records=[record]);archive=deepcopy(repo.archive(j,a));archive['result']={}
    assert records_from_archive(archive)[0]==[]
    archive['artifacts']['evidence_records'][0]['content']+='tampered'
    reads,geometry,warnings=records_from_archive(archive)
    assert not reads and warnings


def test_package_presence_is_not_analysis_selection(setup):
    fleet,wf,repo,add=setup;j,a=add();package=export_package(repo.archive(j,a),'p')
    wf.put_object('p','fact_package',package,package['package_id'])
    view=build_overview(repo,j);assert not view['items'][0]['selected']
    assert 'packaged' in view['items'][0]['stages']
    run=wf.create('p','rule',dict(package=package,rule=dict(rule_id='AR',library_id='lib',version='1')))
    view=build_overview(repo,j);assert view['items'][0]['selected']
    assert view['items'][0]['associations'][-1]['workflow_id']==run['run_id']


def test_project_and_cross_node_lineage_do_not_borrow_current_node(setup):
    fleet,wf,repo,add=setup;j,a=add(node='a');j2,a2=add(node='c',source=evidence('e2'))
    packages=[]
    for job,attempt in [(j,a),(j2,a2)]:
        p=export_package(repo.archive(job,attempt),'p');wf.put_object('p','fact_package',p,p['package_id']);packages.append(p)
    task=build_overview(repo,j)
    assert {o['node_id'] for r in task['items'] for o in r['origins']}=={'a'}
    project=build_overview(repo,j,project_scope=True)
    assert project['counts']['sources']==1
    assert {o['node_id'] for r in project['items'] for o in r['origins']}=={'a','c'}


def test_archive_attempt_identity_and_path_fences(setup,tmp_path):
    fleet,wf,repo,add=setup;j,a=add()
    with pytest.raises(KeyError):build_overview(repo,j,attempt_id='unrelated')
    with fleet.connection(True) as db:db.execute('UPDATE attempts SET archive_path=? WHERE id=?',('../outside.json',a))
    with pytest.raises(ValueError,match='越界'):repo.archive(j,a)


def test_live_response_is_discarded_after_reassignment(setup):
    fleet,wf,repo,add=setup;j,a=add(state='running')
    def live(job,attempt):
        with fleet.connection(True) as db:db.execute('UPDATE jobs SET attempt_id=? WHERE id=?',('reassigned',j))
        return dict(job_id=j,attempt_id=a,node_id='a',result={'citations':[evidence()]},artifacts={'drawings':[dict(id='s',sha256='h')]}),{}
    view=build_overview(repo,j,live_reader=live)
    assert view['items']==[] and '丢弃' in view['warnings'][0]


def test_sidecar_auth_and_every_mutation_rejected(setup):
    fleet,wf,repo,add=setup;j,a=add();client=TestClient(create_app(fleet.root))
    assert client.get('/source-overview/api/jobs').status_code==401
    assert client.get('/source-overview/nodes/a/api/drawings').status_code==401
    client.cookies.set('cad_fleet_session',fleet.session())
    assert client.get('/source-overview/api/jobs/'+j).status_code==200
    assert client.get('/source-overview/nodes/a/api/chat-runs/anything').status_code==404
    for method in ('post','put','delete','patch'):
        assert getattr(client,method)('/source-overview/nodes/a/api/chat-runs').status_code==405
    assert client.get('/source-overview/assets/../source_overview.py').status_code==404


def test_live_reader_bounded_pages_and_complete_hash():
    records=[read_record(evidence('e'+str(i))) for i in range(7)]
    requested=[]
    def get(node,path,params):
        requested.append(path)
        if path=='/api/chat-runs/run':return {'id':'run','task_id':'t'}
        if path=='/api/tasks/t':return dict(task_id='t',snapshot_id='s',evidence_records={r['ref']['record_id']:r['ref'] for r in records})
        if path=='/api/drawings/s':return dict(id='s',sha256='h')
        r=next(r for r in records if r['ref']['record_id']==params['record_id'])
        return dict(record_id=r['ref']['record_id'],sha256=r['ref']['sha256'],start=0,content=r['content'],sources=r['sources'],has_more=False)
    live=LiveReader(get);job=dict(id='j',run_id='run',node_id='a');attempt={'id':'a'}
    archive,status=live(job,attempt)
    assert status['requests']==8 and not status['complete'] and status['records_loaded']==5
    for state in live.states.values():state['updated']=-100000
    archive,status=live(job,attempt)
    assert status['complete'] and status['records_loaded']==7 and status['requests']<=8
    assert len(records_from_archive(archive)[0])==7


def test_model_failure_does_not_erase_verified_reads(setup):
    fleet,wf,repo,add=setup;j,a=add();archive=deepcopy(repo.archive(j,a));archive['result']={};archive['execution_status']='failed'
    p=Projection();p.ingest(archive);view=p.finish()
    assert len(view['items'])==1 and view['items'][0]['stages']==['read']


def test_private_snapshot_includes_committed_wal_not_uncommitted_rows(tmp_path):
    path=tmp_path/'original.sqlite3';writer=sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE t(value)');writer.commit()
    writer.execute('INSERT INTO t VALUES(1)');writer.commit()
    reader=SnapshotReader(tmp_path/'private')
    before={p.name:sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob('original.sqlite3*')}
    assert reader.rows(path,'SELECT * FROM t',())==[{'value':1}]
    assert {p.name:sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob('original.sqlite3*')}==before
    writer.execute('INSERT INTO t VALUES(2)')
    assert reader.rows(path,'SELECT * FROM t',())==[{'value':1}]
    writer.commit()
    for key,v in reader.cache.items():reader.cache[key]=(v[0],v[1],0)
    assert reader.rows(path,'SELECT * FROM t',())==[{'value':1},{'value':2}]
    writer.close()
    for key,v in reader.cache.items():reader.cache[key]=(v[0],v[1],0)
    assert reader.rows(path,'SELECT * FROM t',())==[{'value':1},{'value':2}]
    assert not Path(str(path)+'-shm').exists()


def test_snapshot_retries_concurrent_file_change_and_never_reads_mixed_copy(tmp_path,monkeypatch):
    path=tmp_path/'original.sqlite3';db=sqlite3.connect(path);db.execute('CREATE TABLE t(value)');db.commit()
    from cad_agent.fleet import source_overview as module
    real=module.shutil.copyfile;calls=[]
    def copying(source,target):
        result=real(source,target);calls.append(source)
        if len(calls)==1:db.execute('INSERT INTO t VALUES(8)');db.commit()
        return result
    monkeypatch.setattr(module.shutil,'copyfile',copying)
    assert SnapshotReader(tmp_path/'private').rows(path,'SELECT * FROM t',())==[{'value':8}]
    assert len(calls)==2
    db.close()


def test_clean_main_viewer_adaptation_is_isolated_idempotent_and_valid_js(tmp_path):
    import subprocess
    source=Path(__file__).resolve().parents[1]/'web/app.js'
    original=source.read_bytes()
    adapted=viewer_script(original.decode())
    assert source.read_bytes()==original
    assert viewer_script(adapted)==adapted
    assert 'installSourceOverlay({state:S' in adapted
    assert 'if (!sourceOverviewMode) await loadFacts()' in adapted
    assert 'const saved=keepConversation || sourceOverviewMode' in adapted
    output=tmp_path/'viewer.mjs';output.write_text(adapted)
    subprocess.run(['node','--check',str(output)],check=True,capture_output=True)
    with pytest.raises(ValueError,match='接口已变化'):viewer_script('incompatible viewer')


def test_sidecar_serves_its_own_hooks_without_optional_results_or_workflow(setup):
    fleet,wf,repo,add=setup;add()
    client=TestClient(create_app(fleet.root));client.cookies.set('cad_fleet_session',fleet.session())
    response=client.get('/source-overview/nodes/a/assets/app.js')
    assert response.status_code==200
    assert 'installSourceOverlay({' in response.text
    assert '/source-overview/nodes/a/api/' in response.text
    assert client.get('/source-overview/nodes/a/assets/source-overlay.mjs').status_code==200


def test_measurement_projection_requires_host_receipt_and_exact_source_binding():
    from cad_agent.fleet.source_overview_support import export_measurements
    sources={'s':{'source_sha256':'h'}}
    ref={'complete_record':True};record=dict(ref=ref,sources={'s':'h'})
    bundle=dict(bundle_id='b',snapshot_id='s',source_sha256='h',scope={'layout_id':'Model'})
    measurement=dict(measurement_id='m',snapshot_id='s',source_sha256='h',bundle_id='b',observation_id='o',
        source_entity_ids=['e1','e2'],status='local_pair_measured',measurements={'width_drawing_units':10},
        measurement_line=[[0,0],[10,0]],operation_receipts=[dict(tool='measure_local_geometry',reference='m',local_result_available=True)])
    result=export_measurements([(record,bundle),(record,measurement)],sources)
    assert result['m']['bbox']==[0,0,10,0] and result['m']['layout_id']=='Model'
    assert export_measurements([(record,dict(measurement,operation_receipts=[]))],sources)=={}
    with pytest.raises(ValueError,match='SOURCE_NOT_BOUND'):
        export_measurements([(record,dict(measurement,source_sha256='wrong'))],sources)
    with pytest.raises(ValueError,match='LINE_VALUE_MISMATCH'):
        export_measurements([(record,dict(measurement,measurement_line=[[0,0],[20,0]]))],sources)
