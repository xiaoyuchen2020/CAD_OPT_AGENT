import json

import pytest
from fastapi.testclient import TestClient

from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.semantics.contracts import AnnotationQuery, AxisLocation, DetailTrace, SemanticTable
from cad_agent.semantics.service import SemanticService
from conftest import dxf_bytes


@pytest.fixture
def symbols(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        def text(s,x,y,h=2):m.add_text(s,dxfattribs={'insert':(x,y),'height':h})
        for x,label in ((0,'1'),(100,'2')):
            m.add_circle((x,0),5);text(label,x-1,-1)
            m.add_line((x,5),(x,100))
        for y,label in ((25,'A'),(75,'B')):
            m.add_circle((-30,y),5);text(label,-31,y-1)
            m.add_line((-25,y),(130,y))
        m.add_circle((300,0),5);text('9',299,-1)
        m.add_circle((200,0),8);m.add_line((192,0),(208,0))
        text('1',198,1);text('A-01',195,-4)
        text('1 节点详图',180,80);text('11 节点详图',180,100)
        text('图号：A-01',180,120)
        m.add_leader([(250,0),(260,10),(280,10)])
        text('防水做法',280,10)
        m.add_lwpolyline([(0,130),(4,136),(-4,136)],close=True)
        text('+3.600',5,137);text('二层',30,137)
        text('+9.999',300,150)
    d=workspace.ingest('symbols.dxf',dxf_bytes(tmp_path,build))
    return d,SemanticService(workspace,d['id']),d['layouts'][0]['layout_id']


def test_axes_and_region_geometry(symbols):
    d,s,lid=symbols
    axes=s.query(AnnotationQuery(kind='axes',layout_id=lid))['items']
    assert {a['label'] for a in axes}=={'1','2','A','B'}
    by={a['label']:a['object_id'] for a in axes}
    result=s.locate(AxisLocation(layout_id=lid,axis_ids=[by['1'],by['A']]))
    assert result['point']==pytest.approx([0,25])
    from shapely.geometry import Polygon
    result=s.locate(AxisLocation(layout_id=lid,axis_ids=list(by.values())))
    assert Polygon(result['boundary']).area==pytest.approx(5000)
    assert s.locate(AxisLocation(layout_id=lid,axis_ids=[by['1'],by['2']]))['state']=='unresolved'
    with pytest.raises(ValueError):s.locate(AxisLocation(layout_id=lid,axis_ids=[by['1'],'foreign']))
    with pytest.raises(ValueError):AnnotationQuery(kind='axes',layout_id=lid,bbox=[0,0,float('nan'),1])
    assert s.query(AnnotationQuery(kind='axes',layout_id=lid,query='A',limit=1))['total']==1


def test_detail_trace_elevation_leader_and_ambiguity(symbols):
    d,s,lid=symbols
    refs=s.query(AnnotationQuery(kind='references',layout_id=lid))['items']
    assert len(refs)==1 and refs[0]['label']=='1/A-01'
    trace=s.trace(DetailTrace(layout_id=lid,reference_id=refs[0]['object_id']))
    assert trace['total']==1 and trace['items'][0]['title']=='1 节点详图'
    elevations=s.query(AnnotationQuery(kind='elevations',layout_id=lid))['items']
    assert len(elevations)==1 and elevations[0]['value']==3.6
    assert elevations[0]['value_unit'] is None and elevations[0]['datum']=='unknown'
    floors=s.query(AnnotationQuery(kind='storeys',layout_id=lid))['items']
    assert len(floors)==1 and floors[0]['elevation_ids']==[elevations[0]['object_id']]
    leaders=s.query(AnnotationQuery(kind='leaders',layout_id=lid))['items']
    assert len(leaders)==1 and leaders[0]['head']==[250,0]
    assert leaders[0]['text_relation']=='proximity_candidate'
    assert leaders[0]['target_state']=='unresolved'


def test_cross_snapshot_trace_keeps_provenance(workspace,tmp_path,symbols):
    d,s,lid=symbols
    other=workspace.ingest('detail.dxf',dxf_bytes(tmp_path,lambda doc:doc.modelspace().add_text('1 节点详图',dxfattribs={'height':2})))
    ref=s.query(AnnotationQuery(kind='references',layout_id=lid))['items'][0]
    result=s.trace(DetailTrace(layout_id=lid,reference_id=ref['object_id'],target_snapshot_ids=[other['id']]))
    assert result['status']=='ambiguous' and result['total']==2
    foreign=next(t for t in result['items'] if t['snapshot_id']==other['id'])
    assert all(e['snapshot_id']==other['id'] and 'evidence_id' not in e for e in foreign['source_records'])
    with pytest.raises(ValueError):s.trace(DetailTrace(layout_id=lid,reference_id='foreign'))


@pytest.fixture
def semantic_table(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for y in (0,15,30,45,60):m.add_line((0,y),(120,y))
        for x in (0,40,80,120):m.add_line((x,0),(x,60 if x in (0,40,120) else 45))
        for e in list(m.query('LINE')):
            if tuple(e.dxf.start)[:2]==(0,15):m.delete_entity(e)
        m.add_line((40,15),(120,15))
        values=[('编号',5,52),('洞口尺寸(mm)',45,52),('编号',5,37),('宽度',45,37),('高度',85,37),
                ('M101',5,22),('900',45,22),('2100',85,22),('2400',85,7),('M101',180,22)]
        for t,x,y in values:m.add_text(t,dxfattribs={'insert':(x,y),'height':2})
    d=workspace.ingest('table.dxf',dxf_bytes(tmp_path,build))
    s=SemanticService(workspace,d['id']);table=s.index.tables[0]
    return d,s,table


def test_multilevel_headers_units_merges_and_no_fill(semantic_table):
    d,s,t=semantic_table
    result=s.table(SemanticTable(table_id=t['table_id'],row_limit=5))
    assert result['header_rows']==[0,1]
    assert result['row_count']==4
    rows=result['items']
    width=next(c for c in rows[2]['cells'] if c['column_index']==1)
    assert width['header_path']==['洞口尺寸(mm)','宽度'] and width['unit']=='mm'
    assert width['numeric_value']==900 and width['unit_evidence']
    first=rows[2]['cells'][0];continued=rows[3]['cells'][0]
    assert first['row_span']==2 and continued['continuation_of']==first['cell_id']
    blank=next(c for c in rows[3]['cells'] if c['column_index']==1)
    assert blank['text']=='' and blank['numeric_value'] is None and blank['value_state']=='unknown'
    assert result['instance_links'][0]['total']==1
    assert s.index.read_table(t['table_id'])['header_state']=='unconfirmed'
    for row in rows:
        for c in row['cells']:
            for e in c['evidence']:
                region=s.index.by_id[e['region_id']];a,b=e['text_span']
                assert region.text[a:b]==e['quote']


def test_tool_and_http_integration(workspace,symbols):
    d,service,lid=symbols
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='轴网',mode='agent'))
    result=s.execute('query_annotations',{'kind':'axes','layout_id':lid,'limit':1})
    assert result['has_more'] and result['items'][0]['evidence'][0]['evidence_id'] in s.evidence
    s.register_observation('query_annotations',result)
    oid=result['observation_id']
    with pytest.raises(ValueError):
        s.validate_answer(json.dumps({'claims':[{'text':'已确认轴网','state':'observed','evidence_basis':'tool_observation','observation_ids':[oid],'evidence_ids':[]}]}))
    with TestClient(create_app(workspace.settings)) as client:
        prefix=f"/api/drawings/{d['id']}/semantics/"
        response=client.post(prefix+'query',json={'kind':'axes','layout_id':lid})
        assert response.status_code==200 and response.json()['total']==4
        assert client.post(prefix+'query',json={'kind':'axes','layout_id':'foreign'}).status_code==400
        assert client.post(prefix+'locate',json={'layout_id':lid,'axis_ids':['a','b']}).status_code==400


