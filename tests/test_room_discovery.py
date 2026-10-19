"""General space discovery: no room vocabulary, known seed or historical oracle."""
import io
import json
import math

import ezdxf
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from shapely.geometry import Polygon

from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.contracts import RoomDiscover, RoomMeasure
from cad_agent.geometry.service import GeometryService


def plan(workspace, names, *, units=6, angle=0, gap=False, column=False):
    doc=ezdxf.new('R2018');doc.units=units;m=doc.modelspace()
    c,s=math.cos(angle),math.sin(angle)
    def pt(x,y):return (x*c-y*s,x*s+y*c)
    for i,name in enumerate(names):
        x=(i%3)*8;y=(i//3)*6
        for xx,yy,w,h in [(x,y,6,4),(x-.2,y-.2,6.4,4.4)]:
            if gap:
                m.add_line(pt(xx,yy),pt(x+2,yy));m.add_line(pt(x+3,yy),pt(xx+w,yy))
            else:m.add_line(pt(xx,yy),pt(xx+w,yy))
            for a,b in [((xx+w,yy),(xx+w,yy+h)),((xx+w,yy+h),(xx,yy+h)),((xx,yy+h),(xx,yy))]:m.add_line(pt(*a),pt(*b))
        if gap:
            for xx in (x+2,x+3):m.add_line(pt(xx,y),pt(xx,y-.2))
        if name:m.add_text(name,dxfattribs={'insert':pt(x+1,y+1),'height':.15})
        if column:m.add_lwpolyline([pt(x+4,y+2),pt(x+5,y+2),pt(x+5,y+3),pt(x+4,y+3)],close=True)
    stream=io.StringIO();doc.write(stream)
    drawing=workspace.ingest('general-spaces.dxf',stream.getvalue().encode())
    service=GeometryService(workspace,drawing['id'])
    return service,RoomDiscover(layout_id=drawing['layouts'][0]['layout_id'],bbox=[-30,-30,40,40]),drawing


def rooms(record):
    return [c for c in record['candidates'] if c['outside_face_fraction']>.999 and abs(c['candidate_shell_area_m2']-24)<.02]


@pytest.mark.parametrize('angle',[0,.37])
def test_arbitrary_and_unnamed_rooms_need_no_seed(workspace,angle):
    names=['卧室','客厅','教室','办公室','卫生间','档案室','走廊','X-203',None]
    service,args,drawing=plan(workspace,names,angle=angle)
    r=service.discover(args)
    found=rooms(r)
    assert len(found)==9
    assert all(c['identity_status']=='unconfirmed_enclosed_space' and c['area_m2'] is None for c in found)
    assert {t['text'] for c in found for t in c['text_regions']}==set(names[:-1])
    assert sum(c['name_status']=='unassigned' for c in found)==1
    assert all(Polygon(c['boundary']).contains(__import__('shapely').geometry.Point(c['seed'])) for c in found)
    assert r['source_sha256']==drawing['sha256']
    assert all(c['entity_ids'] and c['edge_evidence'] for c in found)
    assert service.discover(args)==r
    first=service.discovery_summary(r,0,2);second=service.discovery_summary(r,2,2)
    assert first['has_more'] and len(first['items'])==2
    assert not {c['candidate_id'] for c in first['items']} & {c['candidate_id'] for c in second['items']}
    measured=service.room(RoomMeasure.model_validate(found[0]['measurement_request']))
    assert measured['candidate_area_m2']==pytest.approx(24,abs=.02)


def test_supported_door_openings_and_interior_objects(workspace):
    service,args,_=plan(workspace,['任意标签'],gap=True,column=True)
    found=rooms(service.discover(args))
    assert len(found)==1
    c=found[0]
    assert c['inferred_closures'] and c['area_m2'] is None
    measured=service.room(RoomMeasure.model_validate(c['measurement_request']))
    assert measured['status']=='needs_review'
    assert 'unclassified_interior_closed_objects' in measured['diagnostics']


def test_unknown_unit_invalid_scope_and_unclosed_space(workspace):
    service,args,_=plan(workspace,['Unknown'],units=0)
    assert service.discover(args)['diagnostics']==['unknown_units']
    assert rooms(service.discover(args.model_copy(update={'unit_override':'m'})))
    assert service.discover(args.model_copy(update={'unit_override':'m','bbox':[0,0,201,10]}))['diagnostics']==['discovery_scope_too_large']
    with pytest.raises(ValidationError):RoomDiscover(layout_id='x',bbox=[0,0,float('inf'),2])
    with pytest.raises(ValueError):service.discover(args.model_copy(update={'layout_id':'foreign'}))
    clipped=service.discover(args.model_copy(update={'unit_override':'m','bbox':[1,1,3,3]}))
    assert not clipped['candidates']


def test_annotation_is_never_confirmed_room(workspace):
    from cad_agent.geometry.discovery import discover_enclosures
    opts=RoomDiscover(layout_id='layout',bbox=[-1,-1,8,8])
    records=[dict(entity_id='annotation',linear=True,paths=[[(0,0),(6,0),(6,4),(0,4),(0,0)]])]
    r=discover_enclosures(records,opts.bbox,1,opts)
    assert len(r['candidates'])==1
    assert r['candidates'][0]['area_m2'] is None
    assert 'insufficient_outside_wall_face_support' in r['candidates'][0]['diagnostics']


def test_http_and_agent_discovery(workspace):
    service,args,drawing=plan(workspace,['会议室',None])
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='识别本范围所有房间'))
    result=json.loads(session.invoke_tool('discover_rooms',args.model_dump()))
    assert result['total']>=2 and result['observation_id']
    assert len(rooms(service.get(result['measurement_id'])))==2
    app=create_app(workspace.settings)
    with TestClient(app) as client:
        base=f"/api/drawings/{drawing['id']}"
        reply=client.post(base+'/rooms/discover',json=args.model_dump())
        assert reply.status_code==200,reply.text
        data=reply.json()
        assert data['measurement_id']==result['measurement_id']
        assert client.get(data['evidence_url']).json()['kind']=='room_discovery'
        assert 'Enclosure candidates' in client.get(data['preview_url']).text


