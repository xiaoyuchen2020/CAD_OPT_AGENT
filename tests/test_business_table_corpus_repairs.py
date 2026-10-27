"""General failure shapes: no real filenames, coordinates, or expected values."""
import pytest
from cad_agent.analysis.business_tables import (
    extract_building_schedule, extract_planning_metrics, field_key, METRICS)
from cad_agent.analysis.business_table_contracts import BuildingScheduleRequest, PlanningMetricsRequest
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.source_record_store import verify_sources
from conftest import dxf_bytes
from test_business_tables import drawing


def collect(w,idx,method,schema):
    pages=[];discovery=method(w,idx.snapshot,schema(limit=12),idx)
    for c in discovery['candidates']:
        args=schema(table_id=c['table_id'])
        while True:
            p=method(w,idx.snapshot,args,idx);verify_sources(p,idx);pages.append(p)
            if not p.get('has_more'):break
            args=schema(**p['next_actions'][0]['arguments'])
    return discovery,pages


def test_identity_variants_and_separate_storey_height_bases(workspace,tmp_path):
    d,idx,_=drawing(workspace,tmp_path,[['楼号','地上层数','地下层数','规划建筑高度(米)','消防建筑高度(米)','主要功能'],
        ['甲楼','8','2','31.2','30.8','办公'],['乙楼','6','1','25.4','24.9','商业']])
    _,pages=collect(workspace,idx,extract_building_schedule,BuildingScheduleRequest)
    records=[r for p in pages for r in p.get('records',[])]
    assert len(records)==2
    assert all(f['state']=='observed' for r in records for f in r['fields'])
    assert [f['value'] for f in records[0]['fields'] if f['field']=='storeys']==['8','2']
    assert len({tuple(f['qualifiers']) for f in records[0]['fields'] if f['field']=='height'})==2


