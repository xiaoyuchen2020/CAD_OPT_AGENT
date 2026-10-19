import copy
import json

import pytest
from fastapi.testclient import TestClient

from cad_agent.analysis.general import DrawingIndex
from cad_agent.app import create_app
from cad_agent.extensions.table_recovery.bundle import prepare, verify_bundle, digest
from cad_agent.extensions.table_recovery.contracts import RunFixed, ReadInput
from cad_agent.extensions.table_recovery.service import RecoveryService
from cad_agent.extensions.table_recovery.validator import validate
from cad_agent.extensions.table_recovery.worker import recover
from cad_agent.extensions.table_recovery.sandbox import SandboxError
from cad_agent.semantics.tables import grid
from conftest import dxf_bytes


@pytest.fixture
def recovery_case(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for x in (0,40,80,120):m.add_line((x,0),(x,45))
        for y in (0,15,30,45):m.add_line((0,y),(120,y))
        for y,values in ((38,('编号','宽度(mm)','高度(mm)')),(23,('M1','900','2100')),(8,('M2','','2400'))):
            for x,value in zip((5,45,85),values):
                if value:m.add_text(value,dxfattribs={'insert':(x,y),'height':2})
    d=workspace.ingest('recovery.dxf',dxf_bytes(tmp_path,build))
    idx=DrawingIndex(workspace,d['id']);table=idx.tables[0]
    bundle=prepare(idx,table['table_id'])
    return d,idx,table,bundle


def reseal(bundle):
    body={k:v for k,v in bundle.items() if k not in ('input_id','bundle_sha256')}
    bundle['bundle_sha256']=digest(body);bundle['input_id']='tri_'+bundle['bundle_sha256']


def test_frozen_source_contract_and_unchanged_component(recovery_case):
    d,idx,table,bundle=recovery_case
    before=digest(grid(idx,table));raw_before=digest(idx.read_table(table['table_id']))
    verify_bundle(bundle)
    output=recover(bundle);result=validate(bundle,output)
    assert result['validation_status']=='passed' and result['review_status']=='pending'
    assert result['semantic_acceptance'] is False
    assert len(result['cells'])==9
    assert any(c['text']=='' and not c['evidence'] for c in result['cells'])
    for c in result['cells']:
        for e in c['evidence']:
            r=idx.by_id[e['region_id']];a,b=e['text_span']
            assert r.text[a:b]==e['quote'] and e['snapshot_id']==d['id']
    assert digest(grid(idx,table))==before and digest(idx.read_table(table['table_id']))==raw_before
    assert prepare(idx,table['table_id'])==bundle
    bundle['texts'][0]['text']='tampered'
    with pytest.raises(ValueError,match='INPUT_INTEGRITY_FAILED'):verify_bundle(bundle)


@pytest.mark.parametrize('attack,expected',[
    ('foreign_input','INPUT_ID_MISMATCH'),('fake_text','UNKNOWN_TEXT_ENTITY'),
    ('dropped_text','TEXT_LOSS_OR_DUPLICATION'),('duplicate_text','TEXT_LOSS_OR_DUPLICATION'),
    ('wrong_cell','TEXT_ASSIGNMENT_MISMATCH'),('fake_segment','UNKNOWN_SEGMENT'),
    ('missing_edge','UNSUPPORTED_CELL_EDGE'),('overlap','OVERLAPPING_CELLS'),
    ('empty','GRID_SLOT_COVERAGE_INCOMPLETE'),('out_of_scope','GRID_ANCHOR_MISMATCH'),
    ('nan','INVALID_OUTPUT_SCHEMA'),('forged_evidence','INVALID_OUTPUT_SCHEMA')])
def test_independent_validator_rejects_wrong_results(recovery_case,attack,expected):
    bundle=recovery_case[3];output=copy.deepcopy(recover(bundle))
    cells=output['cells']
    if attack=='foreign_input':output['input_id']='tri_foreign'
    elif attack=='fake_text':cells[0]['text_entity_ids']=['foreign_entity']
    elif attack=='dropped_text':cells[0]['text_entity_ids']=[]
    elif attack=='duplicate_text':cells[0]['text_entity_ids']*=2
    elif attack=='wrong_cell':cells[0]['text_entity_ids'],cells[1]['text_entity_ids']=cells[1]['text_entity_ids'],cells[0]['text_entity_ids']
    elif attack=='fake_segment':cells[0]['edge_segment_ids'][0]=['forged']
    elif attack=='missing_edge':cells[0]['edge_segment_ids'][0]=[]
    elif attack=='overlap':cells.append(copy.deepcopy(cells[0]))
    elif attack=='empty':output['cells']=[]
    elif attack=='out_of_scope':output['xs'][0]-=1
    elif attack=='nan':output['xs'][0]=float('nan')
    elif attack=='forged_evidence':cells[0]['evidence']=[{'quote':'invented'}]
    result=validate(bundle,output)
    assert result['validation_status']=='rejected' and not result['cells']
    assert expected in result['errors']


def test_one_giant_cell_cannot_hide_real_separators(recovery_case):
    bundle=recovery_case[3];output=recover(bundle)
    cell=copy.deepcopy(output['cells'][0]);cell.update(row_span=3,column_span=3,text_entity_ids=[t['entity_id'] for t in bundle['texts']])
    output['cells']=[cell]
    assert 'MERGE_ERASES_SOURCE_SEPARATOR' in validate(bundle,output)['errors']


def test_known_graphics_and_incomplete_inputs_fail_closed(recovery_case):
    bundle=copy.deepcopy(recovery_case[3]);bundle['baseline']={'status':'unresolved','code':'TABLE_GRAPHICS_DOMINATE'};reseal(bundle)
    output=recover(bundle)
    assert output['status']=='unresolved'
    assert validate(bundle,output)['validation_status']=='needs_review'
    bundle['coverage']['blocking_diagnostics']=[{'code':'ENTITY_LIMIT'}];reseal(bundle)
    assert recover(bundle)['reason']=='INPUT_INCOMPLETE_OR_UNSUPPORTED'


def test_service_idempotence_failure_and_api_no_code(workspace,recovery_case,monkeypatch):
    d,idx,table,bundle=recovery_case
    service=RecoveryService(workspace,d['id'],idx)
    diag=service.diagnose(table['table_id'])
    args=RunFixed(diagnostic_id=diag['diagnostic_id'],input_id=diag['input_id'],idempotency_key='one')
    calls=[]
    def unavailable(*a,**kw):
        calls.append(1);raise SandboxError('SANDBOX_UNAVAILABLE')
    monkeypatch.setattr('cad_agent.extensions.table_recovery.service.execute',unavailable)
    result=service.run(args)
    assert result['execution_status']=='failed' and result['error']=='SANDBOX_UNAVAILABLE'
    assert service.run(args)==result and len(calls)==1
    assert service.read_input(ReadInput(input_id=bundle['input_id'],limit=1))['has_more']
    with TestClient(create_app(workspace.settings)) as client:
        prefix=f"/api/drawings/{d['id']}/table-recovery"
        assert client.post(prefix+'/diagnostics',json={'table_id':table['table_id']}).status_code==200
        assert client.post(prefix+'/diagnostics',json={'table_id':'foreign'}).status_code==400
        assert client.post(prefix+'/runs',json=args.model_dump()|{'source':'import os'}).status_code==422
        assert client.post(prefix+'/runs',json=args.model_dump()|{'method_ref':'arbitrary'}).status_code==422
        assert client.post('/api/drawings/snap_foreign/table-recovery/inputs/read',json={'input_id':bundle['input_id']}).status_code==404
    for i in (2,3):service.run(args.model_copy(update={'idempotency_key':str(i)}))
    with pytest.raises(ValueError,match='RECOVERY_ATTEMPT_LIMIT'):
        service.run(args.model_copy(update={'idempotency_key':'4'}))


def test_source_mutation_refused(workspace,recovery_case):
    d,idx,table,bundle=recovery_case
    path=workspace.store.artifact(d['id'],'source.dxf');path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='SOURCE_INTEGRITY_FAILED'):prepare(idx,table['table_id'])


