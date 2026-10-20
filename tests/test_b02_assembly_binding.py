"""B02 replay uses immutable real DXF source snapshots, never a model or a store write."""
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from cad_agent.analysis.assembly import HEADING, _bounded_section, _section_binding, read
from cad_agent.analysis.fact_tools import ReadAssembly
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.tool_registry import TOOL_SPECS
from cad_agent.contracts import SourceRegion
from cad_agent.semantics.contracts import SemanticTable
from cad_agent.semantics.tables import field_roles, read_semantic_table

FROZEN = Path(__file__).resolve().parents[1] / 'artifacts/designer-facts-repaired-20261019/inputs'
TABLE_SNAPSHOT = 'snap_387d51baa9bf10359b5cf120'
SECTION_SNAPSHOT = 'snap_ef95ba940715f9f93396f465'


class FrozenIndex(DrawingIndex):
    def __init__(self, snapshot):
        folder = FROZEN / snapshot
        if not folder.exists():
            pytest.skip('Frozen real DXF fixture is not distributed with this checkout')
        self.snapshot = snapshot
        self.regions = [SourceRegion.model_validate(r) for r in json.loads((folder/'regions.json').read_text())]
        self.by_id = {r.region_id:r for r in self.regions}
        self._scene = json.loads((folder/'scene.json').read_text())
        self.drawing = self._scene
        self._tables = None
        self.entity_sources = defaultdict(list)
        for r in self.regions:
            for part in r.parts:self.entity_sources[part.entity_id].append((r,part))

    @property
    def scene(self):return self._scene


@pytest.fixture(scope='module')
def frozen():
    return {s:FrozenIndex(s) for s in (TABLE_SNAPSHOT, SECTION_SNAPSHOT)}


def verify(index, value):
    if isinstance(value,dict):
        if 'evidence_id' in value:
            r=index.by_id[value['region_id']]
            assert r.text[slice(*value['text_span'])]==value['quote']
            assert value['snapshot_id']==index.snapshot
            assert set(value['entity_ids'])<=set(r.entity_ids)
            assert value['bbox'] and value['component_boxes']
        if 'tool' in value and 'arguments' in value:
            TOOL_SPECS[value['tool']][0].model_validate(value['arguments'],strict=True)
        for item in value.values():verify(index,item)
    elif isinstance(value,list):
        for item in value:verify(index,item)


def pages(index,rid,view='record',limit=3):
    args=ReadAssembly(region_id=rid,view=view,limit=limit)
    output=[]
    for _ in range(100):
        result=read(index,args);verify(index,result);output.append(result)
        if not result['has_more']:return output
        args=args.model_copy(update={'offset':result['next_offset']})
    raise AssertionError('Nonterminating pagination')


@pytest.mark.parametrize('rid,phrase,excluded,count',[
    ('region_2ce04d7bc949ee948f56cba9','非固化沥青','耐根穿刺',7),
    ('region_d08125ccb740d8bdd1a43909','耐根穿刺','上人屋面面层及结合层见',11),
])
def test_real_closed_row_binds_split_headers_fields_and_direction(frozen,rid,phrase,excluded,count):
    results=pages(frozen[TABLE_SNAPSHOT],rid)
    binding=results[0]['source_binding']
    assert binding['state']=='source_bound'
    assert [h['text'] for h in binding['headers']['fields']]==['序号','部位','做法','备注']
    assert all(h['evidence'] for h in binding['headers']['fields'])
    assert binding['headers']['source_lines']
    assert binding['layer_order']=='explicit_header_direction' and binding['order_evidence']
    assert binding['identity_columns']==[1] and binding['layer_columns']==[2]
    assert binding['fields'][3]['value_state']=='unknown'
    assert binding['fields'][3]['evidence']==[]
    assert binding['object_instance_state']=='unverified'
    items=[i for page in results for i in page['items']]
    assert len(items)==count
    text=''.join(i['evidence']['quote'] for i in items)
    assert phrase in text and excluded not in text
    assert all(i['source_binding_state']=='source_bound' for i in items)
    assert all(i['source_field_roles']==['layers'] for i in items if i['column_index']==2)
    assert all(c['unit'] is None for i in items for f in i['fragments'] for c in f['thickness_candidates'])


