"""Adversarial delivery tests: source segments, per-item coverage and rank frontier."""
from copy import deepcopy
import pytest
from cad_agent.geometry.connection_review import jamb_measurement,ConnectionReviewService,ReviewConnections
from cad_agent.geometry.discovery_review import DiscoveryReviewService,ReviewDiscoveredSpaces
from cad_agent.geometry.vision import VisionService


def segment(eid,a,b):
    return dict(entity_id=eid,linear=True,paths=[[a,b]])

def test_jamb_distance_is_original_segment_not_snapped_face_or_whole_entity():
    records=[segment('left',[.00038,0],[.00038,.24]),segment('right',[1.10039,0],[1.10039,.24])]
    # The same source polyline has an unrelated tail that comes closer: don't
    # use whole-entity minimum distance in place of the chosen jamb segment.
    records[0]['paths'].append([[1.09,4],[1.09,5]])
    m=jamb_measurement([[[0,0],[1.1,0]],[[0,.24],[1.1,.24]]],records,1)
    assert m['value']==1.10001
    assert m['jambs'][0]['points'][0][0]==.00038
    assert m['basis']=='observed_jamb_segment_minimum'
    assert jamb_measurement([[[0,0],[1.1,0]],[[0,.24],[1.1,.24]]],records[:1],1) is None

def test_arc_and_transverse_wall_are_not_jambs():
    r=[segment('wall',[0,0],[1.1,0]),dict(entity_id='arc',linear=False,paths=[[[0,0],[0,.24]]])]
    assert jamb_measurement([[[0,0],[1.1,0]],[[0,.24],[1.1,.24]]],r,1) is None

class Store:
    def put(self,*args):pass
class W:
    store=Store()

def review_service(cls,r,monkeypatch):
    service=object.__new__(cls);service.w=W();service.sid='sid';service.get=lambda rid:deepcopy(r)
    monkeypatch.setattr(VisionService,'__init__',lambda *a:None)
    monkeypatch.setattr(VisionService,'png',lambda *a:b'checked')
    return service

def discovery_record():
    rows=[]
    for n in range(4):
        rows.append(dict(item_id=str(n),number=n,render_status='available',area_m2=4-n,perimeter_m=8,
                         boundary=[[n*5,0],[n*5+1,0],[n*5+1,1],[n*5,1],[n*5,0]],diagnostics=[]))
    return dict(items=rows,images=[{'image_id':'image'}],layout_id='layout',scope_bbox=[0,0,20,4],omitted_larger_count=0,largest_omitted_area_m2=None)

def test_unknown_larger_candidate_prevents_top_three_but_delivers_selected_shapes(monkeypatch):
    r=discovery_record();s=review_service(DiscoveryReviewService,r,monkeypatch)
    args=ReviewDiscoveredSpaces(review_id='review',top_n=3,choices=[dict(item_id=str(i),classification='uncertain' if i==0 else 'space',reason='图像中逐处核查围合情况') for i in range(4)])
    out=s.review(args,{'image'})
    assert not out['ranking_complete']
    assert len(out['highlight_action']['regions'])==3
    assert out['highlight_action']['object_ids']==['1','2','3']
    assert out['selected'][0]['area_m2']==3

def test_review_requires_images_all_rows_and_nonoverlap(monkeypatch):
    r=discovery_record();s=review_service(DiscoveryReviewService,r,monkeypatch)
    args=ReviewDiscoveredSpaces(review_id='review',choices=[dict(item_id=str(i),classification='space',reason='逐处原图中可见围合边界') for i in range(4)])
    with pytest.raises(ValueError,match='实际读取'):s.review(args,set())
    with pytest.raises(ValueError,match='逐项'):s.review(args.model_copy(update={'choices':args.choices[:2]}),{'image'})
    r['items'][1]['boundary']=r['items'][0]['boundary']
    with pytest.raises(ValueError,match='重叠'):s.review(args,{'image'})

