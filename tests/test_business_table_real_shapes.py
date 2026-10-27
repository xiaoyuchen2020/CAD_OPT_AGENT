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


def test_headerless_ordinal_is_never_the_metric_value(workspace,tmp_path):
    from test_business_tables import drawing
    d,idx,tid=drawing(workspace,tmp_path,[['2','总建筑面积','500?','㎡'],
        ['5','容积率','3?',''],['6','建筑占地面积','20?','㎡']])
    result=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid),idx)
    assert result['diagnostics']==['HEADER_UNRESOLVED'] and not result['fields']


def test_business_projection_keeps_entire_page_and_citation_roles(workspace,tmp_path):
    from test_business_tables import drawing
    from cad_agent.analysis.evidence_store import working_result
    from cad_agent.analysis.delivery_context import _reference_ids, bounded_packet
    from cad_agent.analysis.business_table_context import project
    d,idx,tid=drawing(workspace,tmp_path,[['项目','数值','单位'],['绿地率','29%%%',''],['用地面积','731','㎡']])
    raw=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid),idx)
    packet=working_result(raw,{'record_id':'archive_test'},limit=100)
    assert packet['result_projection']=='business_source_page' and 'preview_json' not in packet
    assert [(f['field'],f['value'],f['unit']) for f in packet['fields']]==[
        (f['field'],f['value'],f['unit']) for f in raw['fields']]
    sources={e['evidence_id']:e for e in packet['source_evidence']}
    assert _reference_ids(project(raw))==sources.keys()
    assert packet['fields'][0]['display_text']=='29%'
    assert any(e['quote']=='29%%%' for e in sources.values())
    final=bounded_packet({'observations':[{'observation_id':'obs_test','tool':'extract_planning_metrics','data':raw}],
                          'evidence':list(sources.values())},12000)
    assert len(final['observations'][0]['data']['fields'])==2
    assert _reference_ids(final['observations']) <= {e['evidence_id'] for e in final['evidence']}
    # Runtime checkpoints have already compacted evidence (no entity_ids).
    from cad_agent.analysis.efficiency import compact
    checkpoint=bounded_packet(compact({'observations':[{'observation_id':'obs_test','tool':'extract_planning_metrics','data':raw}],
                          'evidence':list(sources.values())}),12000)
    assert len(checkpoint['observations'][0]['data']['fields'])==2


def test_schedule_projection_bundles_ordinal_name_values_and_units(workspace,tmp_path):
    from test_business_tables import drawing
    from cad_agent.analysis.business_table_context import project, citation_ids
    d,idx,tid=drawing(workspace,tmp_path,[['建筑编号','名称','层数','总建筑面积(㎡)'],
        ['8','独立样本','7F','923.54'],['9','另一建筑','3F','521.43']])
    raw=extract_building_schedule(workspace,d['id'],BuildingScheduleRequest(table_id=tid),idx)
    packet=project(raw,include_sources=True)
    sources={e['evidence_id']:e for e in packet['source_evidence']}
    for original,row in zip(raw['records'],packet['records']):
        assert set(row['evidence_ids'])==set(citation_ids([original['identity'],original['fields']]))
        assert all(eid in sources for eid in row['evidence_ids'])
        ordinal=next(f for f in original['fields'] if f['field']=='ordinal')
        assert set(citation_ids(ordinal)) <= set(row['evidence_ids'])
        # Reprojection at checkpoints retains exactly the same ready references.
    assert [r['evidence_ids'] for r in project(packet)['records']]==[r['evidence_ids'] for r in packet['records']]


def test_repeated_business_reads_never_displace_distinct_pages(workspace,tmp_path):
    import json
    from copy import deepcopy
    from test_business_tables import drawing
    from cad_agent.analysis.business_table_context import project, unique_pages
    from cad_agent.analysis.delivery_context import bounded_packet
    d,idx,tid=drawing(workspace,tmp_path,[['项目','数值','单位'],['用地面积','731','㎡'],['绿地面积','83','㎡']])
    raw=[extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid,row_offset=i,row_limit=1),idx) for i in (1,2)]
    observations=[dict(observation_id=f'obs_{i}',tool='extract_planning_metrics',data=project(p)) for i,p in enumerate(raw)]
    sources={e['evidence_id']:e for p in raw for e in project(p,include_sources=True)['source_evidence']}
    packet=dict(observations=observations,evidence=list(sources.values()))
    budget=len(json.dumps(packet,ensure_ascii=False))+3000
    repeated=deepcopy(observations[0]);repeated['observation_id']='repeat'
    repeated['data']['cache_hit']=True
    packet['observations']=[observations[0]]+[repeated]*12+[observations[1]]
    final=bounded_packet(packet,budget)
    assert {o['data']['row_offset'] for o in final['observations']}=={1,2}
    assert len(final['observations'])==2
    different=deepcopy(repeated);different['data']['fields'][0]['value']='changed'
    assert len(unique_pages([repeated,different]))==2