def test_exploded_arrow_and_index_not_axis(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        m.add_lwpolyline([(0,0),(10,1),(10,-1)],close=True)
        m.add_line((0,0),(60,0));m.add_line((60,0),(70,10))
        m.add_text('防水收口做法',dxfattribs={'insert':(70,10),'height':2})
        # A long divider passing through a split index is not an axis.
        m.add_circle((200,0),5);m.add_line((180,0),(240,0))
        m.add_text('10',dxfattribs={'insert':(199,1),'height':2})
        m.add_text('-',dxfattribs={'insert':(199,-3),'height':2})
        m.add_text('江北区JB15地块',dxfattribs={'insert':(300,0),'height':2})
        m.add_text('(B2F)',dxfattribs={'insert':(300,30),'height':2})
    d=workspace.ingest('exploded.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id']);lid=d['layouts'][0]['layout_id']
    assert s.query(AnnotationQuery(kind='axes',layout_id=lid))['total']==0
    refs=s.query(AnnotationQuery(kind='references',layout_id=lid))['items']
    assert refs[0]['label']=='10/-'
    leaders=s.query(AnnotationQuery(kind='leaders',layout_id=lid))['items']
    arrow=next(l for l in leaders if l['method']=='triangle_connected_lines')
    assert arrow['label']=='防水收口做法'
    floors=s.query(AnnotationQuery(kind='storeys',layout_id=lid))['items']
    assert len(floors)==1 and floors[0]['storey_number']==-2


