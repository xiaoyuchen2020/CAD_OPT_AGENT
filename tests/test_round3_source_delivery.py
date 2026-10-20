"""Frozen source replay: citation validity must not imply construction ownership."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cad_agent.analysis.assembly import _bounded_section, _section_binding, read
from cad_agent.analysis.assembly_delivery import delivery_fields, ownership_issues
from cad_agent.analysis.delivery import AnswerValidationError, supersedes_claim
from cad_agent.analysis.delivery_context import bounded_packet, _reference_ids
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.fact_tools import ReadAssembly
from cad_agent.analysis.tool_registry import Claim
from scripts.replay_b03_answers import ReplaySession
from test_b02_assembly_binding import FrozenIndex, SECTION_SNAPSHOT, synthetic_region

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / 'artifacts/designer-facts-unlimited-final-20261020'


def frozen_case(case):
    if not (LATEST/'evaluated-results.json').exists():
        pytest.skip('optional frozen evaluation artifacts unavailable')
    row = next(r for r in json.loads((LATEST/'evaluated-results.json').read_text()) if r['id'] == case)
    folder = Path(row['evidence_path'])
    return (json.loads((folder/'run.json').read_text()),
            json.loads((folder/'task.json').read_text()))


@pytest.mark.parametrize('rid,correct,excluded,diagnostic', [
    ('region_64b01541bc9087a6b5c4a5e3','7、外墙反射隔热涂料层','7、外墙仿石涂料','SECTION_END_UNRESOLVED'),
    ('region_06a8f3c3d653564fea8853e7','2、最薄处20厚C25','以下做法同楼地面9第3~9条','LAYER_NUMBER_SEQUENCE_UNRESOLVED'),
])
def test_latest_frozen_sources_clip_prefix_on_every_page(rid,correct,excluded,diagnostic):
    with patch('test_b02_assembly_binding.FROZEN', LATEST/'inputs'):
        index = FrozenIndex(SECTION_SNAPSHOT)
    args = ReadAssembly(region_id=rid,limit=1)
    pages = []
    while True:
        data = read(index,args); pages.append(data)
        if not data['has_more']:
            break
        args = args.model_copy(update={'offset':data['next_offset']})
    text = ''.join(item['evidence']['quote'] for page in pages for item in page['items'])
    assert correct in text and excluded not in text
    assert all(page['source_binding']['state']=='candidate' for page in pages)
    assert diagnostic in pages[0]['diagnostics']
    assert excluded in pages[0]['source_binding']['excluded_preceding_evidence'][0]['quote']
    for page in pages:
        for item in page['items']:
            e=item['evidence']
            assert index.by_id[e['region_id']].text[slice(*e['text_span'])] == e['quote']


@pytest.mark.parametrize('case', ['Q04-B-luna-high', 'Q10-B-luna-high'])
def test_actual_wrong_attribution_rejected_and_independent_facts_survive(case):
    run, task = frozen_case(case)
    claims = [Claim.model_validate({k:v for k,v in c.items() if k in Claim.model_fields})
              for c in run['result']['claims']]
    assert ownership_issues(claims[3], task['evidence'])
    assert not ownership_issues(claims[0], task['evidence'])
    assert not ownership_issues(claims[1], task['evidence'])
    session = ReplaySession(run, task)
    payload = dumps(dict(claims=[c.model_dump() for c in claims],
                         requirement_outcomes=run['result']['requirement_outcomes']))
    with pytest.raises(AnswerValidationError) as error:
        session.validate_answer(payload)
    assert '来源标题边界冲突' in str(error.value)
    assert claims[3].text not in [c['text'] for c in error.value.validated_claims]
    session.record_validation_issue(error.value)
    recovered = session.recover_valid_claims(payload)
    assert recovered is not None
    assert claims[0].text in recovered['answer']
    assert claims[3].text not in [c['text'] for c in recovered['claims']]
    assert all(o['status'] != 'fulfilled' for o in recovered['requirement_outcomes'])


def source(quote, **kw):
    return dict(evidence_id='e', snapshot_id='s', region_id='r', text_span=[0,len(quote)], quote=quote) | kw


def claim(text):
    return Claim(text=text, state='observed', evidence_ids=['e'], question_part_ids=['q1'])


@pytest.mark.parametrize('text', [
    '外墙面2C做法列有“外墙仿石涂料”。',
    '2C的末层是“外墙仿石涂料”。',
])
def test_generic_codes_and_quoted_prefix_are_rejected(text):
    e = source('8、外墙仿石涂料\n外墙面2C：涂料饰面\n1、基层')
    assert ownership_issues(claim(text), {'e':e})


@pytest.mark.parametrize('text', [
    '外墙面2C标题前出现“外墙仿石涂料”，不能归给该做法。',
    '外墙面2C不属于外墙仿石涂料做法。',
    '外墙面2C标题为“涂料饰面”，该标题后的原文为“1、基层”。',
    '8、外墙仿石涂料\n外墙面2C：涂料饰面\n1、基层',
    '图纸出现“外墙仿石涂料”，具体做法归属未确认。',
])
def test_literal_reports_correct_fields_and_explicit_disclaimers_survive(text):
    e = source('8、外墙仿石涂料\n外墙面2C：涂料饰面\n1、基层')
    assert not ownership_issues(claim(text), {'e':e})


def test_containing_read_can_guard_fragment_but_never_other_snapshot_region_or_page():
    whole = source('8、外墙仿石涂料\n外墙面2C：涂料饰面', evidence_id='whole')
    fragment = source('外墙仿石涂料', text_span=[2,9])
    bad = claim('外墙面2C做法列有“外墙仿石涂料”。')
    assert ownership_issues(bad, {'e':fragment, 'whole':whole})
    for change in ({'snapshot_id':'other'}, {'region_id':'other'}, {'text_span':[90,97]}):
        assert not ownership_issues(bad, {'e':fragment|change, 'whole':whole})
    after = source('外墙面2C：涂料饰面\n8、外墙仿石涂料')
    assert not ownership_issues(bad, {'e':after})
    repeated = source('8、外墙仿石涂料\n外墙面2C：涂料饰面\n1、外墙仿石涂料')
    assert not ownership_issues(bad, {'e':repeated})


@pytest.mark.parametrize('scope', ['layout_id','frame_id','column_key'])
def test_section_cannot_cross_layout_frame_or_column(scope):
    title = synthetic_region('title', '外墙1B（墙）', 10)
    body = synthetic_region('body', '1、砂浆\n2、基层', 8)
    next_title = synthetic_region('next', '外墙2（墙）', 6)
    for region in (title,body,next_title):
        region.layout_id='L'; region.frame_id='F'; region.column_key='C'
    setattr(body,scope,'other')
    sources,stop = _bounded_section([title,body,next_title],title)
    assert [r.region_id for r, *_ in sources] == ['title'] and stop is None
    index=SimpleNamespace(evidence=lambda r,a,b:dict(quote=r.text[a:b],text_span=[a,b]))
    assert _section_binding(index,title,sources,stop)['state']=='candidate'


def collect_evidence(value, result):
    if isinstance(value, dict):
        if 'evidence_id' in value and 'quote' in value:
            result[value['evidence_id']] = value
        for child in value.values():
            collect_evidence(child,result)
    elif isinstance(value,list):
        for child in value:
            collect_evidence(child,result)


def test_exact_source_fields_are_selectable_and_paginated_without_auto_completion():
    index = FrozenIndex(SECTION_SNAPSHOT)
    data = read(index,ReadAssembly(region_id='region_593e279bd02789f8e87337f6',limit=2))
    evidence = {}; collect_evidence(data,evidence)
    fields = delivery_fields(data,evidence,['q1','q2'])
    assert fields and data['has_more']
    assert len(fields) == len(data['items'])
    for field in fields:
        parsed = Claim.model_validate(field['claim_template'])
        assert parsed.question_part_ids == []
        assert field['candidate_requirement_ids'] == ['q1','q2']
        assert not ownership_issues(parsed,evidence)
        assert field['engineering_applicability_state'] == 'unverified'
    obs = dict(observation_id='o',tool='read_assembly',data=data)
    packet = dict(evidence=list(evidence.values()),observations=[obs])
    steps = [dict(id='q1',observation_ids=['o']),dict(id='q2',observation_ids=['o'])]
    out = bounded_packet(packet,100000,steps)
    assert out['observations'][0]['data']['delivery_fields']
    assert _reference_ids(out['observations'][0]) <= evidence.keys()
    for state in ('candidate','unresolved'):
        candidate = deepcopy(data); candidate['source_binding']['state']=state
        assert delivery_fields(candidate,evidence,['q1']) == []
    assert 'requirement_outcomes' not in out
    run = dict(request={'question':'列出汽车库做法。'},snapshot_id=index.snapshot)
    task = dict(subtasks=[dict(id='q1',requirement='列出汽车库做法。',delivered=False,batch_ids=[])],
                evidence=evidence,observations={'o':obs})
    session = ReplaySession(run,task)
    result = session.validate_answer(dumps(dict(
        claims=[f['claim_template'] | {'question_part_ids':['q1']} for f in fields],
        requirement_outcomes=[dict(requirement_id='q1',status='partial',gap_kind='unread_source',reason='还有未读层次。')])))
    assert result['claims'] and result['requirement_outcomes'][0]['status']=='partial'


def test_shared_paragraph_does_not_replace_independent_positive_fields():
    first = claim('来源记载材料为砂浆。').model_dump()
    second = claim('来源记载厚度为20mm。').model_dump()
    assert not supersedes_claim(second,first)
    assert supersedes_claim(second | {'text':'修正原结论：厚度20mm。'},first)
    first['evidence_roles']={'subject':['title'],'property':['material']}
    second['evidence_roles']={'subject':['title'],'property':['thickness']}
    assert not supersedes_claim(second | {'state':'unknown','text':'厚度尚未确认。'},first)


def spatial_session(data):
    run = dict(request={'question':'提取空间来源信息。'},snapshot_id='s')
    task = dict(subtasks=[dict(id='q1',requirement='提取空间来源信息。',delivered=False,batch_ids=[])],
                evidence={},observations={'o':dict(observation_id='o',snapshot_id='s',
                                                   tool='a_future_spatial_method',data=data)})
    return ReplaySession(run,task)


def spatial_payload(text,state='derived'):
    return dumps(dict(claims=[dict(text=text,state=state,evidence_basis='tool_observation',
                                   observation_ids=['o'],question_part_ids=['q1'])]))


def test_future_spatial_methods_keep_identity_and_numeric_scope_conservative():
    data = dict(kind='spatial_object',role={'status':'asserted'},
                identity={'engineering_confirmed':False},
                geometry={'bbox':[333,0,400,100]},object_id='object_789',
                measurements={'area_m2':12.5,'unit':'m2','entity_id':'source_456'})
    session = spatial_session(data)
    for text,state in [('对象已绑定。','observed'),('已确认建筑主体。','derived'),
                       ('面积333平方米。','derived'),('面积789平方米。','derived'),
                       ('面积456平方米。','derived'),('面积2.5平方米。','derived'),
                       ('面积2平方米。','derived')]:
        with pytest.raises(AnswerValidationError):
            session.validate_answer(spatial_payload(text,state))
    result = session.validate_answer(spatial_payload('所选来源几何面积12.5平方米，工程对象范围未确认。'))
    assert result['claims'][0]['state']=='derived'
    unmeasured = deepcopy(data); unmeasured.pop('measurements')
    with pytest.raises(AnswerValidationError):
        spatial_session(unmeasured).validate_answer(spatial_payload('所选面积12.5平方米。'))

