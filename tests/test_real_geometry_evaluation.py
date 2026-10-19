import importlib.util
import json
from pathlib import Path
import sys
from hashlib import sha256

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('real_geometry_evaluation',ROOT/'scripts/evaluate_real_geometry.py')
runner=importlib.util.module_from_spec(spec);sys.modules[spec.name]=runner;spec.loader.exec_module(runner)


def opening(key,x,kind='door_candidate'):
    return dict(object_id=key,boundary=[[x,0],[x+10,0],[x+10,10],[x,10],[x,0]],opening_type=kind)


def test_reference_score_one_to_one_and_unscored_predictions():
    samples=[dict(id='a',anchor=[5,5],expected_identity='door'),dict(id='b',anchor=[6,5],expected_identity='door')]
    score=runner.score_annotations([opening('a',0),opening('outside',1000)],samples,distance=1)
    assert score['matched_identity']==1
    assert score['reference_recall']==.5
    assert score['reference_precision']==1
    assert score['unscored_typed_predictions_outside_annotation_coverage']==1


def test_reference_wrong_identity_and_unknown_are_not_correct():
    sample=[dict(id='a',anchor=[5,5],expected_identity='window')]
    wrong=runner.score_annotations([opening('a',0)],sample)
    assert wrong['reference_precision']==wrong['reference_recall']==0
    unresolved=runner.score_annotations([opening('a',0,'unknown')],sample)
    assert unresolved['reference_precision'] is None and unresolved['reference_recall']==0


def test_failed_attempts_remain_in_summary():
    s=runner.summarize([dict(seconds=1,complete=True,contract_pass=True),dict(seconds=10,complete=False,contract_pass=False)])
    assert s['attempts']==2 and s['failures']==1 and s['p95_seconds']==10


def test_real_runner_pipeline_smoke_without_env_or_model(tmp_path,monkeypatch):
    import ezdxf
    import cad_agent.config
    def forbidden():raise AssertionError('Must not read local env')
    monkeypatch.setattr(cad_agent.config,'local_env',forbidden)
    doc=ezdxf.new('R2018');doc.units=4
    model=doc.modelspace()
    for offset in (0,-200):
        size=6000-2*offset
        model.add_lwpolyline([(offset,offset),(offset+size,offset),(offset+size,4000-offset),(offset,4000-offset)],close=True)
    model.add_text('测试',dxfattribs={'insert':(1000,1000),'height':100})
    source=tmp_path/'source.dxf';doc.saveas(source)
    manifest=dict(annotation_source='synthetic_test_only',evidence_class='synthetic',sampling='smoke_not_real_accuracy',cases=[dict(case='smoke',source=str(source),sha256=sha256(source.read_bytes()).hexdigest(),bbox=[-1000,-1000,8000,6000],annotation_samples=[])])
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest))
    result=runner.evaluate(path,tmp_path/'output',backends=('local','spawn'),phases=('cold',),retain_data=True,pagination_limits=('1','default','20'),policies=('legacy','efficient'),require_full_objects=True)
    assert result['imports'][0]['success'],result['imports']
    assert result['summary']['attempts']==2
    assert result['summary']['contract_pass']==2,result['observations']
    assert result['backend_comparison'][0]['identical']
    assert result['observations'][0]['geometry_events']
    assert all(len(r['pagination_variants'])==6 for r in result['observations'])
    assert all(v['full_object_content_and_order_equal'] for r in result['observations'] for v in r['pagination_variants'])
    assert all(v['geometry_calls']==0 for r in result['observations'] for v in r['pagination_variants'])
    # Force delivery-only replay of saved evidence; NoComputeExecutor rejects hidden work.
    for row in result['observations']:row['delivery_complete']=False
    (tmp_path/'output/evaluation.json').write_text(json.dumps(result))
    recovery=runner.recover_delivery(tmp_path/'output',path)
    assert len(recovery['observations'])==2
    assert all(r['success'] and not r['geometry_recomputed'] for r in recovery['observations']),recovery


def test_import_failure_counts_all_scheduled_attempts(tmp_path,monkeypatch):
    import cad_agent.config
    monkeypatch.setattr(cad_agent.config,'local_env',lambda: (_ for _ in ()).throw(AssertionError('env forbidden')))
    manifest=dict(annotation_source='test',evidence_class='test',sampling='test',cases=[dict(case='missing',source=str(tmp_path/'absent.dxf'),sha256='0'*64,bbox=[0,0,1,1],annotation_samples=[dict(id='one',anchor=[0,0],expected_identity='door')])])
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest))
    result=runner.evaluate(path,tmp_path/'out',repeats=2,backends=('local',))
    assert result['summary']['attempts']==result['summary']['failures']==6
    assert result['selected_reference_consistency']['local']['reference_recall']==0


def test_duplicate_predictions_reduce_covered_reference_precision():
    samples=[dict(id='a',anchor=[5,5],expected_identity='door')]
    score=runner.score_annotations([opening('a',0),opening('duplicate',0)],samples,distance=1)
    assert score['matched_identity']==1
    assert score['typed_predictions_in_annotation_coverage']==2
    assert score['reference_precision']==.5


def test_pagination_uses_server_offset_not_requested_limit():
    import pytest
    assert runner.next_page_offset(dict(next_offset=4,items=[{},{}],has_more=True),0)==4
    assert runner.next_page_offset(dict(items=[{},{}],has_more=True),5)==7
    with pytest.raises(ValueError,match='did not advance'):
        runner.next_page_offset(dict(next_offset=2,items=[{}],has_more=True),2)
