"""Real rejected drafts plus cross-number, sign and partial-token controls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from cad_agent.cad.numbers import evidence_readings, readings, unsupported_values
from cad_agent.contracts import SourceRegion
from cad_agent.evidence import bind_evidence
from test_remediation_delivery import read_fact
from test_unified_numbers import source, compute


@pytest.mark.parametrize('reference', ['原编号', '序号', '原序号：', '第'])
@pytest.mark.parametrize('n', [2, 9, 17, 105])
def test_source_item_references_require_the_same_source_ordinal(reference, n):
    text = f'{reference}{n}' + ('条' if reference == '第' else '｜') + '基层'
    assert not unsupported_values(text, [{'quote':f'{n}、基层'}])
    assert unsupported_values(text, [{'quote':f'{n+1}、基层'}]) == [str(n)]
    assert unsupported_values(text, [{'quote':f'厚度{n}mm'}]) == [str(n)]
    with pytest.raises(ValueError):
        compute({'quote':text}, str(n))


@pytest.mark.parametrize('text', ['编号17', '型号17', '原编号17A', '原编号17.5', '原编号17—19'])
def test_identifiers_decimals_and_ranges_do_not_become_list_references(text):
    assert not any(t['kind']=='ordinal_reference' for t in readings(text))
    assert unsupported_values(text, [{'quote':'17、基层\n19、面层'}])


def test_fullwidth_and_multiple_source_item_references():
    assert not unsupported_values('原编号１７、１９｜基层', [{'quote':'17、基层\n19、面层'}])
    assert unsupported_values('原编号１７、２０｜基层', [{'quote':'17、基层\n19、面层'}]) == ['20']


@pytest.mark.parametrize('text', ['17、基层', '（17）基层', '第17项：基层', '１７． 基层'])
def test_separate_ordinal_digits_keep_identity_without_becoming_a_quantity(text):
    r=source(text);digits='１７' if '１７' in text else '17';start=text.index(digits)
    cell=bind_evidence('s',r,start,start+2,'unknown').model_dump()
    whole=bind_evidence('s',r,0,len(text),'unknown').model_dump()
    token=evidence_readings(cell)[0]
    assert token['kind']=='ordinal' and token['number_id']==evidence_readings(whole)[0]['number_id']
    assert not unsupported_values('原编号17｜基层',[cell])
    assert unsupported_values('厚度17mm',[cell])==['17']
    with pytest.raises(ValueError):compute(cell,'17')
    partial=bind_evidence('s',r,start+1,start+2,'unknown').model_dump()
    assert not evidence_readings(partial)
    assert unsupported_values('原编号7｜基层',[partial])==['7']


@pytest.mark.parametrize('value,unit', [('1.5',''), ('12.50','mm'), ('6','厘米')])
def test_quantity_cell_without_list_hyphen_preserves_source_identity(value, unit):
    r=source(f'构造\n-{value}{unit}厚砂浆')
    start=r.text.index(value);end=start+len(value)
    whole=bind_evidence('s',r,0,len(r.text),'unknown').model_dump()
    cell=bind_evidence('s',r,start,end,'unknown').model_dump()
    token=evidence_readings(cell)[0]
    parent=next(t for t in evidence_readings(whole) if t['number_id']==token['number_id'])
    assert token['source_span']==parent['source_span']==[start-1,end]
    assert token['source_raw']=='-'+value and token['raw']==cell['quote']==value
    assert token['text_span']==[0,len(value)]
    assert not unsupported_values(f'厚度为{value}{unit}',[cell])
    assert compute(cell,value,quantity_id=token['number_id'])['status']=='calculated'


@pytest.mark.parametrize('text,start,end,value', [
    ('标高-1.5m',3,6,'1.5'), ('−1.5厚砂浆',1,4,'1.5'),
    ('-12.5厚砂浆',2,5,'2.5'), ('-12.5厚砂浆',1,3,'12'),
    ('型号A-1.5',4,7,'1.5'),
])
def test_partial_numbers_and_true_negative_signs_are_not_reinterpreted(text,start,end,value):
    r=source(text);e=bind_evidence('s',r,start,end,'unknown').model_dump()
    assert not evidence_readings(e)
    assert unsupported_values(f'厚度{value}mm',[e])==[value]
    with pytest.raises(ValueError):compute(e,value)


FIXTURE=json.loads((Path(__file__).parent/'fixtures/numeric-reference-slices.json').read_text())


def test_legacy_pages_with_missing_numeric_readings_are_rebuilt(workspace,tmp_path,monkeypatch):
    from conftest import dxf_bytes
    from cad_agent.analysis import assembly, source_record_store as pages
    from cad_agent.analysis.fact_tools import ReadAssembly
    from cad_agent.analysis.session import CadAgentSession
    from cad_agent.contracts import ChatRequest

    data=dxf_bytes(tmp_path,lambda doc:doc.modelspace().add_mtext('外墙1——测试\n1、5厚砂浆'))
    drawing=workspace.ingest('cache.dxf',data)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='外墙做法'))
    index=session.index
    args=ReadAssembly(region_id=index.regions[0].region_id)
    current_read=assembly.read
    # A legacy page has valid quotes/digests but stale numeric metadata. The
    # ordinary source-span check cannot detect that the binding rules changed.
    def legacy_read(index,args):
        page=current_read(index,args)
        page['title_evidence']['numeric_readings']['items']=[]
        return page
    with monkeypatch.context() as old:
        old.setattr(pages,'VERSION','source-record-store-v1')
        old.setattr(assembly,'read',legacy_read)
        stale=pages.read_page(session,index,args)
        assert not stale['title_evidence']['numeric_readings']['items']
    fresh=pages.read_page(session,index,args)
    assert fresh==current_read(index,args)
    assert fresh['title_evidence']['numeric_readings']['items']
    assert pages.read_page(session,index,args)==fresh
    assert session.source_record_cache['builds']==2
    assert session.source_record_cache['hits']==1


@pytest.mark.parametrize('case', FIXTURE, ids=lambda c:c['case_id'])
def test_real_drafts_survive_full_answer_validation_and_partial_recovery(workspace,demo_bytes,case):
    session,_=read_fact(workspace,demo_bytes)
    session.allowed_snapshots.add(case['snapshot_id'])
    # Publication resolves frame labels from the store; retain the real fixture
    # metadata without importing DXF or calling any external service.
    frames={r['frame_id']:r['frame_name'] for r in case['regions'].values()}
    session.w.store.put('drawing',case['snapshot_id'],{
        'id':case['snapshot_id'],
        'frames':[{'frame_id':fid,'name':name} for fid,name in frames.items()]})
    session.evidence={}
    for eid,ref in case['evidence'].items():
        r=SourceRegion.model_validate(case['regions'][ref['region_id']])
        e=bind_evidence(case['snapshot_id'],r,*ref['text_span'],'mm').model_dump()
        assert e['evidence_id']==eid and e['quote']==ref['quote']
        session.evidence[eid]=e
    claims=deepcopy(case['claims'])
    for claim in claims:
        # This focused replay has one requirement; preserve all source text/IDs.
        claim['question_part_ids']=['q1']
    result=session.validate_answer(json.dumps({'claims':claims}))
    assert [c['text'] for c in result['claims']]==[c['text'] for c in claims]
    assert all(c['text'] in result['answer'] for c in claims)
    bad=deepcopy(claims[0]);bad['text']='厚度为987654mm'
    with pytest.raises(ValueError) as error:
        session.validate_answer(json.dumps({'claims':claims+[bad]}))
    session.record_validation_issue(error.value)
    recovered=session.recover_valid_claims(json.dumps({'claims':claims+[bad]}))
    assert all(c['text'] in recovered['answer'] for c in claims)
    assert '987654' not in recovered['answer']
