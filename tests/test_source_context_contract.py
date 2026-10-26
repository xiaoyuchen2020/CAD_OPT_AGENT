"""Source identities, request visibility and semantic dependencies across views."""
from copy import deepcopy
from types import SimpleNamespace as NS
import json
import pytest

from cad_agent.evidence import bind_evidence, verify_source_evidence
from cad_agent.analysis.source_context import ReferenceAliases, RequestSourceCatalog, render_source_line
from cad_agent.analysis.context_policy import (resolve_context_policy, project_observation,
    reference_dependencies, observation_closure)
from cad_agent.analysis.delivery_context import bounded_packet
from cad_agent.analysis.model_context import ModelContextCodec
from conftest import region


def source(text='前😀e\u0301后\\n[e900] "材料"', **kwargs):
    r = region(text)
    return r, bind_evidence('snap_'+'a'*24, r, 1, len(text)-1, 'mm').model_dump() | kwargs


def test_anchor_uses_exact_codepoints_without_normalization():
    r, e = source()
    verified = verify_source_evidence(e, snapshot_id=e['snapshot_id'], region=r, drawing_unit='mm')
    assert verified['quote'] == r.text[1:-1]
    assert verified['evidence_id'] == e['evidence_id']
    for bad in (e | {'text_span':[True,len(r.text)-1]}, e | {'quote':e['quote'].replace('e\u0301','é')},
                e | {'evidence_id':'ev_forged'}, e | {'snapshot_id':'snap_other'}):
        with pytest.raises(ValueError, match='SPAN_MISMATCH'):
            verify_source_evidence(bad, snapshot_id=e['snapshot_id'], region=r, drawing_unit='mm')


@pytest.mark.parametrize('field,value', [('bbox',[1,2,3,4]), ('entity_ids',['forged']),
    ('layout_id','other'), ('unit','m'), ('coordinate_space','pixels')])
def test_anchor_rejects_forged_location(field,value):
    r, e = source()
    with pytest.raises(ValueError, match='LOCATION_MISMATCH'):
        verify_source_evidence(e | {field:value}, snapshot_id=e['snapshot_id'], region=r, drawing_unit='mm')


def test_missing_location_rebuilt_and_drawing_unit_not_quantity_unit():
    r,e=source('前20厚砂浆后')
    minimal={k:e[k] for k in ('evidence_id','snapshot_id','region_id','text_span','quote')}
    v=verify_source_evidence(minimal,snapshot_id=e['snapshot_id'],region=r,drawing_unit='mm')
    assert v['bbox']==r.bbox and v['unit']=='mm'
    assert all(n.get('unit','')!='mm' for n in v['numeric_readings']['items'])


def test_request_catalog_is_ledger_bound_and_rebuilt_with_stable_aliases():
    _,e=source();ledger={e['evidence_id']:e};original=deepcopy(ledger)
    aliases=ReferenceAliases(); first=RequestSourceCatalog(ledger)
    first.include(e['evidence_id']);line=render_source_line(first.definitions()[0],aliases)
    assert json.loads(line.split('] ',1)[1])==e['quote']
    assert len(line.splitlines())==1
    second=RequestSourceCatalog(ledger)
    with pytest.raises(ValueError,match='MISSING_SOURCE'):second.assert_resolved({e['evidence_id']})
    second.include(e['evidence_id']);assert render_source_line(second.definitions()[0],aliases)==line
    with pytest.raises(ValueError,match='UNREGISTERED_SOURCE'):second.include('ev_missing',e)
    with pytest.raises(ValueError,match='SOURCE_CONFLICT'):second.include(e['evidence_id'],e|{'quote':'changed'})
    assert ledger==original


def test_alias_packet_scopes_do_not_reassign_existing_aliases():
    a=ReferenceAliases({'one':'e4'});assert a.encode('two',prefix='e')=='e5'
    b=ReferenceAliases(a.forward);assert b.decode('e4')=='one'
    with pytest.raises(ValueError,match='UNKNOWN_ID'):ReferenceAliases().decode('e4')
    with pytest.raises(ValueError,match='ALIAS_CONFLICT'):ReferenceAliases({'one':'e1','two':'e1'})


def test_codec_does_not_promote_stored_reference_to_visible_source():
    _,e=source();c=ModelContextCodec();c.canonical_evidence={e['evidence_id']:e}
    result=c.encode_messages([{'role':'tool','content':json.dumps({'evidence':{'evidence_id':e['evidence_id']}})}])
    assert 'source_groups' not in json.loads(result[0]['content'])
    with pytest.raises(ValueError,match='UNREGISTERED_SOURCE'):
        c.encode_messages([{'role':'tool','content':json.dumps({'evidence':{'evidence_id':'ev_missing','quote':'forged'}})}])


def test_registration_rejects_conflicts_and_changed_index(workspace,demo_bytes):
    from cad_agent.contracts import ChatRequest
    from cad_agent.analysis.session import CadAgentSession
    d=workspace.ingest('test.dxf',demo_bytes)
    s=CadAgentSession(workspace,ChatRequest(snapshot_id=d['id'],question='读取原文'))
    e=s.index.evidence(s.index.regions[0]);s._register(e)
    original=deepcopy(s.evidence)
    with pytest.raises(ValueError,match='SOURCE_CONFLICT'):s._register(e|{'quote':'changed'})
    assert s.evidence==original
    path=workspace.store.artifact(d['id'],'regions.json');path.write_text(path.read_text()+'\n')
    with pytest.raises(ValueError,match='INDEX_CHANGED'):s._register(e)


