"""Deterministic source replay; historical runs/snapshots are read-only fixtures."""
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from cad_agent.analysis.assembly import read, ruled_band
from cad_agent.analysis.fact_tools import ReadAssembly
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.tool_registry import TOOL_SPECS
from cad_agent.contracts import SourceRegion
from cad_agent.semantics.contracts import SemanticTable
from cad_agent.semantics.tables import explicit_units, read_semantic_table

FROZEN = Path(__file__).resolve().parents[1] / 'artifacts/designer-facts-latest-20261019'


class FrozenIndex(DrawingIndex):
    """Reuse production scope/evidence/grid code without opening a writable store."""
    def __init__(self, snapshot):
        folder = FROZEN / 'data/drawings' / snapshot
        if not folder.exists():
            pytest.skip('Frozen real-DXF snapshot is not distributed with this checkout')
        self.snapshot = snapshot
        self.regions = [SourceRegion.model_validate(r) for r in json.loads((folder/'regions.json').read_text())]
        self.by_id = {r.region_id:r for r in self.regions}
        self._scene = json.loads((folder/'scene.json').read_text())
        self.drawing = self._scene
        self._tables = None
        self.entity_sources = defaultdict(list)
        for r in self.regions:
            for part in r.parts:
                self.entity_sources[part.entity_id].append((r,part))

    @property
    def scene(self):
        return self._scene


@pytest.fixture(scope='module')
def frozen_indices():
    return {}


def frozen(frozen_indices, case):
    run = json.loads((FROZEN/f'{case}-run.json').read_text())
    snapshot = run['snapshot_id']
    if snapshot not in frozen_indices:
        frozen_indices[snapshot] = FrozenIndex(snapshot)
    return frozen_indices[snapshot],run


def verify_sources(index, value):
    if isinstance(value,dict):
        if 'evidence_id' in value and 'quote' in value:
            region=index.by_id[value['region_id']]
            assert region.text[slice(*value['text_span'])] == value['quote']
            assert value['snapshot_id'] == index.snapshot
            assert set(value['entity_ids']) <= set(region.entity_ids)
            assert value['bbox'] and value['component_boxes']
        for child in value.values():verify_sources(index,child)
    elif isinstance(value,list):
        for child in value:verify_sources(index,child)


def pages(index, region_id, view='record', limit=3):
    args=ReadAssembly(region_id=region_id,view=view,limit=limit)
    output=[]
    for _ in range(100):
        result=read(index,args)
        verify_sources(index,result)
        for action in result['next_actions']:
            TOOL_SPECS[action['tool']][0].model_validate(action['arguments'],strict=True)
        output.append(result)
        if not result['has_more']:
            assert result['next_offset'] is None
            return output
        action=next(a for a in result['next_actions'] if a['tool']=='read_assembly' and a['arguments'].get('offset')==result['next_offset'] and a['arguments'].get('view')==view)
        args=ReadAssembly.model_validate(action['arguments'])
    raise AssertionError('Continuation failed to terminate')


@pytest.mark.parametrize('region_id,total,expected,excluded',[
    ('region_69f7a246408d77f73036bd92',11,'用于阳台或设备平台等局部侧墙','保温砂浆Ⅱ型'),
    ('region_66fe4f4f916d6216811c8851',10,'用于17#楼涂料外墙部分','9个/平方米'),
])
def test_frozen_q06_recovers_body_notes_and_exact_boundaries(frozen_indices,region_id,total,expected,excluded):
    index,run=frozen(frozen_indices,'Q06-A')
    assert any(t['tool']=='read_assembly' for t in run['trace'])
    results=pages(index,region_id)
    assert results[0]['relation_basis']=='source_ruled_band'
    assert results[0]['total']==total
    assert all(r['row_structure']['source_lines'] for r in results)
    assert results[0]['row_structure']['maximum_endpoint_gap'] <= results[0]['row_structure']['endpoint_tolerance']
    assert 'ROW_ENDPOINT_GAPS_REQUIRE_REVIEW' in results[0]['diagnostics']
    items=[i for r in results for i in r['items']]
    quotes=''.join(i['evidence']['quote'].replace('\n','') for i in items)
    assert '5厚抗裂砂浆' in quotes and '10厚DWM15' in quotes
    assert expected in quotes and excluded not in quotes
    assert all(i['ownership_state']=='unresolved' for i in items)
    assert results[-1]['checks']['object_completeness']=='unverified'
    thicknesses=[c for i in items for f in i['fragments'] for c in f['thickness_candidates']]
    assert thicknesses and all(c['unit'] is None for c in thicknesses)


