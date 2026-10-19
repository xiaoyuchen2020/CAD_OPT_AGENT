import json
from copy import deepcopy
from types import SimpleNamespace
import pytest
from cad_agent.geometry.room_boundary import generate_candidates, RoomBoundaryService, PrepareBoundaries, SelectBoundaries
from cad_agent.geometry.vision import VisionService
from cad_agent.geometry.room_batch import UnitConfirmation
from cad_agent.analysis.agent import AgentRunner
from cad_agent.contracts import ChatRequest
from cad_agent.runtimes.vision import attach_images,mark_images_sent
from test_room_batch import room_fixture


def label(key='room', x=5,y=5):
    return dict(region_id=key,number=1,label_evidence={'bbox':[x-.2,y-.2,x+.2,y+.2]})


def rec(key,points,layer='WALL'):
    return dict(entity_id=key,linear=True,paths=[points],effective_layer=layer,
        bbox=[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)])


def records(gap=False):
    data=[rec('wall',[[0,0],[10,0],[10,10],[0,10],[0,0]])]
    if gap:
        data=[rec('wall',[[0,0],[10,0],[10,10],[6,10]]),rec('wall2',[[4,10],[0,10],[0,0]])]
    return data+[rec('bed',[[4,4],[6,4],[6,6],[4,6],[4,4]],'FURNITURE')]


def test_bed_is_candidate_but_never_eligible():
    item=label();cs=generate_candidates(records(),item,[item],[-1,-1,11,11])
    wall=next(c for c in cs if c['kind']=='closed_wall_face');bed=next(c for c in cs if c['kind']=='furniture_loop')
    assert wall['eligible'] and not bed['eligible']
    assert wall['bbox']==[0,0,10,10] and bed['bbox']==[4,4,6,6]
    assert all(c['area_m2'] is None for c in cs)
    assert 'bed' not in wall['source_entity_ids']


def test_missing_wall_no_rectangle_guess_and_unknown_layer_no_wall():
    item=label()
    assert not generate_candidates([rec('open',[[0,0],[10,0],[10,10]])],item,[item],[-1,-1,11,11])
    hypotheses=generate_candidates([rec('unknown',[[0,0],[10,0],[10,10],[0,10],[0,0]],'0')],item,[item],[-1,-1,11,11])
    assert hypotheses and all(c['area_m2'] is None for c in hypotheses)


def test_explicit_opening_gaps_intrusion_and_multi_labels():
    item=label();r=records(True)+[rec('return',[[0,9],[1,9],[1,10],[0,10],[0,9]])]
    c=generate_candidates(r,item,[item],[-1,-1,11,11])[0]
    assert c['eligible'] and [1.,9.] in c['boundary']
    assert sum(len(e['unclosed_gaps']) for e in c['edge_evidence'])==1
    cs=generate_candidates(r,item,[item,label('other',3,3)],[-1,-1,11,11])
    assert not cs[0]['eligible'] and not cs[0]['checks']['single_batch_label']


def prepare(workspace, monkeypatch):
    service,q,d=room_fixture(workspace);batch=service.run(q)
    # Independent geometry with room label at its real source location.
    import cad_agent.geometry.room_boundary as module
    item=batch['items'][0];a,b,c,e=item['label_evidence']['bbox'];x=(a+c)/2;y=(b+e)/2
    r=[rec('wall',[[x-1000,y-1000],[x+1000,y-1000],[x+1000,y+1000],[x-1000,y+1000],[x-1000,y-1000]]),
       rec('bed',[[x-300,y-300],[x+300,y-300],[x+300,y+300],[x-300,y+300],[x-300,y-300]],'FURNITURE')]
    monkeypatch.setattr(module,'scoped_geometry_index',lambda *a:SimpleNamespace(data={'complete':True},query=lambda *a:r))
    monkeypatch.setattr(module,'source_descriptors',lambda w,sid,rows:dict(effective_layers={r['entity_id']:r['effective_layer'] for r in rows},arc_sources={}))
    bs=RoomBoundaryService(workspace,d['id']);review=bs.prepare(PrepareBoundaries(batch_id=batch['batch_id']))
    return bs,batch,review,d,q


def choices(review,kind='closed_wall_face'):
    return SelectBoundaries(review_id=review['review_id'],choices=[dict(region_id=i['region_id'],decision='select',
        candidate_id=next(c['candidate_id'] for c in i['candidates'] if c['kind']==kind),reason='墙面轮廓包围家具，家具框不是房间边界。') for i in review['items']])


def test_image_binding_integrity_selection_and_unknown_unit(workspace,monkeypatch):
    bs,batch,review,d,q=prepare(workspace,monkeypatch)
    ids={i['image_id'] for i in review['images']};args=choices(review)
    with pytest.raises(ValueError,match='尚未发送'):bs.select(args,set())
    with pytest.raises(ValueError):bs.select(args.model_copy(update={'choices':[]}),ids)
    wrong=deepcopy(args);wrong.choices[0].candidate_id='foreign'
    with pytest.raises(ValueError,match='不属于'):bs.select(wrong,ids)
    r,b=bs.select(args,ids)
    assert r['items'][0]['status']=='visual_boundary_candidate'
    assert b['items'][0]['highlight']['kind']=='candidate_boundary'
    assert b['items'][0]['area_m2'] is None and b['unit']=='unknown'
    assert bs.batches.get(batch['batch_id'])['items'][0]['highlight']['kind']=='label_locator'
    measured=bs.batches.confirm(b['batch_id'],UnitConfirmation(unit='mm',confirmed=True))
    assert measured['items'][0]['highlight']==b['items'][0]['highlight']
    assert measured['items'][0]['candidate_area_m2']==pytest.approx(4)
    assert measured['items'][0]['area_m2'] is None
    r,b=bs.select(choices(review,'furniture_loop'),ids)
    assert r['items'][0]['status']=='unresolved' and b['items'][0]['highlight']['kind']=='label_locator'
    image=review['images'][0];cell=image['cells'][0];x,y=cell['bbox'][:2]
    s,_,_,ns,tx,ty=cell['wcs_to_pixel'];inv,_,_,ni,ix,iy=cell['pixel_to_wcs']
    assert (s*x+tx)*inv+ix==pytest.approx(x)
    assert (ns*y+ty)*ni+iy==pytest.approx(y)
    VisionService(workspace,d['id']).path(image['image_id']).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='integrity'):bs.select(args,ids)


