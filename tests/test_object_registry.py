"""Object lifecycle checks are pure and do not touch the user's data directory."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from cad_agent.analysis.object_registry import record_tool_result, restore_registry, registry_summary


def session(unit='mm', sid='s'):
    return SimpleNamespace(request=SimpleNamespace(snapshot_id=sid), drawing={'sha256':'sha','unit':unit}, task=SimpleNamespace(data={}), room_batches={})


def page(offset, ids, total=3):
    return dict(measurement_id='discovery', snapshot_id='s', source_sha256='sha', layout_id='l', scope_bbox=[0,0,10,10], applied_unit='mm', offset=offset,total=total,has_more=offset+len(ids)<total, complete=True,items=[dict(candidate_id=i) for i in ids])


def test_duplicate_replay_and_missing_page_survive_restore():
    s=session()
    last=page(2,['c'])
    record_tool_result(s,'discover_rooms',{},last)
    record_tool_result(s,'discover_rooms',{},last)
    assert len(s.task.data['object_registry']['objects'])==1
    assert len(s.task.data['object_registry']['events'])==1
    summary=registry_summary(s.task.data)
    assert summary['collections'][0]['coverage']['enumeration_exhaustive'] is False
    resumed=session()
    assert restore_registry(resumed,s.task.data)
    out=record_tool_result(resumed,'discover_rooms',{},page(0,['a','b']))
    assert out['object_registry']['coverage']==dict(read_count=3,reviewed_count=0,unread_count=0,unreviewed_count=3,enumeration_exhaustive=True,engineering_room_count=None)
    assert out['object_workflow']['stages']['identity_review']=='pending'


def test_source_algorithm_unit_and_snapshot_invalidation():
    s=session();record_tool_result(s,'discover_rooms',{},page(0,['a'],1))
    for key,value in [('drawing_unit','m'),('snapshot_id','other'),('source_sha256','other'),('algorithms',['old'])]:
        old=deepcopy(s.task.data);old['object_registry']['context'][key]=value
        target=session();assert not restore_registry(target,old)
        assert not target.task.data
    s.task.data['unit_policy']={'condition':'explicit','unit':'m'}
    out=record_tool_result(s,'discover_rooms',{},page(2,['c']))
    assert out['object_registry']['coverage']['read_count']==1


def test_foreign_object_rejected_atomically_and_nonobject_tools_untouched():
    s=session();before=deepcopy(s.task.data)
    bad=page(0,['a'],1);bad['items'][0]['snapshot_id']='foreign'
    with pytest.raises(ValueError):record_tool_result(s,'discover_rooms',{},bad)
    assert s.task.data==before
    bad=page(0,['a'],1);bad['snapshot_id']='foreign'
    with pytest.raises(ValueError):record_tool_result(s,'discover_rooms',{},bad)
    assert record_tool_result(s,'search_sources',{},bad)=={}


def test_review_wrong_object_does_not_promote_identity_or_merge_names():
    s=session();record_tool_result(s,'discover_rooms',{},page(0,['a','b'],2))
    review=dict(discovery_id='discovery',review_id='r',layout_id='l',scope_bbox=[0,0,10,10],applied_unit='mm',total=2,
                items=[dict(item_id='a',number=1,label='房间',boundary=[[0,0],[1,0],[1,1],[0,0]],area_m2=1,decision={'classification':'furniture'}),
                       dict(item_id='b',number=2,label='房间',decision={'classification':'uncertain'})])
    out=record_tool_result(s,'review_discovered_spaces',{},review)
    assert out['object_registry']['coverage']['reviewed_count']==2
    objects=list(s.task.data['object_registry']['objects'].values())
    assert len(objects)==2
    assert all(o['identity']['engineering_confirmed'] is False for o in objects)
    assert {o['identity']['review_classification'] for o in objects}=={'furniture','uncertain'}


def test_unit_change_clears_measurement_even_when_anchor_reused():
    s=session();d=page(0,['a'],1);d['items'][0]['area_m2']=1
    record_tool_result(s,'discover_rooms',{},d)
    d['applied_unit']='m';del d['items'][0]['area_m2']
    record_tool_result(s,'discover_rooms',{},d)
    obj=next(iter(s.task.data['object_registry']['objects'].values()))
    assert obj['measurement_basis']['status']=='unknown'


def test_contour_subpagination_does_not_overwrite_room_coverage():
    s=session();first=record_tool_result(s,'discover_rooms',{},page(0,['a','b'],2))
    contours=dict(discovery_id='discovery', candidate_id='a', applied_unit='mm', total=50, offset=0, has_more=True,
                  outer_contour={'boundary':[[0,0],[1,0],[1,1],[0,0]]}, items=[{'contour_id':'inner'}])
    out=record_tool_result(s,'get_room_contours',{},contours)
    assert out['object_registry']['collection_id']==first['object_registry']['collection_id']
    assert out['object_registry']['objects'][0]['object_id']==first['object_registry']['objects'][0]['object_id']
    assert out['object_registry']['coverage']['unread_count']==0
    assert out['object_registry']['coverage']['enumeration_exhaustive'] is True


def test_batch_workflow_reuses_objects_through_boundary_selection():
    s=session()
    batch=dict(batch_id='batch',scope={'layout_id':'l','frame_id':'floor','bbox':[0,0,10,10]},request={'room_label':'房间','offset':0},unit='mm',total=1,has_more=False,
               items=[dict(region_id='r',number=1,label='房间',highlight={'kind':'label_locator'})])
    s.room_batches['batch']=batch
    first=record_tool_result(s,'analyze_room_batch',{},dict(batch_id='batch',items=[{'region_id':'r'}]))
    new=deepcopy(batch);new['batch_id']='selected';new['items'][0].update(candidate_area_m2=1,highlight={'boundary':[[0,0],[1,0],[1,1],[0,0]]})
    s.room_batches['selected']=new
    out=record_tool_result(s,'select_room_boundaries',{'choices':[{'region_id':'r','decision':'select','reason':'supported walls'}]},dict(batch_id='selected',review_id='review',image_ids=['image']))
    assert out['object_registry']['objects']==first['object_registry']['objects']
    assert out['object_workflow']['stages']['boundary']=='candidate'
    assert out['object_registry']['coverage']['reviewed_count']==1
    assert len(s.task.data['object_workflows'])==1
    obj=next(iter(s.task.data['object_registry']['objects'].values()))
    assert obj['visual_reviews'][0]['image_ids']==['image']


def test_duplicate_candidate_in_page_does_not_prove_exhaustion():
    s=session()
    result=record_tool_result(s,'discover_rooms',{},page(0,['a','a'],2))
    coverage=result['object_registry']['coverage']
    assert coverage['read_count']==1 and coverage['unread_count']==1
    assert coverage['enumeration_exhaustive'] is False