def test_unit_conflict_and_data_not_assumed_header(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for y in (0,15,30,45):m.add_line((0,y),(120,y))
        # Table columns can continue below the selected table band to a footer.
        for x in (0,40,80,120):m.add_line((x,-20),(x,45))
        for t,x,y in [('编号',5,38),('宽度(mm)',45,38),('高度(mm)',85,38),('M1',5,23),('0.9m',45,23),('2100',85,23),('M2',5,8),('1000',45,8),('2400',85,8)]:
            m.add_text(t,dxfattribs={'insert':(x,y),'height':2})
        # A line extending well beyond the table must not invent extra columns.
        m.add_line((50,-200),(50,200))
    d=workspace.ingest('units.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id'])
    t=s.index.tables[0];result=s.table(SemanticTable(table_id=t['table_id'],row_limit=5))
    assert result['column_count']==3
    width=result['items'][1]['cells'][1]
    assert width['unit_state']=='conflict' and width['unit'] is None
    assert width['unit_candidates']==['m','mm'] and width['numeric_value']==.9
    with pytest.raises(ValueError):SemanticTable(table_id=t['table_id'],header_rows=[0,2])
    with pytest.raises(ValueError):s.table(SemanticTable(table_id='foreign'))


def test_attdef_display_and_bulged_detail_marker(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        # Two real sheets in one Model layout. A layout is not a sheet identity.
        for dx,sheet in ((0,'J11'),(250,'J12')):
            m.add_lwpolyline([(dx,0),(dx+180,0),(dx+180,120),(dx,120)],close=True)
            m.add_text('排水平面图',dxfattribs={'insert':(dx+100,100),'height':2})
            m.add_lwpolyline([(dx+15,20,1),(dx+25,20,1)],format='xyb',close=True)
            m.add_attdef('18',insert=(dx+18,18),text='3',dxfattribs={'height':3})
            m.add_text('排水沟大样4 1:25',dxfattribs={'insert':(dx+30,20),'height':2})
            for y in (19,18.7):m.add_line((dx+30,y),(dx+60,y))
            for y in (0,10,20):m.add_line((dx+110,y),(dx+175,y))
            for x in (110,140,175):m.add_line((dx+x,0),(dx+x,20))
            for text,x,y in [('图纸编号',112,13),(sheet,142,13),('项目名称',112,3),('测试',142,3)]:
                m.add_text(text,dxfattribs={'insert':(dx+x,y),'height':2})
        m.add_circle((80,80),7);m.add_line((73,80),(87,80))
        m.add_text('18',dxfattribs={'insert':(77,81),'height':2})
        m.add_text('J11',dxfattribs={'insert':(77,76),'height':2})
        # Ordinary INSERT values continue to read ATTRIB text, never its tag.
        block=doc.blocks.new('TAGGED');block.add_attdef('CODE',insert=(0,0),text='default')
        m.add_blockref('TAGGED',(200,150)).add_auto_attribs({'CODE':'instance-value'})
    d=workspace.ingest('details.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id']);lid=d['layouts'][0]['layout_id']
    assert [t['text'] for t in s.index.scene['texts'] if t['kind']=='ATTDEF']==['18','18']
    assert any(t['kind']=='ATTRIB' and t['text']=='instance-value' for t in s.index.scene['texts'])
    ref=s.query(AnnotationQuery(kind='references',layout_id=lid))['items'][0]
    result=s.trace(DetailTrace(layout_id=lid,reference_id=ref['object_id']))
    assert result['total']==1 and result['excluded_sheet_mismatches']==1
    target=result['items'][0]
    assert target['detail_number']=='18' and target['method']=='circle_underlined_title'
    assert target['sheet_basis']=='explicit_titleblock_field'
    assert {'18','图纸编号','J11'} <= {v['quote'] for v in target['source_records']}
    assert target['support_entity_ids']