def test_raw_cell_matrix_preserves_null_missing_spans_and_sources():
    from cad_agent.analysis.business_table_context import pack_cells
    original={'unmapped_cells':[{'text':'raw','row_span':2,'unit':None,'evidence':[{'evidence_id':'one'}]},
                                {'text':'','column_span':3,'evidence':[{'evidence_id':'two'}]}]}
    packed=pack_cells(original)
    matrix=packed['unmapped_cells']
    restored=[{matrix['columns'][i]:row[i] for i in present} for row,present in
              zip(matrix['rows'],matrix['present_columns'])]
    assert restored==original['unmapped_cells']
    assert pack_cells(packed)==packed


def test_requested_business_fields_precede_raw_only_pages_under_budget():
    from cad_agent.analysis.delivery_context import bounded_packet
    observations=[]
    for i in range(6):
        observations.append(dict(observation_id=f'raw_{i}',tool='extract_planning_metrics',data=dict(
            kind='planning_metrics',mode='extraction',table_id='test',row_offset=i,
            unresolved_rows=[{'row_index':i,'cells':[{'text':'raw detail '+str(i)+'x'*2500,'evidence':[]}]}])))
    observations.append(dict(observation_id='wanted',tool='extract_planning_metrics',data=dict(
        kind='planning_metrics',mode='extraction',table_id='test',row_offset=6,
        fields=[{'field':'land_area','value':'357','state':'observed','evidence':[{'evidence_id':'actual'}]}])))
    p=bounded_packet(dict(observations=observations,evidence=[{'evidence_id':'actual','quote':'357'}]),5000)
    assert any(o['observation_id']=='wanted' for o in p['observations'])
    assert p['context_omissions']['observation_count']>0


def test_cited_table_ordinal_binding_does_not_authorize_unrelated_codes(workspace,tmp_path):
    import json
    from test_business_tables import drawing
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.business_table_delivery import cited_ordinals
    from cad_agent.analysis.business_table_context import citation_ids
    from cad_agent.cad.numbers import unsupported_values
    d,idx,tid=drawing(workspace,tmp_path,[['建筑编号','名称','总建筑面积(㎡)'],['8','独立样本','923.54']])
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='提取建筑物一览表'))
    result=json.loads(s.invoke_tool('extract_building_schedule',{'table_id':tid}))
    observation=next(o for o in s.observations.values() if o['data'].get('records'))
    record=observation['data']['records'][0]
    ids=citation_ids(record['fields']);evidence=[s.evidence[eid] for eid in ids]
    bound=cited_ordinals(s.observations,s.evidence,ids)
    assert bound=={'8'}
    assert unsupported_values('建筑编号8，名称独立样本，总建筑面积923.54㎡。',evidence,bound_ordinals=bound)==[]
    assert unsupported_values('序号8',evidence,bound_ordinals=bound)==[]
    assert unsupported_values('建筑编号9',evidence,bound_ordinals=bound)==['9']
    assert unsupported_values('型号8',evidence,bound_ordinals=bound)==['8']
    assert unsupported_values('8轴',evidence,bound_ordinals=bound)==['8']
    assert unsupported_values('厚度8mm',[],bound_ordinals=bound)==['8']
    value_ids=citation_ids(next(f for f in record['fields'] if f['field']=='ordinal')['evidence'])
    assert not cited_ordinals(s.observations,s.evidence,value_ids)
    area_ids=citation_ids(next(f for f in record['fields'] if f['field']=='total_building_area'))
    assert not cited_ordinals(s.observations,s.evidence,area_ids)
    # The actual final-answer validator uses the typed binding too.
    answer=s.validate_answer(json.dumps({'claims':[{'text':'建筑编号8，名称独立样本，总建筑面积923.54㎡。',
        'state':'observed','question_part_ids':['q1'],'evidence_ids':ids}],'missing_information':[]}))
    assert '建筑编号8' in answer['answer']
