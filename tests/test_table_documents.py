import json
from copy import deepcopy
from dataclasses import replace
import pytest
from conftest import dxf_bytes
from cad_agent.tables.service import TableService
from cad_agent.tables.contracts import SearchTables, ReadTableContent, RecoverTableRegion, ReviewTableRegion
from cad_agent.tables.reading import read
from cad_agent.tables.validation import validate


def grid(doc,angle=0):
    block=doc.blocks.new('TABLE')
    for x in (0,40,100):block.add_line((x,0),(x,60))
    for y in (0,20,40,60):block.add_line((0,y),(100,y))
    for x,y,t in [(2,65,'设备明细表'),(2,45,'序号'),(43,45,'面积(㎡)'),(2,25,'1'),(43,25,'933'),(2,5,'2'),(43,5,'830')]:
        block.add_text(t,dxfattribs=dict(insert=(x,y),height=3))
    doc.modelspace().add_blockref('TABLE',(1000,2000),dxfattribs=dict(rotation=angle))


def load(workspace,tmp_path,build=grid):
    drawing=workspace.ingest('table.dxf',dxf_bytes(tmp_path,build),synthetic=True)
    service=TableService(workspace,drawing['id'])
    return service,service.documents()['documents'][0]


@pytest.mark.parametrize('angle',[0,30,90,180])
def test_native_topology_rotated_instances(workspace,tmp_path,angle):
    service,d=load(workspace,tmp_path,lambda doc:grid(doc,angle))
    assert len(d['cells'])==6
    assert d['diagnostics']==[]
    assert {c['text'] for c in d['cells']}=={'序号','面积(㎡)','1','2','933','830'}
    assert len(d['source_inventory'])==6
    assert len({f['entity_id'] for c in d['cells'] for f in c['fragments']})==6


def test_scope_and_actual_union_coverage_survive_restart(workspace,tmp_path):
    service,d=load(workspace,tmp_path)
    p=read(service,ReadTableContent(table_id=d['table_id'],limit=2),'owner')
    assert p['coverage']['read_cells']==2 and not p['coverage']['selection_complete']
    again=read(service,ReadTableContent(table_id=d['table_id'],limit=2,read_plan_id=p['read_plan_id']),'owner')
    assert again['coverage']['read_cells']==2
    service=TableService(workspace,service.snapshot)
    while p['next_cursor']:
        p=read(service,ReadTableContent(table_id=d['table_id'],limit=3,read_plan_id=p['read_plan_id'],cursor=p['next_cursor']),'owner')
    assert p['coverage']['table_read_complete'] and p['coverage']['read_cells']==6
    assert not p['coverage']['scope_exhaustive']
    with pytest.raises(ValueError,match='SCOPE_CHANGED'):
        read(service,ReadTableContent(table_id=d['table_id'],read_plan_id=p['read_plan_id']),'different-owner')
    with pytest.raises(ValueError,match='CURSOR_INVALID'):
        read(service,ReadTableContent(table_id=d['table_id'],read_plan_id=p['read_plan_id'],cursor='forged'),'owner')


def test_field_selection_not_whole_table(workspace,tmp_path):
    service,d=load(workspace,tmp_path)
    f=next(f for f in d['fields'] if f['value']=='933')
    page=read(service,ReadTableContent(table_id=d['table_id'],fields=[f['field_id']]),'owner')
    assert page['coverage']['selection_complete']
    assert not page['coverage']['table_read_complete']
    assert page['items'][0]['fields'][0]['header_path']==['面积(㎡)']
    assert f['value']=='933' and f['unit']=='㎡'


def test_hierarchical_search_and_snapshot_reject(workspace,tmp_path):
    service,d=load(workspace,tmp_path)
    assert service.search(SearchTables(query='面积',level='field'))['total']==2
    assert service.search(SearchTables(query='设备明细表',level='table'))['total']==1
    with pytest.raises(ValueError,match='TABLE_NOT_IN_SNAPSHOT'):service.get('table-other')


def test_independent_validator_detects_forged_text_edge_duplicate(workspace,tmp_path):
    service,d=load(workspace,tmp_path)
    bad=deepcopy(d);bad['cells'][0]['fragments'][0]['raw_text']='made up'
    bad['cells'][0]['edge_sources'][0]=[]
    bad['cells'][1]['fragments'].append(bad['cells'][0]['fragments'][0])
    issues=validate(bad,service.index)
    assert {'SOURCE_TEXT_MISMATCH','UNSUPPORTED_CELL_EDGE','DUPLICATE_TEXT_ASSIGNMENT'}<=set(issues)