def test_grid_reuses_anchors_ignores_logo_and_recovers_through_columns(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for y in (0,15,30,45,60):m.add_line((0,y),(120,y))
        for x in (0,40,80,120):m.add_line((x,0),(x,60))
        for t,x,y in [('编号',5,52),('宽度',45,52),('高度',85,52),('M1',5,37),('900',45,37),('2100',85,37),('M2',5,22),('1000',45,22),('2400',85,22),('M3',5,7)]:
            m.add_text(t,dxfattribs={'insert':(x,y),'height':2})
        # Dense signature-like graphics inside one cell must not cause 100x100 grid.
        for i in range(90):
            m.add_line((85+i*.2,3),(85+i*.2,5));m.add_line((85,3+i*.02),(103,3+i*.02))
    d=workspace.ingest('logo.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id']);t=s.index.tables[0]
    full=s.table(SemanticTable(table_id=t['table_id'],row_limit=5))
    assert full['column_count']==3 and full['row_count']==4
    # Same existing source bands, cropped to the middle continuation page.
    import copy
    middle=copy.deepcopy(t);middle['rows']=middle['rows'][1:3];middle['row_count']=2;middle['bbox'][1]=15;middle['bbox'][3]=45
    s.index._tables=[middle]
    page=s.table(SemanticTable(table_id=t['table_id'],row_limit=5))
    assert page['column_count']==3 and page['row_count']==2
    assert [c['text'] for c in page['items'][0]['cells']]==['M1','900','2100']


def test_scale_only_target_retains_unknown_title_and_does_not_promote_axis(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        m.add_circle((20,20),5);m.add_text('9',dxfattribs={'insert':(18,18),'height':3})
        m.add_text('1:50',dxfattribs={'insert':(28,16),'height':3})
        m.add_circle((80,20),5);m.add_text('10',dxfattribs={'insert':(78,18),'height':3})
        m.add_text('1:50',dxfattribs={'insert':(88,16),'height':3});m.add_line((80,25),(80,100))
        for x,key in [(20,'9'),(80,'10')]:
            m.add_circle((x,150),5);m.add_line((x-5,150),(x+5,150))
            m.add_text(key,dxfattribs={'insert':(x-1,151),'height':2})
            m.add_text('-',dxfattribs={'insert':(x-1,147),'height':2})
    d=workspace.ingest('scale-only.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id']);lid=d['layouts'][0]['layout_id']
    refs=s.query(AnnotationQuery(kind='references',layout_id=lid))['items']
    results={r['detail_number']:s.trace(DetailTrace(layout_id=lid,reference_id=r['object_id'])) for r in refs}
    assert results['9']['total']==1 and results['10']['total']==0
    target=results['9']['items'][0]
    assert target['title_state']=='unresolved' and target['method']=='circle_scale_marker'
    # The layout fallback is not accepted as a same-sheet identity.
    assert target['state']=='candidate_sheet_unresolved'


def test_dense_plan_candidate_is_not_recovered_as_table(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for y in (0,20,40):m.add_line((0,y),(120,y))
        for x in (0,40,80,120):m.add_line((x,0),(x,40))
        for x in (0,40,80):
            m.add_text('房间',dxfattribs={'insert':(x+2,35),'height':1})
            m.add_text('标记',dxfattribs={'insert':(x+2,15),'height':1})
            for j in range(10):m.add_line((x+10+j,23),(x+10+j,29))
    d=workspace.ingest('not-a-table.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id'])
    with pytest.raises(ValueError,match='TABLE_GRAPHICS_DOMINATE'):
        s.table(SemanticTable(table_id=s.index.tables[0]['table_id']))


def test_nonrectangular_missing_edges_do_not_silently_drop_text(workspace,tmp_path):
    from cad_agent.semantics.tables import grid
    def build(doc):
        m=doc.modelspace()
        for a,b in [((0,0),(40,0)),((40,0),(40,40)),((40,40),(0,40)),((0,40),(0,0)),((20,0),(20,20)),((20,20),(40,20))]:m.add_line(a,b)
        m.add_text('保留原文',dxfattribs={'insert':(3,30),'height':2})
    d=workspace.ingest('nonrectangle.dxf',dxf_bytes(tmp_path,build));s=SemanticService(workspace,d['id']);lid=d['layouts'][0]['layout_id']
    # A source candidate with incomplete inner edges forms an L-shaped component.
    table={'table_id':'candidate','layout_id':lid,'bbox':[0,0,40,40],'column_count':2,'rows':[
        {'bbox':[0,20,40,40],'cells':[{'bbox':[0,20,20,40]},{'bbox':[20,20,40,40]}]},
        {'bbox':[0,0,40,20],'cells':[{'bbox':[0,0,20,20]},{'bbox':[20,0,40,20]}]}]}
    with pytest.raises(ValueError,match='NON_RECTANGULAR_CELL_COMPONENT'):grid(s.index,table)
