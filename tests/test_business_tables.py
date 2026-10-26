"""Real DXF fixtures exercise grid/header/evidence binding and both adapters."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cad_agent.analysis.business_table_contracts import PlanningMetricsRequest, BuildingScheduleRequest
from cad_agent.analysis.business_tables import extract_planning_metrics, extract_building_schedule
from cad_agent.analysis.general import DrawingIndex
from cad_agent.analysis.source_record_store import verify_sources
from cad_agent.analysis.source_record_query import route
from cad_agent.analysis.source_record_workflow import try_run
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.app import create_app
from conftest import dxf_bytes


def drawing(workspace, tmp_path, rows, merge=None, edit=None):
    def build(doc):
        m=doc.modelspace(); n=len(rows); cols=max(map(len,rows))
        for y in range(n+1): m.add_line((0,y*20),(cols*80,y*20))
        for x in range(cols+1): m.add_line((x*80,0),(x*80,n*20))
        if merge:
            # Merge the final two columns of the first header row.
            x=(cols-1)*80
            for e in list(m.query('LINE')):
                if tuple(e.dxf.start)[:2]==(x,0): m.delete_entity(e)
            m.add_line((x,0),(x,(n-1)*20))
        for ri,row in enumerate(rows):
            for ci,text in enumerate(row):
                if text: m.add_text(text,dxfattribs={'insert':(ci*80+3,(n-ri-1)*20+7),'height':2})
        if edit: edit(m)
    d=workspace.ingest('business.dxf',dxf_bytes(tmp_path,build))
    index=DrawingIndex(workspace,d['id'])
    assert index.tables
    return d,index,index.tables[0]['table_id']


def metrics(workspace,tmp_path,rows,**args):
    d,index,tid=drawing(workspace,tmp_path,rows)
    result=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid,**args),index)
    verify_sources(result,index)
    return result,d,index,tid


def test_metrics_values_units_negation_and_no_calculation(workspace,tmp_path):
    rows=[['项目','数值','单位'],['用地面积','1,200.50','㎡'],['计容建筑面积','2400','㎡'],
          ['不计容建筑面积','300','㎡'],['容积率','2','']]
    r,_,_,_=metrics(workspace,tmp_path,rows,requested_fields=['land_area','counted_area','excluded_area','floor_area_ratio'])
    f={v['field']:v for v in r['fields']}
    assert f['land_area']['value']=='1200.50' and f['land_area']['unit']=='m2'
    assert f['counted_area']['value']=='2400' and f['excluded_area']['value']=='300'
    assert f['floor_area_ratio']['unit_state']=='dimensionless'
    assert r['fields_complete'] and r['table_read_complete']
    assert all(x['evidence'] and x['label_evidence'] and x['header_evidence'] for x in f.values())


def test_design_control_percent_and_approximate_are_separate(workspace,tmp_path):
    r,_,_,_=metrics(workspace,tmp_path,[['项目','设计值','控制值','单位'],
        ['绿地率','35','≥30','%'],['建筑密度','约20','≤25','%']],requested_fields=['green_ratio','building_density'])
    assert [f['value_kind'] for f in r['fields']]==['设计值','控制值']*2
    assert all(f['unit']=='%' and f['state']=='observed' for f in r['fields'])
    assert [f['qualifier'] for f in r['fields']]==['','≥','约','≤']


def test_missing_conflicting_units_and_range_preserve_raw(workspace,tmp_path):
    r,_,_,_=metrics(workspace,tmp_path,[['项目','数值(m2)','单位'],
        ['绿地面积','30m','㎡'],['用地面积','','㎡'],['总建筑面积','100~200','㎡']],
        requested_fields=['green_area','land_area','total_building_area','counted_area'])
    assert r['fields'][0]['state']=='conflict'
    assert r['fields'][1]['state']=='missing' and r['fields'][1]['value'] is None
    assert r['fields'][2]['raw_text']=='100~200' and r['fields'][2]['value'] is None
    assert r['missing_fields']==['counted_area'] and not r['fields_complete']


def test_parking_subtypes_and_duplicate_conflict(workspace,tmp_path):
    r,_,_,_=metrics(workspace,tmp_path,[['项目','数量','单位'],['机动车停车位','100','辆'],
        ['非机动车停车位','200','辆'],['机动车停车位','110','辆']],requested_fields=['parking_spaces'])
    assert [f['state'] for f in r['fields']]==['conflict','observed','conflict']
    assert r['fields'][1]['source_labels']==['非机动车停车位']


def test_pagination_cache_and_cross_page_completeness(workspace,tmp_path):
    rows=[['项目','数值','单位']]+[['绿地面积',str(i),'㎡'] for i in range(7)]
    r,d,index,tid=metrics(workspace,tmp_path,rows,requested_fields=['green_area'],row_limit=3)
    assert r['has_more'] and r['next_offset']==3 and not r['table_read_complete']
    r2=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(**r['next_actions'][0]['arguments']),index)
    assert r2['fields'][0]['value']=='2'
    again=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid,requested_fields=['green_area'],row_limit=3),index)
    assert again['cache_hit']
    assert again['fields']==r['fields']
    # Caller mutation cannot poison the canonical page cache.
    again['fields'][0]['evidence'][0]['quote']='tampered'
    assert extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid,row_limit=3),index)['fields'][0]['evidence'][0]['quote']!='tampered'


def test_schedule_merged_headers_and_summary_rows(workspace,tmp_path):
    rows=[['序号','名称','使用性质','层数(层)','建筑高度(m)','建筑面积(m2)',''],
          ['序号','名称','使用性质','层数(层)','建筑高度(m)','计容','不计容'],
          ['1','1号楼','住宅','18','54','10000','200'],
          ['2','2号楼','办公','12','36','','300'],
          ['','合计','','','','10000','500']]
    d,index,tid=drawing(workspace,tmp_path,rows,merge=True)
    req=BuildingScheduleRequest(table_id=tid,requested_fields=['name','use','storeys','height','counted_area','excluded_area'])
    r=extract_building_schedule(workspace,d['id'],req,index)
    records=list(r['records']); summaries=list(r['summary_rows']); current=r
    while current['has_more']:
        current=extract_building_schedule(workspace,d['id'],BuildingScheduleRequest(**current['next_actions'][0]['arguments']),index)
        records.extend(current['records']); summaries.extend(current['summary_rows'])
    assert len(records)==2 and len(summaries)==1
    f={f['field']:f for f in records[0]['fields']}
    assert f['counted_area']['value']=='10000' and f['counted_area']['unit']=='m2'
    assert f['excluded_area']['value']=='200'
    assert f['counted_area']['header_path']==['建筑面积(m2)','计容']
    assert f['height']['value']=='54' and f['height']['unit']=='m'
    assert records[1]['unresolved_fields']==['counted_area']
    assert not records[1]['fields_complete'] and records[0]['engineering_identity_state']=='unverified'
    verify_sources(r,index)


def test_discovery_does_not_extract_or_select_other_table_kind(workspace,tmp_path):
    d,index,_=drawing(workspace,tmp_path,[['项目','数值','单位'],['用地面积','100','㎡'],['绿地率','30','%']])
    r=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(),index)
    assert r['mode']=='discovery' and r['total']==1 and 'fields' not in r
    assert extract_building_schedule(workspace,d['id'],BuildingScheduleRequest(),index)['total']==0
    with pytest.raises(ValueError): extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id='foreign'),index)
    with pytest.raises(ValueError): extract_planning_metrics(workspace,'foreign',PlanningMetricsRequest(),index)


def test_no_header_never_guesses_column_meaning(workspace,tmp_path):
    r,_,_,_=metrics(workspace,tmp_path,[['用地面积','100'],['绿地面积','30']])
    assert r['diagnostics']==['HEADER_UNRESOLVED'] and not r['fields_complete']


@pytest.mark.parametrize('make_request',[lambda:PlanningMetricsRequest(requested_fields=['height']),
    lambda:BuildingScheduleRequest(header_rows=[1]),lambda:BuildingScheduleRequest(row_limit=100)])
def test_invalid_contracts(make_request):
    with pytest.raises(ValueError): make_request()


def test_business_route_distinguishes_read_from_calculation(workspace):
    from cad_agent.analysis.operations import infer_operations
    from cad_agent.analysis.tasks import draft_kind
    def session(q):
        return SimpleNamespace(request=ChatRequest(snapshot_id='s',question=q),w=workspace,
            task=SimpleNamespace(parts=[dict(id='q1',text=q)]))
    s=session('提取总建筑面积及建（构）筑物一览表')
    p=route(s)
    assert p['route']=='business_tables' and p['business_kinds']==['planning_metrics','building_schedule']
    assert try_run(s) is None and '尚未执行任何读取' in s.source_record_handoff
    for q in ['计算容积率','测量建筑基底面积','复核绿地率是否满足规范']:
        assert route(session(q))['route']=='agent'
    assert route(session('找出外墙做法'))['route']=='source_records'
    assert draft_kind('提取总建筑面积')=='lookup'
    assert 'arithmetic' not in infer_operations('读取停车位合计')
    assert 'arithmetic' in infer_operations('读取停车位，然后求和')
    current=session('提取当前图纸的绿地率')
    current.request=current.request.model_copy(update={'related_drawings':[SimpleNamespace(snapshot_id='other')]})
    assert route(current)['snapshot_ids']==['s']
    assert route(session('提取建（构）筑物一览表中的计容面积'))['business_kinds']==['building_schedule']


def test_agent_and_http_entries_register_evidence(workspace,tmp_path):
    d,index,tid=drawing(workspace,tmp_path,[['项目','数值','单位'],['绿地率','30','%'],['用地面积','100','㎡']])
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='提取经济技术指标'))
    r=json.loads(s.invoke_tool('extract_planning_metrics',dict(table_id=tid)))
    assert not r.get('error'),r
    assert r['observation_id'] in s.observations
    assert all(e['evidence_id'] in s.evidence for f in r['fields'] for e in f['evidence'])
    from cad_agent.analysis.property_evidence import query_property_evidence
    properties=query_property_evidence(s)['items']
    assert len(properties)==2
    assert all(p['binding_state']=='source_bound' for p in properties)
    assert next(p for p in properties if p['property_name']=='green_ratio')['unit']=='%'
    from cad_agent.analysis.coverage import record_cells
    cells=[dict(object='绿地率',field='数值',state='unread',evidence_ids=[])]
    canonical=s.observations[r['observation_id']]['data'] | {'observation_id':r['observation_id']}
    record_cells(cells,canonical,s.evidence,tool='extract_planning_metrics',registry=s.evidence,observations=s.observations)
    assert cells[0]['state']=='source_candidate'
    assert cells[0]['state']!='addressed'
    app=create_app(workspace.settings)
    with TestClient(app) as client:
        response=client.post('/api/drawings/'+d['id']+'/business/planning-metrics',json={'table_id':tid})
        assert response.status_code==200,response.text
        assert response.json()['fields'][0]['value']=='30'


def test_horizontal_metrics_keep_row_scope_and_header_units(workspace,tmp_path):
    r,_,_,_=metrics(workspace,tmp_path,[['项目','用地面积(m2)','绿地率(%)'],
        ['一期','1000','30'],['二期','2000','35']],requested_fields=['land_area','green_ratio'])
    assert [f['value'] for f in r['fields']]==['1000','30','2000','35']
    assert [f['qualifiers'] for f in r['fields']]==[['一期'],['一期'],['二期'],['二期']]
    assert all(f['state']=='observed' for f in r['fields'])


def test_merged_building_identity_keeps_same_source_without_blank_fill(workspace,tmp_path):
    def edit(m):
        for e in list(m.query('LINE')):
            if tuple(e.dxf.start)[:2]==(0,20): m.delete_entity(e)
        m.add_line((80,20),(240,20))
    d,index,tid=drawing(workspace,tmp_path,[['名称','使用性质','建筑面积(m2)'],
        ['1号楼','住宅','200'],['','商业','']],edit=edit)
    r=extract_building_schedule(workspace,d['id'],BuildingScheduleRequest(table_id=tid,
        requested_fields=['name','use','total_building_area']),index)
    assert len(r['records'])==2
    a,b=r['records']
    assert a['identity'][0]['cell_id']==b['identity'][0]['cell_id']
    assert b['identity'][0]['continuation_of']==a['identity'][0]['cell_id']
    assert b['fields'][-1]['value'] is None and b['fields'][-1]['state']=='missing'


def test_session_coverage_requires_prefix_and_checks_cross_page_conflicts(workspace,tmp_path):
    from cad_agent.analysis.business_table_ledger import attach_read_coverage
    d,index,tid=drawing(workspace,tmp_path,[['项目','数值','单位'],['用地面积','100','㎡'],
        ['绿地面积','20','㎡'],['用地面积','120','㎡']])
    session=SimpleNamespace(observations={})
    name='extract_planning_metrics'
    def read(offset,limit):
        r=extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid,row_offset=offset,
            row_limit=limit,requested_fields=['land_area','green_area']),index)
        attach_read_coverage(session,name,r)
        session.observations[str(offset)]={'tool':name,'data':r}
        return r
    last=read(3,1)
    assert not last['read_coverage']['table_read_complete']
    first=read(0,3)
    assert first['read_coverage']['table_read_complete']
    assert not first['read_coverage']['fields_complete']
    assert len(first['read_coverage']['conflicting_cells'])==2
    assert first['fields'][0]['state']=='conflict'


def test_stale_index_cache_is_rejected(workspace,tmp_path):
    r,d,index,tid=metrics(workspace,tmp_path,[['项目','数值'],['绿地率','30%'],['容积率','2']])
    path=workspace.store.artifact(d['id'],'regions.json')
    path.write_text(path.read_text()+' ',encoding='utf8')
    with pytest.raises(ValueError,match='SOURCE_RECORD_INDEX_CHANGED'):
        extract_planning_metrics(workspace,d['id'],PlanningMetricsRequest(table_id=tid),index)


def test_storey_subcolumns_and_count_units(workspace,tmp_path):
    d,index,tid=drawing(workspace,tmp_path,[['序号','名称','层数',''],
        ['序号','名称','地上','地下'],['1','1号楼','18','2']],merge=True)
    r=extract_building_schedule(workspace,d['id'],BuildingScheduleRequest(table_id=tid,requested_fields=['name','storeys']),index)
    fields=[f for f in r['records'][0]['fields'] if f['field']=='storeys']
    assert [f['value'] for f in fields]==['18','2'] and all(f['unit']=='层' for f in fields)
    assert all(f['state']=='observed' for f in fields)


def test_selected_related_snapshot_and_observation_provenance(workspace,tmp_path):
    d,index,tid=drawing(workspace,tmp_path,[['项目','数值'],['绿地率','30%'],['容积率','2']])
    other,_,_=drawing(workspace,tmp_path,[['项目','数值'],['绿地率','40%'],['容积率','3']])
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=other['id'],question='提取经济技术指标',
        related_drawings=[{'snapshot_id':d['id']}]))
    r=s.execute('extract_planning_metrics',{'snapshot_id':d['id'],'table_id':tid})
    assert r['snapshot_id']==d['id']
    assert all(e['snapshot_id']==d['id'] for f in r['fields'] for e in f['evidence'])
    from cad_agent.analysis.evidence_ledger import register_observation
    register_observation(s,'extract_planning_metrics',r)
    assert s.observations[r['observation_id']]['snapshot_id']==d['id']
    denied=CadAgentSession(workspace,ChatRequest(snapshot_id=other['id'],question='提取经济技术指标'))
    with pytest.raises(ValueError,match='用户选定'):
        denied.execute('extract_planning_metrics',{'snapshot_id':d['id'],'table_id':tid})


def test_field_projection_rejects_changed_value_and_keeps_qualifier(workspace,tmp_path):
    from cad_agent.analysis.property_evidence import query_property_evidence
    from cad_agent.analysis.evidence_ledger import register_observation
    d,index,tid=drawing(workspace,tmp_path,[['项目','控制值','单位'],['绿地率','≥30','%'],['容积率','2','']])
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='提取经济技术指标'))
    r=s.execute('extract_planning_metrics',{'table_id':tid})
    register_observation(s,'extract_planning_metrics',r)
    projected=query_property_evidence(s,property_name='green_ratio')['items'][0]
    assert projected['value']=='≥30'
    s.observations[r['observation_id']]['data']['fields'][0]['value']='99'
    bad=query_property_evidence(s,property_name='green_ratio')['items'][0]
    assert bad['state']=='unknown' and 'CELL_VALUE_SOURCE_MISMATCH' in bad['diagnostics']


def test_scripted_agent_reads_business_source_and_delivers_percent(workspace,tmp_path,monkeypatch):
    from cad_agent.analysis.agent import AgentRunner
    d,index,tid=drawing(workspace,tmp_path,[['项目','数值','单位'],['绿地率','30','%'],['用地面积','100','㎡']])
    turns=[]
    def turn(messages,tools,timeout):
        turns.append(messages)
        n=len(turns)
        if n<=2:
            args={} if n==1 else {'table_id':tid,'requested_fields':['green_ratio']}
            assert 'extract_planning_metrics' in {t['function']['name'] for t in tools}
            return {'role':'assistant','content':None,'tool_calls':[{'id':'c'+str(n),'type':'function',
                'function':{'name':'extract_planning_metrics','arguments':json.dumps(args)}}]},{}
        data=json.loads(messages[-1]['content'])
        f=data['fields'][0]
        ids=list({e['evidence_id'] for k in ('evidence','label_evidence','header_evidence','unit_evidence') for e in f[k]})
        return {'role':'assistant','content':json.dumps({'claims':[{'text':'表中绿地率为30%。',
            'state':'observed','question_part_ids':['q1'],'evidence_ids':ids}], 'missing_information':[]},ensure_ascii=False)},{}
    monkeypatch.setattr(workspace.gateway,'chat_turn',turn)
    runner=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='提取经济技术指标表中的绿地率',mode='agent'))
    result=runner.run()
    assert runner.source_record_route['route']=='business_tables'
    assert len(turns)==3 and '30%' in result['answer']
    assert [t['tool'] for t in result['trace']]==['extract_planning_metrics']*2
