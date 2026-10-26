"""Actual frozen DXF regression. Model/network-free; no main workspace writes."""
from pathlib import Path
import json
import os
import pytest
from cad_agent.config import Settings
from cad_agent.service import Workspace
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.source_record_workflow import try_run
from scripts.test_source_record_workflow import seed

ROOT=Path(os.environ.get('CAD_REAL_FIXTURES', Path(__file__).resolve().parents[1]))
OUTER='snap_0a8472b8804178ea90914866'
TABLE='snap_387d51baa9bf10359b5cf120'
SECTION='snap_ef95ba940715f9f93396f465'

@pytest.fixture(scope='module')
def real(tmp_path_factory):
    inputs=ROOT/'artifacts/designer-facts-repaired-20261019/inputs'
    external=ROOT/'artifacts/source-record-workflow/prepare-2/data'
    if not inputs.exists() or not external.exists():
        pytest.skip('Frozen real drawing artifacts are not distributed in Git')
    w=Workspace(Settings(data_dir=tmp_path_factory.mktemp('source-record-real'),source_record_workflow=True,provider='responses_subscription'))
    seed(external,w,OUTER)
    for sid in (TABLE,SECTION):seed(inputs,w,sid)
    yield w
    w.close()


def run(w,sid,question,**kw):
    session=CadAgentSession(w,ChatRequest(snapshot_id=sid,question=question,mode='agent',runtime='responses_subscription',model_profile='siwc_sol',**kw))
    result=try_run(session)
    assert result is not None,getattr(session,'source_record_fallback',session.source_record_route)
    for e in result['citations']:
        r=next(r for r in w.regions(e['snapshot_id']) if r.region_id==e['region_id'])
        assert r.text[slice(*e['text_span'])]==e['quote']
    assert set(result['delivery_manifest']['selected_evidence_ids'])=={e for c in result['claims'] for e in c['evidence_ids']}
    return session,result


def test_external_wall_original_gold_and_candidate_states(real):
    session,result=run(real,OUTER,'帮我找下外墙做法')
    records=session.source_record_packet['records']
    assert len(records)==12 and sum(r['ready'] for r in records)==9
    assert len(result['citations'])==100
    assert len([c for c in result['claims'] if c['state']=='unknown'])==3
    assert not any('内墙' in c['text'] for c in result['claims'])
    assert not session.usage
    gold=json.loads((ROOT/'artifacts/source-record-workflow/exterior-gold-before-live.json').read_text())
    by={r['target_region_id']:r for r in records}
    for expected in gold['records']:
        r=by[expected['target_region_id']]
        assert r['ready']==(expected['expected']=='complete_source')
        assert [session.evidence[e]['quote'] for e in r['body_ids']]==expected['quotes']


def test_unnumbered_ruled_table_identity_headers_and_blank_remarks(real):
    session,result=run(real,TABLE,'帮我找下屋面做法')
    records=session.source_record_packet['records']
    assert {r['target_region_id'] for r in records}=={'region_2ce04d7bc949ee948f56cba9','region_d08125ccb740d8bdd1a43909'}
    assert sorted(len(r['items']) for r in records)==[7,11]
    assert all(r['ready'] for r in records)
    assert all(r['binding']['fields'][3]['value_state']=='unknown' for r in records)
    assert all([h['text'] for h in r['binding']['headers']['fields']]==['序号','部位','做法','备注'] for r in records)
    assert not any('屋面检修爬梯' in c['text'] for c in result['claims'])
    assert all('屋' in g['title'] for g in result['source_records']['groups'])


def test_numbered_target_does_not_include_neighbor_inner_wall(real):
    _,result=run(real,SECTION,'列出外墙面4做法')
    assert len(result['source_records']['groups'])==1
    assert '外墙面4' in result['answer'] and '女儿墙内墙' not in result['answer']
    assert len(result['source_records']['excluded'])==4


def test_multiple_parts_and_duplicate_codes_preserve_requirement_ownership(real):
    _,result=run(real,OUTER,'列出外墙1做法；再列出外墙4做法')
    assert {q for c in result['claims'] for q in c['question_part_ids']}=={'q1','q2'}
    assert len(result['source_records']['groups'])==6
    for c in result['claims']:
        assert c['question_part_ids']==(['q1'] if '外墙1' in c['text'] else ['q2'])


def test_cross_drawing_sources_never_share_record_or_evidence_identity(real):
    _,result=run(real,OUTER,'列出选定图纸的外墙做法',related_drawings=[dict(snapshot_id=SECTION)])
    assert len(result['source_records']['groups'])==17
    assert {e['snapshot_id'] for e in result['citations']}=={OUTER,SECTION}
    assert len({g['canonical_record'] for g in result['source_records']['groups']})==17


def test_followup_filters_previous_source_family_and_executes_selection(real):
    _,prior=run(real,OUTER,'帮我找下外墙做法')
    real.store.put('chat_run','prior',dict(result=prior))
    s,result=run(real,OUTER,'只保留含“岩棉”的做法',parent_run_id='prior')
    assert len(result['source_records']['groups'])==7
    assert len(result['source_records']['excluded'])==5
    assert any(t['tool']=='select_source_records' and t['status']=='completed' for t in s.trace)
    assert all('岩棉' in c['text'] for c in result['claims'])


def test_no_match_stays_unknown_not_global_absence(real):
    _,result=run(real,OUTER,'筛选含“不存在的材料词”的外墙做法')
    assert all(c['state']=='unknown' for c in result['claims'])
    assert len(result['source_records']['excluded'])==12
    assert '未据此断言整图不存在' in result['answer']