def test_borderless_recovery_retains_candidate_state(workspace,tmp_path):
    def build(doc):
        for r in range(4):
            for col in range(2):
                doc.modelspace().add_text(('名称' if col==0 else str(100+r)),dxfattribs=dict(insert=(col*40,100-r*10),height=3))
    drawing=workspace.ingest('borderless.dxf',dxf_bytes(tmp_path,build),synthetic=True)
    service=TableService(workspace,drawing['id']);data=service.documents()
    assert not data['documents'] and len(data['candidates'])==1
    from cad_agent.tables.recovery import prepare
    result=prepare(service,RecoverTableRegion(candidate_id=data['candidates'][0]['candidate_id']),'owner')
    recovered=service.get(result['table_ids'][0])
    assert recovered['structure_state']=='candidate'
    assert all(f['binding_state']=='candidate' for f in recovered['fields'])


def test_legacy_joined_numeric_entity_gets_own_readings():
    from cad_agent.contracts import SourceRegion,SourcePart
    from cad_agent.evidence import bind_evidence
    r=SourceRegion(region_id='r',layout_id='l',frame_id='',frame_name='',kind='paragraph',text='1933平方米',entity_ids=['ordinal','area'],bbox=[0,0,100,10],
        parts=[SourcePart(entity_id='ordinal',entity_text_span=[0,1],text_span=[0,1],bbox=[0,0,10,10]),SourcePart(entity_id='area',entity_text_span=[0,3],text_span=[1,4],bbox=[40,0,60,10])])
    e=bind_evidence('snap_a',r,1,4,'mm').model_dump()
    assert any(a['value']=='933' for a in e['numeric_readings']['items'])
    assert not any(a['value']=='1933' for a in e['numeric_readings']['items'])
    # Arbitrary slicing inside an entity must not manufacture a smaller number.
    assert not bind_evidence('snap_a',r,2,4,'mm').numeric_readings['items']


def test_workflow_preserves_all_cells_and_rejects_extra_constraints(workspace,tmp_path,monkeypatch):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from cad_agent.tables.workflow import try_run,route
    service,d=load(workspace,tmp_path)
    def review(session,text,feedback=None,**kwargs):
        session.runtime_diagnostics['model_requests'].append({'workflow':'table_documents'})
        return json.dumps({'cells':[dict(id=c['id'],state='read') for c in json.loads(text)['cells']]})
    monkeypatch.setattr('cad_agent.analysis.source_record_workflow._request',review)
    request=ChatRequest(snapshot_id=service.snapshot,question='设备明细表内有哪些内容',mode='agent')
    session=CadAgentSession(workspace,request)
    result=try_run(session)
    assert result['table_documents']['coverage']['table_read_complete']
    assert len(result['table_documents']['items'])==6
    assert '933' in result['answer'] and '830' in result['answer']
    other=CadAgentSession(workspace,request.model_copy(update={'question':'设备明细表内有哪些内容，只要面积大于900的'}))
    assert route(other) is None


def test_version_change_rejects_cursor_and_dropped_inventory(workspace,tmp_path):
    service,d=load(workspace,tmp_path)
    page=read(service,ReadTableContent(table_id=d['table_id'],limit=1),'owner')
    bad=deepcopy(d);bad['source_inventory']=bad['source_inventory'][1:]
    assert 'SOURCE_INVENTORY_INCOMPLETE' in validate(bad,service.index)
    path=workspace.store.artifact(service.snapshot,'source.dxf')
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='TABLE_SOURCE_CHANGED'):
        read(service,ReadTableContent(table_id=d['table_id'],cursor=page['next_cursor'],read_plan_id=page['read_plan_id']),'owner')