def test_irregular_spaces_and_shared_wall_remain_separate():
    from cad_agent.geometry.discovery import discover_enclosures
    from shapely.geometry import box
    shapes=[Polygon([(0,0),(6,0),(6,2),(3,2),(3,5),(0,5)]),box(6.2,0,10.2,2)]
    records=[]
    for i,shape in enumerate(shapes):
        for j,edge in enumerate([shape,shape.buffer(.2,join_style=2)]):
            records.append(dict(entity_id=f'edge-{i}-{j}',linear=True,paths=[list(edge.exterior.coords)]))
    opts=RoomDiscover(layout_id='shared',bbox=[-1,-1,12,7])
    r=discover_enclosures(records,opts.bbox,1,opts)
    supported=[c for c in r['candidates'] if all(e['outside_face_fraction']>=.9 for e in c['edge_evidence'])]
    assert len(supported)==2
    assert sorted(c['candidate_shell_area_m2'] for c in supported)==[8.,21.]
    assert not Polygon(supported[0]['boundary']).overlaps(Polygon(supported[1]['boundary']))


def test_curves_and_segment_budget_are_not_silent_success():
    from cad_agent.geometry.discovery import discover_enclosures
    opts=RoomDiscover(layout_id='bounded',bbox=[-1,-1,10,10])
    curved=[dict(entity_id='arc',linear=False,paths=[[(0,0),(1,1)]])]
    r=discover_enclosures(curved,opts.bbox,1,opts)
    assert not r['complete'] and 'curved_boundaries_not_discovered' in r['diagnostics']
    lines=[dict(entity_id=str(i),linear=True,paths=[[(0,i*.0015),(8,i*.0015)]]) for i in range(5001)]
    r=discover_enclosures(lines,opts.bbox,1,opts)
    assert not r['complete'] and r['diagnostics']==['segment_limit_exceeded']


def test_wall_scale_loops_do_not_displace_rooms_but_are_retained():
    from cad_agent.geometry.discovery import discover_enclosures
    from shapely.geometry import box
    shapes=[box(0,0,6,4),box(8,0,8.02,.02)]
    records=[dict(entity_id=f'{i}-{j}',linear=True,paths=[list(edge.exterior.coords)])
             for i,shape in enumerate(shapes) for j,edge in enumerate([shape,shape.buffer(.1,join_style=2)])]
    opts=RoomDiscover(layout_id='scale',bbox=[-1,-1,10,6])
    r=discover_enclosures(records,opts.bbox,1,opts)
    assert r['candidates'][0]['candidate_role']=='room_candidate'
    assert r['candidates'][0]['candidate_shell_area_m2']==24
    small=[c for c in r['candidates'] if c['candidate_shell_area_m2']<.01]
    assert small and all('wall_scale_enclosure' in c['diagnostics'] for c in small)
    assert all(c['candidate_role']=='enclosure_unconfirmed' for c in small)


@pytest.mark.parametrize('view_kind',['section','elevation','detail','table','legend'])
def test_known_section_does_not_become_plan_room(workspace,view_kind):
    service,args,_=plan(workspace,['卧室','卫生间'])
    result=service.discover(args.model_copy(update={'view_kind':view_kind}))
    assert result['status']=='unresolved'
    assert result['candidates']==[] and result['diagnostics']==['non_plan_view']
    unclassified=service.discover(args)
    assert 'plan_view_unconfirmed' in unclassified['diagnostics']
    assert unclassified['view_kind']=='unknown'


def test_same_room_from_raw_and_paired_graph_is_not_duplicated():
    from cad_agent.geometry.discovery import discover_enclosures
    from shapely.geometry import box
    records=[dict(entity_id=str(i),linear=True,paths=[list(g.exterior.coords)])
             for i,g in enumerate([box(0,0,6,4),box(-.2,-.2,6.2,4.2)])]
    records.append(dict(entity_id='external_chord',linear=True,paths=[[(3,-1),(3,0)]]))
    args=RoomDiscover(layout_id='dedup',bbox=[-2,-2,8,6])
    result=discover_enclosures(records,args.bbox,1,args)
    assert len([c for c in result['candidates'] if c['candidate_shell_area_m2']==24])==1