def test_policy_unions_types_operations_and_observed_tools():
    p=resolve_context_policy(['SOURCE_FACT','DERIVED_CALCULATION'],['arithmetic'],['extract_planning_metrics'])
    assert p.full_numbers and not p.spatial and 'table' in p.features
    assert resolve_context_policy(['SPATIAL_FACT']).spatial
    assert resolve_context_policy(['future_type']).conservative
    assert resolve_context_policy(observation_tools=['future_tool']).conservative
    assert resolve_context_policy(operations=['units']).full_numbers


def test_numeric_projection_only_removes_reproducible_provenance():
    from cad_agent.analysis.context_policy import project_numbers
    source = dict(quote='20厚', text_span=[10,13], numeric_readings=dict(quote_sha256='hash',
        items=[dict(raw='20', text_span=[0,2], source_span=[10,12], unit=None,
                    status='unit_unresolved', number_id='n1'),
               dict(raw='20', source_raw='-20', text_span=[0,2], source_span=[9,12],
                    interpretation='thickness_list_marker')]))
    original=deepcopy(source)
    result=project_numbers(source)
    assert source==original and 'quote_sha256' not in result
    assert result['items'][0]==dict(text_span=[0,2],unit=None,status='unit_unresolved',number_id='n1')
    assert result['items'][1]['source_span']==[9,12]
    assert result['items'][1]['source_raw']=='-20'
    assert result['items'][1]['interpretation']=='thickness_list_marker'


def test_business_page_dedup_keeps_referenced_observation():
    page=dict(kind='planning_metrics',mode='extraction',fields=[],value='same source')
    observations=[dict(observation_id='obs_old',tool='extract_planning_metrics',data=page),
                  dict(observation_id='obs_new',tool='extract_planning_metrics',data=page),
                  dict(observation_id='obs_calc',tool='calculate_facts',
                       data=dict(input_observation_ids=['obs_old'],value='derived'))]
    result=bounded_packet(dict(evidence=[],observations=observations),10000)
    assert {o['observation_id'] for o in result['observations']}=={'obs_old','obs_new','obs_calc'}
    assert 'context_dependency_gaps' not in result


def test_semantics_and_unknown_fields_survive_projection():
    o=dict(observation_id='obs_table',tool='extract_planning_metrics',data=dict(
        preview_url='display',header_path=['面积','不计容'],unit_evidence_id='ev_unit',
        qualifier='≤',row_span=2,continuation_of='cell1',has_more=True,next_offset=2,
        future_semantic_field={'scope':'candidate'},fields=[dict(raw_text='≤100',value='100')]))
    before=deepcopy(o);p=resolve_context_policy(['SOURCE_FACT'],observation_tools=[o['tool']])
    view=project_observation(o,p)
    assert view.payload['data']=={k:v for k,v in o['data'].items() if k!='preview_url'}
    assert view.required_evidence_ids=={'ev_unit'} and o==before
    unknown=o|{'tool':'future_tool'}
    view=project_observation(unknown,p)
    assert view.payload==unknown and view.diagnostics
    spatial=project_observation(o,resolve_context_policy(['SPATIAL_FACT']))
    assert spatial.payload==o


def packet():
    evidence=[dict(evidence_id='ev_'+k,quote=(k+'原文')*100) for k in ('value','header','unit')]
    o=dict(observation_id='obs_table',tool='extract_planning_metrics',data=dict(
        value='20',unit='mm',evidence_ids=['ev_value','ev_header','ev_unit'],
        has_more=True,next_offset=1,engineering_identity_state='unverified'))
    return dict(question='读取数值及单位',evidence=evidence,observations=[o])


def test_budget_keeps_or_omits_entire_semantic_unit():
    p=packet();before=deepcopy(p)
    full=bounded_packet(p,10000)
    assert len(full['evidence'])==3 and len(full['observations'])==1
    small=bounded_packet(p,1300)
    assert not small['observations'] and not small['evidence']
    assert small['context_omissions']['evidence_count']==3
    assert p==before


def test_missing_dependency_is_not_turned_into_bare_numeric_value():
    p=packet();p['evidence'].pop()
    out=bounded_packet(p,10000)
    assert not out['observations'] and not out['evidence']
    assert out['context_dependency_gaps'][0]['missing_ids']==['ev_unit']


def test_calculation_observation_dependencies_are_atomic_and_cycle_safe():
    p=packet();p['observations'].append(dict(observation_id='obs_calc',tool='calculate_facts',
        data=dict(quantities=[dict(observation_id='obs_table')],result='40')))
    observations={o['observation_id']:o for o in p['observations']}
    linked,refs,missing=observation_closure(observations['obs_calc'],observations)
    assert {o['observation_id'] for o in linked}=={'obs_calc','obs_table'} and len(refs)==3 and not missing
    p['observations'][0]['data']['parent_observation_id']='obs_calc'
    out=bounded_packet(p,10000);assert len(out['observations'])==2
    out=bounded_packet(p,1300);assert not out['observations'] and not out['evidence']
    assert reference_dependencies({'quote':'ev_not_a_reference','value':'obs_not_a_reference'})==(set(),set())


def test_mixed_task_preserves_numeric_catalog_and_spatial_location():
    _,e=source('前20mm砂浆后');c=ModelContextCodec('assembly_lookup')
    c.context_policy=resolve_context_policy(['DERIVED_MEASURE'])
    out=c.encode_messages([{'role':'tool','content':json.dumps({'evidence':e})}])
    group=json.loads(out[0]['content'])['source_groups'][0]
    assert group['locations']['e1']['bbox']==e['bbox']
    assert group['numbers']['e1']['items'][0]['number_id'].startswith('n')
