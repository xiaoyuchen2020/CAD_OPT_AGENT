"""Shared source contracts, independent of benchmark object names/values."""
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from cad_agent.analysis.general import DrawingIndex
from cad_agent.components.context import build_context
from cad_agent.geometry.contracts import GeometryRead
from cad_agent.geometry.service import GeometryService
from cad_agent.semantics.tables import read_semantic_table
from cad_agent.semantics.contracts import SemanticTable, AxisLocation, AnnotationQuery
from test_components import ingest
from test_semantics import symbols


def test_native_path_read_works_without_scene_entity(workspace):
    handles=[]
    def build(doc):
        handles.append(doc.modelspace().add_circle((17,29),41).dxf.handle)
    _,d,lid=ingest(workspace,build)
    service=GeometryService(workspace,d['id'])
    out=service.read(GeometryRead(layout_id=lid,entity_paths=[handles]))
    assert out['items'][0]['native']['radius']==41
    with pytest.raises(ValueError):service.read(GeometryRead(layout_id='foreign',entity_paths=[handles]))
    with pytest.raises(ValidationError):GeometryRead(entity_ids=['x'],layout_id=lid,entity_paths=[handles])
    with pytest.raises(ValidationError):GeometryRead(layout_id=lid,entity_paths=[['../file']])


def test_insertion_point_scope_does_not_include_overhanging_text(workspace):
    def build(doc):
        for text,x in [('overhang',-2),('inside',2)]:
            doc.modelspace().add_text(text,dxfattribs={'height':2,'insert':(x,2)})
    _,d,lid=ingest(workspace,build)
    idx=DrawingIndex(workspace,d['id'])
    assert len(idx.entities(layout_id=lid,bbox=[0,0,20,8])['items'])==2
    actual=idx.entities(layout_id=lid,bbox=[0,0,20,8],spatial_relation='insertion_point_inside')['items']
    assert [i['text'] for i in actual]==['inside'] and actual[0]['origin'][:2]==[2,2]


def test_ordinal_cells_end_header_even_with_material_keywords(monkeypatch):
    import cad_agent.semantics.tables as module
    cells=[]
    for r,row in enumerate([['序号','部位名称','墙面','地面'],['1','设备房','材料做法','材料做法'],['2','过道','白色','黑色']]):
        for c,text in enumerate(row):
            cells.append(dict(cell_id=f'{r}-{c}',row_index=r,column_index=c,row_span=1,column_span=1,bbox=[c,r,c+1,r+1],text=text,entity_ids=[],evidence=[],state='candidate'))
    monkeypatch.setattr(module,'grid',lambda i,t:(cells,3,4,[]))
    idx=SimpleNamespace(tables=[dict(table_id='t',layout_id='l',bbox=[0,0,4,3])],snapshot='s',scene={'texts':[]})
    out=read_semantic_table(idx,SemanticTable(table_id='t',row_limit=3))
    assert out['header_rows']==[0]
    assert out['records'][0]['row_index']==1
    assert out['records'][0]['fields'][1]['text']=='设备房'


def test_axis_region_has_explicit_geometry_and_next_scope(symbols):
    _,service,lid=symbols
    axes=service.query(AnnotationQuery(kind='axes',layout_id=lid))['items']
    out=service.locate(AxisLocation(layout_id=lid,axis_ids=[a['object_id'] for a in axes]))
    assert out['result_kind']=='axis_region' and out['point'] is None and out['geometry_available']
    assert len(out['boundary'])==5 and out['next_actions'][0]['arguments']['spatial_relation']=='insertion_point_inside'


def test_separate_title_fields_have_sources_and_no_boundary(workspace):
    def build(doc):
        m=doc.modelspace()
        for text,x,y in [('图纸编号',0,20),('AB-51',16,20),('日期',0,10),('2030.07',16,10),('版本号',0,0),('C',16,0)]:
            m.add_text(text,dxfattribs={'height':2,'insert':(x,y)})
    _,d,lid=ingest(workspace,build)
    idx=DrawingIndex(workspace,d['id']);out=build_context(idx,lid,d['frames'])
    fields=[f for f in out['frames'] if f['kind']=='titleblock_fields']
    assert fields and any(v['value']=='AB-51' for f in fields for v in f['metadata']['drawing_number']['values'])
    assert all('boundary' not in f for f in fields)
    assert all(v['source_evidence'] for f in fields for value in f['metadata'].values() for v in value['values'])


def test_object_read_does_not_make_literal_field_query_mandatory():
    from test_r3_target_coverage import data
    from cad_agent.analysis.recall import pending_calls
    task=data(obj='交接处',field='内容')
    task['source_recall']['source_records']={'e':dict(quote='交接处设拉结筋，详结构图。',task_ids=['q1'])}
    calls=[c for c in pending_calls(task) if c['reason']=='explicit_object_field_gap']
    assert calls and all(not c['blocking'] for c in calls)
    assert task['subtasks'][0]['coverage'][0]['state']=='unread'
    task['source_recall']['source_records']['e']['task_ids']=['other']
    assert any(c['blocking'] for c in pending_calls(task) if c['reason']=='explicit_object_field_gap')


def test_context_structured_page_registers_exact_sources(workspace):
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    def build(doc):
        for text,x,y in [('图纸编号',0,20),('ZZ-71',16,20),('日期',0,10),('2031.08',16,10)]:
            doc.modelspace().add_text(text,dxfattribs={'height':2,'insert':(x,y)})
    _,d,lid=ingest(workspace,build)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='读取图签ZZ-71日期'))
    out=session.execute('get_drawing_context',dict(layout_id=lid,title='ZZ-71',view='frames',limit=5))
    assert out['items'] and session.evidence
    assert any(e['quote']=='2031.08' for e in session.evidence.values())
    assert all(e['snapshot_id']==d['id'] for e in session.evidence.values())


def test_large_context_keeps_full_items_and_real_page_cursor():
    from cad_agent.components.service import ComponentService
    from cad_agent.analysis.evidence_store import working_result
    from cad_agent.analysis.efficiency import dumps,compact
    rows=[dict(object_id=str(i),metadata={'value':'X'*9000}) for i in range(8)]
    record=dict(kind='context',frames=rows,subframes=[])
    page=ComponentService.page(record,'frames',0,8)
    assert 0<page['effective_limit']<8 and page['next_offset']==len(page['items'])
    out=working_result(page,{'record_id':'archive_dummy'})
    assert out['items']==page['items'] and len(dumps(compact(out)))<45000