def test_real_ruled_catalog_directs_second_object_read(frozen):
    index=frozen[TABLE_SNAPSHOT]
    result=pages(index,'region_2ce04d7bc949ee948f56cba9',view='catalog')
    assert result[0]['relation_basis']=='source_table_identity_catalog'
    assert any(i['region_id']=='region_d08125ccb740d8bdd1a43909' for p in result for i in p['items'])
    assert any(a['tool']=='read_assembly' and a['arguments']['region_id']=='region_d08125ccb740d8bdd1a43909'
               for p in result for a in p['next_actions'])


@pytest.mark.parametrize('rid,code,numbers,phrase,excluded',[
    ('region_593e279bd02789f8e87337f6','楼地面D3',[1,2,3,4],'96厚C25','楼地面D4'),
    ('region_deeebb273a65396d33f13228','顶棚8',[1,2,3,4,5],'5mmFC板','顶棚9'),
    ('region_1e7032565ed6d7219d2bb880','外墙面4',[1,2,3,4,5,6],'侧板改为','架空保温楼板'),
])
def test_real_section_binds_code_layers_without_neighbor_leakage(frozen,rid,code,numbers,phrase,excluded):
    result=pages(frozen[SECTION_SNAPSHOT],rid)
    b=result[0]['source_binding']
    assert b['state']=='source_bound' and b['code']==code and b['layer_numbers']==numbers
    assert b['boundary_evidence'] and b['object_instance_state']=='unverified'
    text=''.join(i['evidence']['quote'] for p in result for i in p['items'])
    assert text.startswith(code) and phrase in text and excluded not in text
    assert 'ROW_ENVELOPE_UNRESOLVED' not in result[0]['diagnostics']
    if code=='楼地面D3':
        assert '现浇混凝土板' not in text  # preceding record's final layer, same parser region
        thickness=[c for p in result for i in p['items'] for f in i['fragments'] for c in f['thickness_candidates'] if c['value']=='96']
        assert thickness and all(c['unit'] is None for c in thickness)
    if code=='外墙面4':
        assert any(f['branch_state']=='nonadditive_alternative' for p in result for i in p['items'] for f in i['fragments'])


def test_real_reference_keeps_layers_separate_and_supplies_exact_target(frozen):
    result=pages(frozen[SECTION_SNAPSHOT],'region_0ef9758be2b9fe92db6db9d0')
    b=result[0]['source_binding']
    assert b['state']=='source_bound' and b['layer_completeness']=='requires_reference_read'
    assert b['layer_numbers']==[1,2,3,4,5,6]
    assert result[0]['reference_reads'][0]['code']=='屋面1'
    assert result[0]['reference_reads'][0]['next_actions'][0]['arguments']['region_id']=='region_166b182f7a3f0b0873224dd5'
    text=''.join(i['evidence']['quote'] for p in result for i in p['items'])
    assert '二、外墙面' not in text and '外墙涂料饰面' not in text


def test_real_open_rail_and_column_end_stay_candidates(frozen):
    for snapshot,rid,diagnostic in [
        (TABLE_SNAPSHOT,'region_69f7a246408d77f73036bd92','ROW_ENDPOINT_GAPS_REQUIRE_REVIEW'),
        (SECTION_SNAPSHOT,'region_64b01541bc9087a6b5c4a5e3','SECTION_END_UNRESOLVED')]:
        result=pages(frozen[snapshot],rid)[0]
        assert result['source_binding']['state']=='candidate'
        assert diagnostic in result['diagnostics']
        assert any(a['tool']=='read_source_context' for a in result['next_actions'])


@pytest.mark.parametrize('text',[
    '材料选用外墙1B（按另图施工）','型号为楼地面D3（样品）','参照顶棚8（图集）',
    '外墙1B型号与既有图相同','3、材料同外墙1B（涂料）','材料同楼板3（材料）',
])
def test_prose_or_model_mentions_are_not_assembly_headers(text):
    assert HEADING.search(text) is None


@pytest.mark.parametrize('text,code',[('楼地面D3（汽车库）','D3'),('外墙面1B：材料','1B'),('顶棚2C（机房）','2C')])
def test_general_alphanumeric_title_codes(text,code):
    assert HEADING.search(text)['code']==code