@pytest.mark.parametrize('scale',[.4,1,800])
def test_nested_grid_split_unit_exponent_and_different_instance(workspace,tmp_path,scale):
    def build(doc):
        m=doc.modelspace();b=doc.blocks.new('small_table')
        for y in (0,20,40,60):b.add_line((0,y),(240,y))
        for x in (0,80,160,240):b.add_line((x,0),(x,60))
        for ri,row in enumerate([['子项','建筑面积','层数'],['甲','327','3'],['乙','684','6']]):
            for ci,t in enumerate(row):b.add_text(t,dxfattribs={'insert':(ci*80+5,47-ri*20),'height':2})
        # Unit is four separate CAD entities on one baseline plus a raised 2.
        for x,t,h,y in [(85,'(',2,43),(87,'m',2,43),(88,'2',1,44),(89,')',2,43)]:
            b.add_text(t,dxfattribs={'insert':(x,y),'height':h})
        m.add_blockref('small_table',(300*scale,200*scale),dxfattribs={'xscale':scale,'yscale':scale})
        # Outer explanation frame must not absorb the internal table.
        for y in (100,180,300):m.add_line((100*scale,y*scale),(600*scale,y*scale))
        for x in (100,250,600):m.add_line((x*scale,100*scale),(x*scale,300*scale))
        m.add_text('说明文字',dxfattribs={'insert':(110*scale,220*scale),'height':2*scale})
    d=workspace.ingest('nested.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    _,ps=collect(workspace,idx,extract_building_schedule,BuildingScheduleRequest)
    rows=[r for p in ps for r in p.get('records',[])]
    assert len(rows)==2
    fs=[f for r in rows for f in r['fields'] if f['field']=='total_building_area']
    assert [(f['value'],f['unit'],f['state']) for f in fs]==[('327','m2','observed'),('684','m2','observed')]


def test_bad_candidate_does_not_abort_valid_table(workspace,tmp_path,monkeypatch):
    import cad_agent.analysis.business_tables as business
    d,idx,tid=drawing(workspace,tmp_path,[['项目','数值','单位'],['用地面积','215','㎡'],['绿地面积','53','㎡']])
    bad=dict(idx.tables[0],table_id='broken-table')
    idx._tables=[bad,*idx.tables]
    original=business._headers
    def fail_one(index,table,kind):
        if table['table_id']=='broken-table':raise ValueError('NON_RECTANGULAR_CELL_COMPONENT')
        return original(index,table,kind)
    monkeypatch.setattr(business,'_headers',fail_one)
    discovery,ps=collect(workspace,idx,extract_planning_metrics,PlanningMetricsRequest)
    assert discovery['diagnostics'][0]['table_id']=='broken-table'
    assert sum(len(p['fields']) for p in ps)==2


def test_explicit_prose_has_source_scope_and_does_not_infer_units(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for y,t in [(60,'总用地面积：357平方米；总建筑面积：864平方米；'),
                    (40,'容积率：2.6；绿地率：32%%%'),
                    (20,'用地面积应不少于100平方米；建筑密度不得超过30%'),
                    (0,'用地面积：123；绿地面积：44')]:
            m.add_text(t,dxfattribs={'insert':(0,y),'height':2})
    d=workspace.ingest('paragraph.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    discovery,ps=collect(workspace,idx,extract_planning_metrics,PlanningMetricsRequest)
    assert discovery['total']==2
    fields=[f for p in ps for f in p['fields']]
    assert {f['value'] for f in fields}=={'357','864','2.6','32'}
    assert all(f['state']=='observed' and f['evidence'] for f in fields)
    assert all(p['source_kind']=='explicit_metric_paragraph' for p in ps)


def test_parking_labels_are_compositional_not_substring_matches():
    assert field_key(['室外非机动车停车位数量'],METRICS)=='parking_spaces'
    assert field_key(['地下装卸车停车泊位'],METRICS)=='parking_spaces'
    assert field_key(['不计停车位配建指标'],METRICS) is None
    assert field_key(['非机动车停车区'],METRICS) is None


def test_material_identity_columns_are_not_a_building_schedule(workspace,tmp_path):
    d,idx,_=drawing(workspace,tmp_path,[['楼号','编号','名称'],['甲楼','A','屋面做法'],['乙楼','B','楼面做法']])
    discovery,ps=collect(workspace,idx,extract_building_schedule,BuildingScheduleRequest)
    assert not discovery['candidates'] and not ps


def test_single_declaration_units_qualifiers_and_normative_negatives(workspace,tmp_path):
    def build(doc):
        for i,t in enumerate(['本工程总用地面积约360平方米；',
            '建筑密度：24%（一期二期统算）；',
            '建筑面积10万平方米（含）以上的工程应执行规定。',
            '太阳能光伏板总面积为300平方米。',
            '不少于住宅建筑面积50%应实施装配式建造。',
            '容积率1.2~1.8；', '地下车位100%预留充电桩。']):
            doc.modelspace().add_text(t,dxfattribs={'insert':(0,i*20),'height':2})
    d=workspace.ingest('declarations.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    _,ps=collect(workspace,idx,extract_planning_metrics,PlanningMetricsRequest)
    fs=[f for p in ps for f in p['fields']]
    assert [(f['field'],f['value'],f['qualifier']) for f in fs]==[
        ('land_area','360','约'),('building_density','24','')]
    assert any('一期二期统算' in q for q in fs[1]['qualifiers'])


def test_header_children_and_small_left_overhang_remain_source_bound(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        for y in (0,20,40,60):m.add_line((0,y),(240,y))
        for x in (0,80,160,240):m.add_line((x,0),(x,60))
        # Height parent has two children; identity header is vertically merged.
        for e in list(m.query('LINE')):
            if tuple(e.dxf.start)[:2]==(0,40):m.delete_entity(e)
        m.add_line((80,40),(240,40))
        m.add_text('楼号',dxfattribs={'insert':(3,47),'height':2})
        m.add_text('建筑总高度(米)',dxfattribs={'insert':(83,47),'height':2})
        m.add_text('建筑层数',dxfattribs={'insert':(163,47),'height':2})
        m.add_text('(从±0.00到屋面高度)',dxfattribs={'insert':(83,27),'height':2})
        for x,t in [(-.3,'甲楼'),(83,'17.5'),(163,'-1F')]:
            m.add_text(t,dxfattribs={'insert':(x,7),'height':2})
    d=workspace.ingest('overhang.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    _,ps=collect(workspace,idx,extract_building_schedule,BuildingScheduleRequest)
    rows=[r for p in ps for r in p.get('records',[])]
    assert len(rows)==1
    fs={f['field']:f for f in rows[0]['fields']}
    assert fs['name']['value']=='甲楼'
    assert fs['storeys']['value']=='-1'
    assert fs['height']['qualifiers'][-1]=='(从±0.00到屋面高度)'


def test_explicit_storey_pair_preserves_the_source_notation():
    from cad_agent.analysis.business_tables import parse_value
    assert parse_value('8F/3D','storeys')==('8F/3D','storey_pair',['层'])
    assert parse_value('3D','storeys')==('3D','below_ground_storeys',['层'])
    assert parse_value('8F/3D','height')[0] is None


def test_complete_child_instance_wins_over_identical_ancestor_envelope(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace();b=doc.blocks.new('owned_grid')
        for y in (0,20,40,60):b.add_line((0,y),(240,y))
        for x in (0,80,160,240):b.add_line((x,0),(x,60))
        for ri,row in enumerate([['项目','数值','单位'],['总用地面积','647','平方米'],['绿地率','28%','']]):
            for ci,t in enumerate(row):
                if t:b.add_text(t,dxfattribs={'insert':(ci*80+3,47-ri*20),'height':2})
        m.add_blockref('owned_grid',(100,200))
        # Root-level line creates an ancestor scan, and root overlay text must
        # never broaden a fully reconstructed child's evidence ownership.
        m.add_line((0,0),(900,0))
        m.add_text('6.28',dxfattribs={'insert':(105,227),'height':1})
        m.add_text('unrelated',dxfattribs={'insert':(110,207),'height':2})
    d=workspace.ingest('ancestry.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    discovery,ps=collect(workspace,idx,extract_planning_metrics,PlanningMetricsRequest)
    assert discovery['total']==1
    assert discovery['candidates'][0]['source_scope']['table_structure']['instance_path']
    fs=[f for p in ps for f in p['fields']]
    assert [(f['field'],f['value']) for f in fs]==[('land_area','647'),('green_ratio','28')]
    assert all('6.28' not in str(f) and 'unrelated' not in str(f) for f in fs)


def test_prose_subscope_is_not_flattened_to_one_conflicting_key(workspace,tmp_path):
    def build(doc):
        m=doc.modelspace()
        m.add_text('屋顶可绿化面积：680平方米，设计屋顶绿化面积：240平方米。',dxfattribs={'insert':(0,20),'height':2})
        m.add_text('标准车位80个，微型车位6个，无障碍车位3个。',dxfattribs={'insert':(0,0),'height':2})
    d=workspace.ingest('scopes.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    _,ps=collect(workspace,idx,extract_planning_metrics,PlanningMetricsRequest)
    fs=[f for p in ps for f in p['fields']]
    assert len(fs)==5 and all(f['state']=='observed' for f in fs)
    assert {f['qualifiers'][0] for f in fs}=={'屋顶可','设计屋顶','标准','微型','无障碍'}
    assert len({tuple(f['qualifiers']) for f in fs})==5