@pytest.mark.parametrize('rid,total,phrase',[
    ('region_2ce04d7bc949ee948f56cba9',7,'非固化沥青'),
    ('region_d08125ccb740d8bdd1a43909',11,'耐根穿刺'),
])
def test_frozen_q05_roof_rows_include_last_layers(frozen_indices,rid,total,phrase):
    index,_=frozen(frozen_indices,'Q05-B')
    result=pages(index,rid)
    assert result[0]['total']==total
    quotes=''.join(i['evidence']['quote'] for r in result for i in r['items'])
    assert phrase in quotes and '结构层' in quotes


def test_frozen_q04_explicit_heading_clips_previous_and_following_assembly(frozen_indices):
    index,_=frozen(frozen_indices,'Q04-B')
    result=pages(index,'region_4563931aa1f6cde0308a5d1b')
    items=[i for r in result for i in r['items']]
    quotes=''.join(i['evidence']['quote'] for i in items)
    assert result[0]['relation_basis']=='explicit_section_candidate'
    assert quotes.startswith('楼地面6') and '楼地面7' not in quotes
    assert '20厚DSM20' in quotes and '1.2厚JS' in quotes
    assert items[0]['text_span'][0]>0


def test_frozen_q07_catalog_paginates_all_explicit_ceiling_headings(frozen_indices):
    index,_=frozen(frozen_indices,'Q07-B')
    result=pages(index,'region_bc9f725d5f1ba8e7ec7803d6',view='catalog',limit=4)
    assert result[0]['total']==15
    titles=[i['evidence']['quote'] for r in result for i in r['items']]
    assert any(t.startswith('顶棚2C') for t in titles) and any(t.startswith('顶棚13') for t in titles)
    record=pages(index,'region_deeebb273a65396d33f13228')
    quotes=''.join(i['evidence']['quote'] for r in record for i in r['items'])
    assert '生活水泵房' in quotes and '5mmFC板' in quotes and '顶棚9' not in quotes


def test_frozen_q02_unruled_labels_stay_unresolved_but_have_catalog(frozen_indices):
    index,_=frozen(frozen_indices,'Q02-B')
    result=read(index,ReadAssembly(region_id='region_a018ffc5e08c071e1caa606c'))
    assert result['row_structure'] is None and result['checks']['ownership']=='unresolved'
    catalog=pages(index,'region_a018ffc5e08c071e1caa606c',view='catalog')
    assert catalog[0]['total']==6
    assert all(i['relation_state']=='candidate' for p in catalog for i in p['items'])


def test_frozen_q20_model_performance_binding_uses_merged_cells_and_headers(frozen_indices):
    index,_=frozen(frozen_indices,'Q20-B')
    args=SemanticTable(table_id='table_c33ceeb4050f9984b5edb3d4',row_limit=5)
    records=[]
    for _ in range(10):
        page=read_semantic_table(index,args)
        verify_sources(index,page)
        assert page['header_rows']==[0]  # Negative floor-number column headings are valid candidates.
        records.extend(page['records'])
        if not page['has_more']:break
        action=page['next_actions'][0]
        args=SemanticTable.model_validate(action['arguments'],strict=True)
    else:raise AssertionError('Table continuation did not terminate')
    codes={}
    for record in records:
        fields={tuple(f['header_path']):f for f in record['fields']}
        code=fields[('设计编号',)]['text']
        if 'FJLM' in code:codes[code]=fields[('备注',)]
    assert len(codes)==11
    assert sum('≥3.0H' in f['text'] for f in codes.values())==9
    assert sum('≥2.0H' in f['text'] for f in codes.values())==2
    assert len({f['cell_id'] for f in codes.values()})==2
    assert all(f['header_evidence'] and f['evidence'] for f in codes.values())
    assert all(r['state']=='candidate' for r in records)
    assert any(r['missing_cell_ids'] for r in records)