def test_task_does_not_stop_at_label_delivery_and_select_delivers(workspace,monkeypatch):
    bs,batch,review,d,q=prepare(workspace,monkeypatch)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='三层平面图列出试点房间。核查并高亮房间轮廓。',
        mode='agent',runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False))
    s.task.auto_plan();ids=[p['id'] for p in s.task.parts]
    result=json.loads(s.invoke_tool('analyze_room_batch',q.model_dump()|{'task_ids':ids}))
    assert 'error' not in result and s.task.runnable() and not s.policy.stop_reason
    assert 'prepare_room_boundaries' in s.focus.names() and not s.focus.review_ready()
    result=json.loads(s.invoke_tool('prepare_room_boundaries',dict(batch_id=batch['batch_id'],task_ids=ids)))
    assert 'error' not in result
    entry={};payload=attach_images(s,{'input':[]},entry);assert entry['image_count']==1
    mark_images_sent(s,entry)
    value=json.loads(s.invoke_tool('select_room_boundaries',choices(review).model_dump()|{'task_ids':ids}))
    assert 'error' not in value,value
    assert s.focus.review_ready() and list(s.room_batches.values())[0]['boundary_review_id']
    assert s.task.data['subtasks'][-1]['status']=='completed'
    assert batch['batch_id'] not in s.task.data['batches']
    assert all(i['area_m2'] is None for b in s.room_batches.values() for i in b['items'])

def test_combined_boundary_and_units_request_stays_runnable(workspace,monkeypatch):
    bs,batch,review,d,q=prepare(workspace,monkeypatch)
    s=AgentRunner(workspace,ChatRequest(snapshot_id=d['id'],question='核查三层平面图试点房间边界与高亮轮廓，单位未知不算面积',
        mode='agent',runtime='responses_subscription',model_profile='siwc_sol',allow_fallback=False))
    value=json.loads(s.invoke_tool('analyze_room_batch',q.model_dump()))
    assert s.task.runnable() and not s.policy.stop_reason
    assert 'prepare_room_boundaries' in s.focus.names()

def test_delivery_projection_keeps_selected_uncertainty_and_unfinished_reviews():
    from cad_agent.analysis.boundary_context import delivery_observations
    menu={'observation_id':'menu','tool':'prepare_room_boundaries','data':{'image_ids':['image1']}}
    unfinished={'observation_id':'pending','tool':'prepare_room_boundaries','data':{'image_ids':['image2']}}
    selected={'observation_id':'selected','tool':'select_room_boundaries','data':{'boundary_review_complete':True,'image_ids':['image1'],
        'items':[{'number':1,'status':'unresolved','issues':['partial_geometry'],'model_choice':{'reason':'门洞闭合未确认'},
          'candidates':[{'candidate_id':'c','kind':'wall','eligible':True,'gap_count':2,'issues':['finish_face_unconfirmed']}]}]}}
    observations={o['observation_id']:o for o in [menu,unfinished,selected]}
    result=delivery_observations(observations)
    assert [o['observation_id'] for o in result]==['pending','selected']
    assert result[-1]['data']['items'][0]['model_choice']==selected['data']['items'][0]['model_choice']
    assert result[-1]['data']['items'][0]['selected_geometry'][0]['gap_count']==2
    assert 'candidates' in observations['selected']['data']['items'][0]


def test_review_crop_preserves_hypotheses_with_bounded_context():
    from cad_agent.geometry.room_boundary import review_crop
    from shapely.geometry import box
    row=label();row['candidates']=[{'bbox':[0,0,10,10]}]
    crop=review_crop(row)
    assert box(*crop).covers(box(0,0,10,10))
    assert crop[2]-crop[0]==pytest.approx(13)


def test_unavailable_cell_preserves_other_rows_and_cannot_be_selected(workspace,monkeypatch):
    bs,batch,review,d,q=prepare(workspace,monkeypatch)
    unavailable=deepcopy(review)
    unavailable['items'][0]['render_status']='unavailable'
    bs.w.store.put('room_boundary_review',review['review_id'],unavailable)
    r,b=bs.select(choices(review),{i['image_id'] for i in review['images']})
    assert r['items'][0]['status']=='unresolved'
    assert b['items'][0]['highlight']['kind']=='label_locator'


def test_model_choice_numbers_are_not_measurement_evidence():
    from cad_agent.analysis.boundary_context import numeric_evidence
    data={'items':[{'candidate_area_m2':24,'model_choice':{'reason':'我认为面积999.123平方米'}}]}
    result=numeric_evidence(data)
    assert result=={'items':[{'candidate_area_m2':24}]}
    assert 'model_choice' in data['items'][0]