def test_selected_regions_are_ranked_and_rejected_furniture_not_highlighted(monkeypatch):
    s=review_service(DiscoveryReviewService,discovery_record(),monkeypatch)
    args=ReviewDiscoveredSpaces(review_id='r',top_n=2,choices=[dict(item_id=str(i),classification='furniture' if i==0 else 'space',reason='原图逐处核对家具及空间') for i in range(4)])
    r=s.review(args,{'image'})
    assert r['ranking_complete'] and r['excluded_count']==1
    assert r['highlight_action']['object_ids']==['1','2']


def test_cannot_approve_door_on_same_side_or_across_another_wall(monkeypatch):
    opening=dict(opening_id='door',opposite_anchor_sides=True,opening_type='door_candidate',visual_door_eligible=True,access_legs_clear=False,dimension_checks=[])
    row=dict(item_id='pair',render_status='available',openings=[opening],barriers=[],probe=[[0,0],[2,0]])
    s=review_service(ConnectionReviewService,dict(items=[row],images=[{'image_id':'image'}],layout_id='l',scope_bbox=[0,0,3,3]),monkeypatch)
    a=ReviewConnections(review_id='r',choices=[dict(item_id='pair',classification='door',opening_ids=['door'],reason='图上门符号位于另一堵墙后')])
    with pytest.raises(ValueError,match='两侧'):s.review(a,{'image'})


def test_paginated_screening_retains_prior_exclusions_and_proves_ranking(monkeypatch):
    from cad_agent.geometry.service import GeometryService
    all_rows=discovery_record()['items'];prior=discovery_record()
    prior.update(items=deepcopy(all_rows[:2]),review_id='prior',discovery_id='discovery',selected=[])
    for row in prior['items']:row['decision']=dict(item_id=row['item_id'],classification='furniture',reason='原图可见这是家具框')
    page=discovery_record();page.update(items=deepcopy(all_rows[2:]),prior_review_id='prior',discovery_id='discovery',offset=2,omitted_larger_count=2,images=[{'image_id':'next'}])
    service=review_service(DiscoveryReviewService,page,monkeypatch)
    service.get=lambda rid:deepcopy(prior if rid=='prior' else page)
    monkeypatch.setattr(GeometryService,'__init__',lambda *args:None)
    monkeypatch.setattr(GeometryService,'get',lambda *args:dict(candidates=[dict(candidate_id=r['item_id'],candidate_shell_area_m2=r['area_m2']) for r in all_rows]))
    result=service.review(ReviewDiscoveredSpaces(review_id='page',top_n=2,choices=[dict(item_id=str(i),classification='space',reason='本页原图空间边界沿墙闭合') for i in (2,3)]),{'next'})
    assert result['ranking_complete'] and result['checked_candidate_count']==4
    assert result['excluded_count']==2 and result['highlight_action']['object_ids']==['2','3']
    assert {i['image_id'] for i in result['images']}=={'image','next'}


def test_native_area_annotation_is_crosscheck_not_replacement_area():
    from cad_agent.geometry.discovery_review import area_annotation_checks
    boundary=[[0,0],[10,0],[10,7],[0,7],[0,0]]
    regions=[dict(region_id='inside',layout_id='model',bbox=[2,2,4,3],text='机房（137.03㎡）'),
             dict(region_id='outside',layout_id='model',bbox=[12,2,13,3],text='128.25m²'),
             dict(region_id='paper',layout_id='paper',bbox=[2,2,4,3],text='128.25m²'),
             dict(region_id='linear',layout_id='model',bbox=[2,2,4,3],text='面积154.05m')]
    checks=area_annotation_checks(boundary,70,regions,'model')
    assert len(checks)==1 and checks[0]['region_id']=='inside'
    assert checks[0]['status']=='unresolved_area_difference'
    assert checks[0]['difference_m2']==-67.03
    assert checks[0]['geometry_area_m2']==70
    assert checks[0]['association_status']=='contained_text_candidate'