def test_row_labels_units_notes_and_cad_percent(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for x in (0,40,70,100,150):m.add_line((x,0),(x,80))
        for y in (0,20,40,60,80):m.add_line((0,y),(150,y))
        rows=[('总用地面积','12636.0','平方米','备注'),('地下建筑面积','9943.33','平方米','不计容建筑面积'),
              ('地上建筑面积','25161.37','平方米','计容建筑面积'),('建筑密度','28.96%%%','','')]
        for r,values in enumerate(rows):
            for x,value in zip((2,42,72,102),values):
                if value:m.add_text(value,dxfattribs=dict(insert=(x,65-r*20),height=2))
    service,d=load(workspace,tmp_path,build)
    assert not any(n['kind']=='group' for n in d['nodes'])
    f=next(f for f in d['fields'] if f['value']=='9943.33')
    assert f['label']=='地下建筑面积' and f['unit']=='平方米' and f['header_path']==[]
    assert [n['text'] for n in f['notes']]==['不计容建筑面积']
    percent=next(f for f in d['fields'] if f['value']=='28.96')
    assert percent['unit']=='%' and percent['label']=='建筑密度'
    assert percent['evidence'][0]['quote']=='28.96%%%'


def test_visual_review_requires_delivered_image_and_retains_transcription(workspace,tmp_path):
    from cad_agent.tables.recovery import review
    service,d=load(workspace,tmp_path)
    record=dict(id='r',owner_id='owner',snapshot_id=service.snapshot,signature=service.signature,
                layout_id=d['layout_id'],bbox=d['bbox'],image_id='image_test')
    workspace.store.put('table_recovery','r',record)
    args=ReviewTableRegion(recovery_id='r',cells=[dict(bbox=d['bbox'],transcription='仅视觉候选',row=0,column=0)])
    with pytest.raises(ValueError,match='IMAGE_NOT_DELIVERED'):review(service,args,'owner',set())
    result=review(service,args,'owner',{'image_test'})
    assert result['structure_state']=='candidate' and result['cells'][0]['source_state']=='image_transcription'
    assert len(result['unassigned_entity_ids'])==6


def test_native_acad_table_metadata_and_graphics_are_retained(workspace,tmp_path):
    from ezdxf.entities import factory
    from ezdxf.lldxf.extendedtags import ExtendedTags
    def build(doc):
        grid(doc)
        # ACAD_TABLE's referenced graphics are read without exploding it.
        block=doc.blocks.get('TABLE')
        tags='0\nACAD_TABLE\n100\nAcDbEntity\n8\n0\n100\nAcDbBlockReference\n2\nTABLE\n10\n0\n20\n0\n30\n0\n100\nAcDbTable\n91\n3\n92\n2\n'
        for value in ('序号','面积(㎡)','1','933','2','830'):
            tags+='171\n1\n1\n'+value+'\n'
        entity=factory.load(ExtendedTags.from_text(tags),doc)
        doc.entitydb.add(entity);doc.modelspace().add_entity(entity)
    service,d=load(workspace,tmp_path,build)
    docs=service.documents()['documents']
    native=next(d for d in docs if d['source_kind']=='native_table')
    assert native['native_metadata']['rows']==[['序号','面积(㎡)'],['1','933'],['2','830']]
    assert native['diagnostics']==[] and len(native['cells'])==6


def test_cell_api_and_overlay_use_exact_source_boundaries(workspace,tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cad_agent.tables.api import router
    service,d=load(workspace,tmp_path);app=FastAPI();app.include_router(router(workspace))
    with TestClient(app) as client:
        prefix=f'/api/drawings/{service.snapshot}/table-documents/{d["table_id"]}'
        c=d['cells'][0];source=client.get(prefix+'/cells/'+c['cell_id']).json()
        assert source['highlight_regions']==[dict(boundary=c['boundary'])]
        assert source['quote']==c['text']
        svg=client.get(prefix+'/overlay.svg')
        assert svg.status_code==200 and '<polygon' in svg.text and 'scale(1 -1)' in svg.text


def test_outer_edge_text_is_retained_but_not_confirmed(workspace,tmp_path):
    def build(doc):
        grid(doc)
        block=doc.blocks.get('TABLE')
        block.add_text('边缘说明',dxfattribs=dict(insert=(-.2,35),height=3))
        block.add_text('外部无关文字',dxfattribs=dict(insert=(-8,15),height=3))
    service,d=load(workspace,tmp_path,build)
    cell=next(c for c in d['cells'] if '边缘说明' in c['text'])
    assert cell['binding_state']=='candidate'
    assert 'TEXT_INSERTION_NEAR_OUTER_EDGE' in cell['diagnostics']
    assert any(f['assignment_basis']=='near_outer_edge' for f in cell['fragments'])
    assert len(d['source_inventory'])==7 and d['diagnostics']==[]
    assert all('外部无关文字' not in c['text'] for c in d['cells'])


def test_archived_view_preserves_history_without_resuming_stale_plan(workspace,tmp_path,monkeypatch):
    service,d=load(workspace,tmp_path)
    page=read(service,ReadTableContent(table_id=d['table_id'],limit=1),'owner')
    monkeypatch.setattr('cad_agent.tables.service.VERSION','test-next-version')
    newer=TableService(workspace,service.snapshot)
    assert newer.archived_view(d['table_id'])['historical_version']
    with pytest.raises(ValueError):
        read(newer,ReadTableContent(table_id=d['table_id'],read_plan_id=page['read_plan_id'],cursor=page['next_cursor']),'owner')