def synthetic_region(key,text,y):
    return SimpleNamespace(region_id=key,text=text,bbox=[0,y,100,y+1],parts=[SimpleNamespace(bbox=[0,y,100,y+1])])


@pytest.mark.parametrize('body,closed,diagnostic',[
    ('1、砂浆\n3、结构层',True,'LAYER_NUMBER_SEQUENCE_UNRESOLVED'),
    ('1、砂浆\n1、替代砂浆',True,'LAYER_NUMBER_SEQUENCE_UNRESOLVED'),
    ('1、砂浆\n2、结构层',False,'SECTION_END_UNRESOLVED'),
])
def test_missing_duplicate_or_unclosed_sequence_cannot_bind(body,closed,diagnostic):
    title=synthetic_region('t','4、上一构造\n外墙1B（墙）',10)
    rows=[title,synthetic_region('b',body,8)]
    if closed:rows.append(synthetic_region('n','外墙2（墙）',6))
    sources,stop=_bounded_section(rows,title)
    index=SimpleNamespace(evidence=lambda r,a,b:dict(quote=r.text[a:b],text_span=[a,b]))
    binding=_section_binding(index,title,sources,stop)
    assert binding['state']=='candidate' and diagnostic in binding['diagnostics']
    assert sources[0][1]>0


def test_material_prose_does_not_supply_field_role():
    assert field_roles(['部 位'])==['applicability']
    assert field_roles(['材料做法'])==['layers']
    assert field_roles(['厚砂浆用于楼梯部位'])==[]
    assert field_roles(['参照构造编号另图'])==[]


def test_semantic_rows_preserve_merged_identity_and_blank_cells(monkeypatch):
    import cad_agent.semantics.tables as tables
    cells=[]
    def cell(r,c,text,span=1):
        cells.append(dict(cell_id=f'c{r}-{c}',row_index=r,column_index=c,row_span=span,column_span=1,
                          bbox=[c,3-r,c+1,4-r],text=text,entity_ids=[],evidence=[],state='candidate'))
    for c,text in enumerate(['构造编号','适用部位','做法']):cell(0,c,text)
    cell(1,0,'D3',2);cell(1,1,'汽车库');cell(1,2,'1、砂浆（或改用混凝土）')
    cell(2,1,'');cell(2,2,'2、结构层')
    table=dict(table_id='t',layout_id='L',bbox=[0,0,3,3])
    ix=SimpleNamespace(snapshot='s',tables=[table],scene={'texts':[]})
    monkeypatch.setattr(tables,'grid',lambda *args:(cells,3,3,[]))
    out=read_semantic_table(ix,SemanticTable(table_id='t',row_limit=3))
    assert out['header_rows']==[0]
    records=out['records'];assert len(records)==2
    assert records[1]['fields'][0]['continuation_of']=='c1-0'
    assert records[1]['binding']['shared_cell_ids']==['c1-0']
    assert records[1]['fields'][1]['text']=='' and records[1]['fields'][1]['state']=='unknown'
    assert records[1]['binding']['object_instance_state']=='unverified'
    cells[3]['text']='D3-01'
    ix._semantic_grids={}
    assert read_semantic_table(ix,SemanticTable(table_id='t',row_limit=3))['records'][0]['binding']['state']=='source_bound'
    cells[3]['text']='D3\nD4'
    ix._semantic_grids={}
    out=read_semantic_table(ix,SemanticTable(table_id='t',row_limit=3))
    assert out['records'][0]['binding']['state']=='unresolved'
    assert 'MULTIPLE_CODES_IN_IDENTITY_CELL' in out['records'][0]['binding']['diagnostics']


def test_damaged_split_header_does_not_promote_row_candidate():
    index=FrozenIndex(TABLE_SNAPSHOT)
    for text in index.scene['texts']:
        if text['text']=='位' and 1350000<text['origin'][0]<1374000 and -920000<text['origin'][1]<-918000:
            text['text']='材料说明'
    result=read(index,ReadAssembly(region_id='region_2ce04d7bc949ee948f56cba9'))
    assert result['source_binding']['state']=='candidate'
    assert 'HEADER_UNRESOLVED' in result['diagnostics']