def test_success_records_have_distinct_ids_and_result_pagination(workspace,recovery_case,monkeypatch):
    from cad_agent.extensions.table_recovery.contracts import ResultPage
    d,idx,table,bundle=recovery_case
    def fixed_for_unit_test(data_dir,request):
        output=recover(request['bundle'])
        return dict(output=output,output_sha256=digest(output),worker_sha256='unit-test',isolation={},elapsed_seconds=0.)
    monkeypatch.setattr('cad_agent.extensions.table_recovery.service.execute',fixed_for_unit_test)
    service=RecoveryService(workspace,d['id'],idx);diag=service.diagnose(table['table_id'])
    run=service.run(RunFixed(diagnostic_id=diag['diagnostic_id'],input_id=diag['input_id'],idempotency_key='success'))
    assert run['execution_status']=='completed' and run['validation_status']=='passed'
    assert service.record('table_recovery_execution',run['execution_id'])['worker_sha256']=='unit-test'
    assert service.get_run(run['run_id'])==run
    page=service.result(ResultPage(result_id=run['result_id'],limit=2))
    assert page['total']==9 and len(page['items'])==2 and page['has_more']
    svg=service.overlay(run['result_id'])
    assert '#d05a00' in svg and '候选网格' in svg


def test_small_gap_supported_large_gap_unresolved(recovery_case):
    bundle=copy.deepcopy(recovery_case[3])
    original=next(s for s in bundle['segments'] if s['a']==[40.,0.] and s['b']==[40.,45.])
    bundle['segments'].remove(original)
    for gap,expect in ((.1,'candidate'),(3.,'unresolved')):
        variant=copy.deepcopy(bundle)
        low=copy.deepcopy(original);low.update(segment_id='low',b=[40.,22.5-gap/2])
        high=copy.deepcopy(original);high.update(segment_id='high',a=[40.,22.5+gap/2])
        variant['segments'] += [low,high];reseal(variant)
        output=recover(variant)
        assert output['status']==expect
        if expect=='candidate':assert validate(variant,output)['validation_status']=='passed'


def test_oversized_input_is_a_diagnostic_not_a_truncated_bundle(workspace,recovery_case,monkeypatch):
    d,idx,table,bundle=recovery_case
    def too_large(*args):raise ValueError('INPUT_LIMIT_EXCEEDED: segments')
    monkeypatch.setattr('cad_agent.extensions.table_recovery.service.prepare',too_large)
    result=RecoveryService(workspace,d['id'],idx).diagnose(table['table_id'])
    assert result['code']=='INPUT_LIMIT_EXCEEDED' and result['input_id'] is None
    assert result['input_available'] is False