def test_fragment_unit_declaration_does_not_inherit_material_parenthesis():
    assert explicit_units('-5厚抗裂砂浆（其上挤浆满铺130g/平方米耐碱网格布）')==[]
    assert explicit_units('洞口尺寸(mm)')==['mm']
    assert explicit_units('0.9m')==['m']


def test_long_source_pagination_is_executable_and_lossless(workspace,demo_bytes):
    from test_factual_workflows import make
    session=make(workspace,demo_bytes)
    original=session.index.regions[0]
    text='-5厚砂浆（改为10mm厚砂浆）\n'*210
    region=original.model_copy(update={'text':text,'parts':[], 'context_region_ids':[]})
    index=SimpleNamespace(snapshot=session.request.snapshot_id,by_id={region.region_id:region},
        scene={'texts':[],'lines':[]},tables=[],drawing=session.index.drawing)
    index.scope=lambda *args:[region]
    index.evidence=lambda r,start=0,end=None:session.index.evidence(r,start,end)
    args=ReadAssembly(region_id=region.region_id,limit=1)
    quotes=[]
    while True:
        result=read(index,args)
        quotes.extend(i['evidence']['quote'] for i in result['items'])
        if args.offset==0:
            fragment=result['items'][0]['fragments'][0]
            assert 'alternative' in fragment['roles']
            assert [c['unit'] for c in fragment['thickness_candidates']]==[None,'mm']
        if not result['has_more']:break
        args=ReadAssembly.model_validate(result['next_actions'][0]['arguments'])
    assert ''.join(quotes)==text
    assert result['checks']['source_sequence_exhausted']


def test_assembly_observation_registered_for_delivery(workspace,demo_bytes):
    from test_factual_workflows import make
    session=make(workspace,demo_bytes)
    result=json.loads(session.invoke_tool('read_assembly',{'region_id':session.index.regions[0].region_id}))
    assert session.observations[result['observation_id']]['tool']=='read_assembly'
    assert session.observations[result['observation_id']]['data']['checks']['object_completeness']=='unverified'


@pytest.mark.parametrize('fault,diagnostic',[
    ('ambiguous','ROW_ENVELOPE_AMBIGUOUS'),
    ('open','ROW_ENVELOPE_UNRESOLVED'),
    ('graphics','ROW_GRAPHICS_OR_SUBROWS_REQUIRE_REVIEW'),
])
def test_geometry_candidates_reject_competing_open_or_graphics_envelopes(fault,diagnostic):
    target=SimpleNamespace(entity_ids=['title'],layout_id='L')
    lines=[]
    def line(a,b):
        lines.append(dict(entity_id=f'line{len(lines)}',layout_id='L',points=[a,b]))
    shift=1 if fault=='open' else 0
    line([shift,60],[100+shift,60]);line([shift,40],[100+shift,40])
    for x in (0,50,100):line([x,40],[x,60])
    if fault=='ambiguous':
        line([10,55],[90,55]);line([10,45],[90,45])
        for x in (10,40,90):line([x,45],[x,55])
    if fault=='graphics':
        for y in range(42,52):line([10,y],[30,y])
    index=SimpleNamespace(scene=dict(lines=lines,texts=[dict(entity_id='title',height=2,origin=[20,50])]))
    band,diagnostics=ruled_band(index,target)
    assert band is None and diagnostic in diagnostics


def test_local_assembly_read_does_not_build_full_drawing_table_catalog(workspace,demo_bytes,monkeypatch):
    from test_factual_workflows import make
    session=make(workspace,demo_bytes)
    def fail():
        raise AssertionError('Local read must not construct all drawing tables')
    monkeypatch.setattr(session.index,'_build_tables',fail)
    result=session.execute('read_assembly',{'region_id':session.index.regions[0].region_id})
    assert result['table_catalog_state']=='not_loaded'
    for action in result['next_actions']:
        TOOL_SPECS[action['tool']][0].model_validate(action['arguments'],strict=True)
