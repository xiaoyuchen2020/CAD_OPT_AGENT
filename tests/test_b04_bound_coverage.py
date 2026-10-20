"""Only registered same-record source bindings can bridge split evidence."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from cad_agent.analysis.coverage import record_cells
from cad_agent.analysis.recall import pending_calls
from cad_agent.analysis.tasks import TaskLedger

ROOT=Path(__file__).resolve().parents[1]
DETAILS=ROOT/'artifacts/bottleneck-repairs-20261019/b02-replay-details.json'
REAL_CASES=[('roof_noninsulated','保温露台部位','防水层二'),
            ('ceiling_8','生活水泵房','岩棉板'),('garage_d3','楼地面D3','96厚C25')]


def evidence_of(value):
    found={}
    def visit(v):
        if isinstance(v,dict):
            if v.get('evidence_id') and 'quote' in v:found[v['evidence_id']]=deepcopy(v)
            for child in v.values():visit(child)
        elif isinstance(v,list):
            for child in v:visit(child)
    visit(value)
    return found


def frozen_pages(name):
    if not DETAILS.exists():pytest.skip('B02 frozen read receipts unavailable')
    return next(c['pages'] for c in json.loads(DETAILS.read_text()) if c['case']==name)


def register(pages, tool='read_assembly'):
    evidence, observations={},{}
    for n,page in enumerate(pages):
        evidence.update(evidence_of(page))
        oid='obs_'+str(n)
        observations[oid]=dict(observation_id=oid,snapshot_id=page['snapshot_id'],tool=tool,data=deepcopy(page))
    return evidence,observations,pages[-1]|{'observation_id':oid}


def cell(obj,field):return dict(object=obj,field=field,state='unread',evidence_ids=[])


def apply(pages,obj,field,tool='read_assembly',mutate=None):
    evidence,observations,result=register(pages,tool)
    if mutate:mutate(evidence,observations,result)
    cells=[cell(obj,field)]
    record_cells(cells,result,evidence_of(result),tool=tool,registry=evidence,observations=observations)
    return cells[0],evidence


@pytest.mark.parametrize('name,obj,field',REAL_CASES)
def test_real_bound_reads_only_discharge_after_all_record_pages(name,obj,field):
    pages=frozen_pages(name)
    for n in range(1,len(pages)+1):
        c,evidence=apply(pages[:n],obj,field)
        assert (c['state']=='source_candidate')==(n==len(pages))
        assert c['state']!='addressed'
    assert all(eid in evidence for eid in c['evidence_ids'])
    assert any(obj in evidence[e]['quote'] for e in c['evidence_ids'])
    assert any(field in evidence[e]['quote'] for e in c['evidence_ids'])
    # Reading a bound sequence is enough to open review, not final acceptance.
    d=dict(snapshot_id=pages[0]['snapshot_id'],source_sha256='test',subtasks=[dict(id='q1',status='pending',calls=3,coverage=[c])])
    assert not pending_calls(d)


def test_missing_middle_page_and_last_page_only_remain_gaps_even_if_sources_registered():
    pages=frozen_pages('ceiling_8')
    all_sources=evidence_of(pages)
    for selected in ([pages[-1]],[pages[0],pages[-1]]):
        c,_=apply(selected,'生活水泵房','岩棉板',mutate=lambda e,o,r:e.update(all_sources))
        assert c['state']!='source_candidate'


@pytest.mark.parametrize('state',['candidate','unresolved'])
def test_untrusted_binding_state_does_not_fall_back_to_cooccurrence(state):
    pages=frozen_pages('ceiling_8')
    for p in pages:p['source_binding']['state']=state
    c,_=apply(pages,'生活水泵房','燃烧性能等级')
    assert c['state']!='source_candidate'


def test_external_reference_must_not_be_treated_as_fully_read():
    pages=frozen_pages('roof_5_reference')
    assert pages[0]['source_binding']['layer_completeness']=='requires_reference_read'
    c,_=apply(pages,'屋面5','以下做法同楼屋面1')
    assert c['state']!='source_candidate'


def test_neighbor_record_cannot_supply_property_or_complete_missing_page():
    pages=frozen_pages('ceiling_8')
    other=frozen_pages('garage_d3')
    c,_=apply(pages+other,'生活水泵房','96厚C25')
    assert c['state']!='source_candidate'
    c,_=apply(pages[:1]+other+pages[-1:],'生活水泵房','岩棉板')
    assert c['state']!='source_candidate'


@pytest.mark.parametrize('fault',['no_observation','wrong_tool','missing_identity','missing_value','changed_quote','wrong_snapshot','forged_result','unknown_basis','truncated'])
def test_binding_cannot_bypass_registered_receipts_and_exact_source_identity(fault):
    pages=frozen_pages('ceiling_8')
    if fault=='unknown_basis':
        for p in pages:p['source_binding']['basis']='same_column_neighbors'
    if fault=='truncated':pages[-1]['items'][-1]['truncated']=True
    def mutate(e,o,r):
        title=pages[0]['source_binding']['title_evidence']['evidence_id']
        value=next(i['evidence']['evidence_id'] for p in pages for i in p['items'] if '岩棉板' in i['evidence']['quote'])
        if fault=='no_observation':o.clear()
        if fault=='wrong_tool':o[r['observation_id']]['tool']='search_sources'
        if fault=='missing_identity':e.pop(title)
        if fault=='missing_value':e.pop(value)
        if fault=='changed_quote':e[value]['quote']='别的内容'
        if fault=='wrong_snapshot':e[value]['snapshot_id']='another-snapshot'
        if fault=='forged_result':r['source_binding']=dict(state='source_bound',basis='fake')
    c,_=apply(pages,'生活水泵房','岩棉板',mutate=mutate)
    assert c['state']!='source_candidate'


def semantic_table():
    def ev(key,text):
        return dict(evidence_id=key,snapshot_id='s',region_id='r_'+key,text_span=[0,len(text)],quote=text)
    id_header=ev('h_id','编号');header=ev('h_field','厚度')
    identity=ev('id','D3');value=ev('value','100mm')
    def f(cid,text,source,head,path,roles):
        return dict(cell_id=cid,field_roles=roles,header_path=[path],text=text,state='observed',evidence=[source],header_evidence=[head])
    fields=[f('id','D3',identity,id_header,'编号',['code']),f('value','100mm',value,header,'厚度',[])]
    record=dict(row_index=1,binding=dict(state='source_bound',basis='explicit_header_and_source_grid',identity_cell_ids=['id'],diagnostics=[],object_instance_state='unverified'),fields=fields)
    return dict(snapshot_id='s',table_id='table',has_more=False,records=[record],items=[dict(row_index=1,cells=deepcopy(fields))])


def test_explicit_semantic_header_binds_identity_and_observed_value_only():
    page=semantic_table()
    c,evidence=apply([page],'D3','厚度','read_table_semantics')
    assert c['state']=='source_candidate'
    assert set(c['evidence_ids'])=={'id','h_field','value'}
    assert page['records'][0]['binding']['object_instance_state']=='unverified'


@pytest.mark.parametrize('fault',['blank','missing_header','foreign_row','unknown_state','header_value_only','metadata_without_registered_read'])
def test_semantic_blank_unread_or_foreign_field_stays_unresolved(fault):
    page=semantic_table();field=page['records'][0]['fields'][1]
    def mutate(e,o,r):
        if fault=='missing_header':e.pop('h_field')
        if fault=='metadata_without_registered_read':o.clear()
    if fault=='blank':field.update(text='',state='unknown',evidence=[])
    if fault=='foreign_row':page['items'][0]['row_index']=2
    if fault=='unknown_state':field['state']='unknown'
    if fault=='header_value_only':page['records'][0]['fields']=page['records'][0]['fields'][1:]
    c,_=apply([page],'D3','厚度','read_table_semantics',mutate=mutate)
    assert c['state']!='source_candidate'


def test_semantic_other_identity_row_cannot_supply_target_thickness():
    page=semantic_table()
    other=deepcopy(page['records'][0]);other['row_index']=2
    for f in other['fields']:
        f['cell_id']='other_'+f['cell_id']
        for e in f['evidence']:e['evidence_id']='other_'+e['evidence_id'];e['region_id']='other_'+e['region_id']
    other['fields'][0]['text']='D4';other['fields'][0]['evidence'][0]['quote']='D4'
    other['binding']['identity_cell_ids']=['other_id']
    page['records'][0]['fields'][1].update(state='unknown',text='',evidence=[])
    page['items'][0]['cells']=deepcopy(page['records'][0]['fields'])
    page['records'].append(other);page['items'].append(dict(row_index=2,cells=deepcopy(other['fields'])))
    c,_=apply([page],'D3','厚度','read_table_semantics')
    assert c['state']!='source_candidate'


def test_real_header_is_supported_but_generic_material_thickness_grade_are_not_inferred():
    pages=frozen_pages('roof_noninsulated')
    c,_=apply(pages,'保温露台部位','做法')
    assert c['state']=='source_candidate'  # Explicit B02 header is 做法.
    for prop in ('材料','厚度','等级','从面层到结构层'):
        c,_=apply(pages,'保温露台部位',prop)
        assert c['state']!='source_candidate'
    old=json.loads((ROOT/'artifacts/designer-facts-repaired-20261019/attempts/Q05-A/0001/task.json').read_text())
    targets=[c for s in old['subtasks'] for c in s.get('coverage',[])]
    assert targets and all(c['field']=='从面层到结构层' for c in targets)
    for target in targets:
        c,_=apply(pages,target['object'],target['field'])
        assert c['state']!='source_candidate'  # No invented object alias or ordering map.


def test_task_record_hook_uses_registered_source_data_without_completing_task():
    pages=frozen_pages('ceiling_8')
    ledger=TaskLedger.__new__(TaskLedger)
    ledger.data=dict(evidence={},observations={},subtasks=[dict(id='q1',requirement='读取生活水泵房岩棉板原文',
        status='pending',coverage=[cell('生活水泵房','岩棉板')])])
    for n,page in enumerate(pages):
        evidence,observations,result=register(pages[:n+1])
        ledger.data['evidence']=evidence;ledger.data['observations']=observations
        ledger.record_coverage(['q1'],'read_assembly',result,evidence_of(result))
    assert ledger.data['subtasks'][0]['coverage'][0]['state']=='source_candidate'
    assert ledger.data['subtasks'][0]['status']=='pending'


def test_section_end_heading_is_boundary_evidence_not_property_value():
    pages=frozen_pages('ceiling_8')
    assert any('顶棚9' in e['quote'] for e in pages[0]['source_binding']['boundary_evidence'])
    c,_=apply(pages,'生活水泵房','顶棚9')
    assert c['state']!='source_candidate'


def test_completed_row_cannot_claim_unread_other_row_identity():
    page=semantic_table();page['has_more']=True
    c,_=apply([page],'D4','厚度','read_table_semantics')
    assert c['state']!='source_candidate'
