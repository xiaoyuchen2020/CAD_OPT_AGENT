import pytest

from cad_agent.semantics.contracts import AnnotationQuery, DetailTrace
from cad_agent.semantics.service import SemanticService
from conftest import dxf_bytes


def text(m, value, x, y, h=2):
    m.add_text(value, dxfattribs={'insert': (x,y), 'height': h})


def reference(m, key, sheet, x=80, y=60):
    m.add_circle((x,y),5);m.add_line((x-5,y),(x+5,y))
    text(m,key,x-2,y+1);text(m,sheet,x-3,y-3)


def sheet(m, x, number):
    m.add_lwpolyline([(x,0),(x+180,0),(x+180,120),(x,120)],close=True)
    text(m,'一层平面图',x+20,100)
    text(m,'图号：'+number,x+20,10)
    text(m,'建筑',x+20,15)


def service(workspace,tmp_path,build):
    d=workspace.ingest('scope.dxf',dxf_bytes(tmp_path,build))
    s=SemanticService(workspace,d['id']);lid=d['layouts'][0]['layout_id']
    r=s.query(AnnotationQuery(kind='references',layout_id=lid))['items'][0]
    return s,s.trace(DetailTrace(layout_id=lid,reference_id=r['object_id']))


def test_placeholder_scope_note_finds_other_sheet_and_preserves_raw(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace();sheet(m,0,'J13');sheet(m,250,'JRF01')
        reference(m,'2A','-');text(m,'详见JRF01',60,54,3)
        text(m,'2A 节点详图',280,60);text(m,'2A 节点详图',20,35)
    s,r=service(workspace,tmp_path,build)
    assert r['reference']['label']=='2A/-'
    assert r['reference_scope']['effective_sheet_number']=='JRF01'
    assert r['total']==1 and r['items'][0]['sheet_basis']=='explicit_titleblock_field'
    assert r['excluded_targets'][0]['declared_sheet_numbers']==['J13']
    assert r['reference_scope']['evidence'][0]['quote']=='详见JRF01'
    for e in r['reference_scope']['evidence']+r['excluded_targets'][0]['source_records']:
        a,b=e['text_span'];assert s.index.by_id[e['region_id']].text[a:b]==e['quote']


@pytest.mark.parametrize('raw,extra,code',[('-', '详见JRF02','COMPETING_REFERENCE_SCOPE_NOTES'),('J02',None,'CIRCLE_AND_NOTE_SHEET_CONFLICT')])
def test_competing_scope_notes_never_choose_nearest(workspace,tmp_path,raw,extra,code):
    def build(doc):
        m=doc.modelspace();reference(m,'2A',raw);text(m,'详见JRF01',60,54,3)
        if extra:text(m,extra,60,65,3)
        text(m,'2A 节点详图',200,60)
    s,r=service(workspace,tmp_path,build)
    assert r['total']==0 and r['diagnosis']['code']=='reference_scope_conflict'
    assert code in r['reference_scope']['diagnostics']


def test_atlas_page_does_not_match_unrelated_drawing_number(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace();sheet(m,0,'161');reference(m,'M1','161')
        m.add_text('预埋件参06J403-1',dxfattribs={'insert':(40,54),'height':3,'rotation':359.996})
        text(m,'M1 节点详图',20,35)
    s,r=service(workspace,tmp_path,build)
    assert r['total']==0
    assert r['reference_scope']['atlas_number']=='06J403-1'
    assert r['reference_scope']['page_number']=='161'
    assert r['diagnosis']['code']=='external_atlas_reference'
    assert not r['diagnosis']['missing_resource_confirmed']


def test_missing_sheet_is_observation_not_claim_of_missing_file(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace();reference(m,'2A','-');text(m,'详见JRF01',60,54,3)
        # A distant note belongs to a different annotation, not this index.
        text(m,'详见JRF02',60,20,3)
    s,r=service(workspace,tmp_path,build)
    assert r['diagnosis']['code']=='requested_sheet_not_observed'
    assert r['reference_scope']['effective_sheet_number']=='JRF01'
    assert not r['diagnosis']['missing_resource_confirmed']


@pytest.mark.parametrize('axis', [False, True])
def test_unscaled_marker_uses_explicit_detail_sheet_fields_not_section_name(workspace,tmp_path,axis):
    def build(doc):
        m=doc.modelspace()
        m.add_lwpolyline([(0,0),(180,0),(180,120),(0,120)],close=True)
        text(m,'门窗表',100,100)
        for value,x,y in [('图纸名称',10,20),('节点大样一',25,20),('图纸编号',10,12),('J03',25,12),('比例',60,12),('1:25',70,12)]:text(m,value,x,y)
        m.add_circle((50,60),5);text(m,'1',48,58,7)
        if axis:m.add_line((50,65),(50,110))
        # A section is not an ordinary detail with the key "1".
        text(m,'1-1剖面图',280,70)
        reference(m,'1','J02',300,60)
    s,r=service(workspace,tmp_path,build)
    assert r['total']==0
    assert any(v['value']=='J03' for v in r['sheet_inventory'])
    assert r['excluded_sheet_mismatches']==(0 if axis else 1)
    if not axis:
        t=r['excluded_targets'][0]
        assert t['method']=='circle_detail_sheet_marker'
        assert t['title_state']=='sheet_context_only'
        assert t['declared_sheet_numbers']==['J03']
