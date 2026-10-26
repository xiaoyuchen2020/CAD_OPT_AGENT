"""Real failure shapes with unrelated values, transforms and source instances."""
import pytest

from cad_agent.analysis.business_tables import extract_building_schedule, extract_planning_metrics, parse_value
from cad_agent.analysis.business_table_contracts import BuildingScheduleRequest, PlanningMetricsRequest
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.source_record_store import verify_sources
from conftest import dxf_bytes


def pages(w, sid, method, schema, index):
    discovery=method(w,sid,schema(),index)
    assert discovery['total']==1
    args=schema(table_id=discovery['candidates'][0]['table_id'])
    result=[]
    while True:
        page=method(w,sid,args,index);verify_sources(page,index);result.append(page)
        if not page.get('has_more'):break
        args=schema(**page['next_actions'][0]['arguments'])
    return result


@pytest.mark.parametrize('scale,offset',[(1,(700,-900)),(1000,(-50000,60000)),(.5,(0,0))])
def test_split_rails_and_small_gap_do_not_merge_rows_or_headers(workspace,tmp_path,scale,offset):
    def build(doc):
        block=doc.blocks.new('unrelated_schedule')
        # Every line is split at a genuine junction; one header column has a
        # short dangling endpoint. Neither division is a merged cell.
        for y in range(0,161,20):
            block.add_line((0,y),(240,y));block.add_line((240,y),(560,y))
        for x in range(0,561,80):
            block.add_line((x,0),(x,60));block.add_line((x,60),(x,159.8 if x==160 else 160))
        headers=['建筑编号','建筑名称','楼层数','基底面积(㎡)','地上建筑面积(㎡)','总建筑面积(㎡)','备注']
        rows=[headers]+[[str(i),f'样本{i}号','-/-2F' if i==6 else str(i+2)+'F',
                        '' if i==6 else str(i*13),'--',str(i*101),'非身份结论'] for i in range(1,7)]+[['','合计','','273','','2121','']]
        for ri,row in enumerate(rows):
            for ci,text in enumerate(row):
                if text:block.add_text(text,dxfattribs={'height':2,'insert':(ci*80+3,147-ri*20)})
        block.add_text('建筑物一览表',dxfattribs={'height':3,'insert':(0,165)})
        doc.modelspace().add_blockref('unrelated_schedule',offset,dxfattribs={'xscale':scale,'yscale':scale})
        noise=doc.blocks.new('background_noise')
        noise.add_text('99999',dxfattribs={'height':2,'insert':(85,127)})
        doc.modelspace().add_blockref('background_noise',offset,dxfattribs={'xscale':scale,'yscale':scale})
    d=workspace.ingest('transformed.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    result=pages(workspace,d['id'],extract_building_schedule,BuildingScheduleRequest,idx)
    records=[r for p in result for r in p['records']]
    assert len(records)==6 and sum(len(p['summary_rows']) for p in result)==1
    for i,record in enumerate(records,1):
        fields={f['field']:f for f in record['fields']}
        assert fields['name']['value']==f'样本{i}号'
        assert fields['storeys']['value']==('-/-2F' if i==6 else str(i+2))
        assert fields['total_building_area']['value']==str(i*101)
        assert fields['footprint_area']['value']==(None if i==6 else str(i*13))
        assert not {'counted_area','excluded_area','height','use'} & fields.keys()
    assert all('99999' not in str(p) for p in result)


def test_headerless_units_notes_and_percent_with_non_target_rows(workspace,tmp_path):
    def build(doc):
        b=doc.blocks.new('metrics')
        rows=[['用地面积','4321','平方米',''],['地下建筑面积','123','平方米','不计容建筑面积'],
              ['地上建筑面积','8400','平方米','计容建筑面积'],['绿地率','27%%%','',''],
              ['机动车停车位','83','个','含无障碍3个'],['地下停车位','71','个',''],
              ['地上停车位','12','个',''],['其他项目','987','平方米','']]
        for y in range(0,161,20):b.add_line((0,y),(320,y))
        for x in range(0,321,80):b.add_line((x,0),(x,160))
        for ri,row in enumerate(rows):
            for ci,text in enumerate(row):
                if text:b.add_text(text,dxfattribs={'height':2,'insert':(ci*80+3,147-ri*20)})
        doc.modelspace().add_blockref('metrics',(0,0))
        doc.modelspace().add_text('1000',dxfattribs={'height':2,'insert':(7,147)})
    d=workspace.ingest('other.dxf',dxf_bytes(tmp_path,build));idx=DrawingIndex(workspace,d['id'])
    result=pages(workspace,d['id'],extract_planning_metrics,PlanningMetricsRequest,idx)
    fields=[f for p in result for f in p['fields']]
    assert len(fields)==7
    assert {f['field']:f['value'] for f in fields if f['field']!='parking_spaces'}=={
        'land_area':'4321','excluded_area':'123','counted_area':'8400','green_ratio':'27'}
    percent=next(f for f in fields if f['field']=='green_ratio')
    assert percent['display_text']=='27%' and percent['raw_text']=='27%%%'
    assert percent['normalizations']==['DXF_LITERAL_PERCENT']
    assert all(f['state']=='observed' for f in fields)
    assert all(p['header_selection']=='row_labels_and_units' for p in result)


def test_ambiguous_headerless_values_are_not_assigned(workspace,tmp_path):
    from test_business_tables import drawing
    d,idx,tid=drawing(workspace,tmp_path,[['用地面积','100','120','㎡'],['绿地面积','20','30','㎡']])
    result=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid),idx)
    assert result['diagnostics']==['HEADER_UNRESOLVED'] and not result['fields']


def test_floor_and_percent_decoding_is_narrow():
    assert parse_value('12F','storeys')==('12','',['层'])
    assert parse_value('-/-2F','storeys')==('-/-2F','storey_pair',['层'])
    assert parse_value('12F','height')[0] is None
    assert parse_value('21%%','green_ratio')[0] is None
    assert parse_value('21%%%','green_ratio')==('21','',['%'])
