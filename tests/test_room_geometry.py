"""Independent expected geometry; no dependency on production AR030 outputs."""
import io
import json
import math
from xml.etree import ElementTree

import ezdxf
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.geometry.contracts import RoomMeasure, GeometryMeasure, GeometryRead, SpatialQuery
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.source import geometry_index


def ingest(workspace, build=None, *, angle=0, units=4, gap=False, missing=False, hole=False, single=False):
    doc=ezdxf.new('R2018'); doc.units=units
    m=doc.modelspace()
    scale=1000 if units in (4,0) else 1
    c,s=math.cos(angle),math.sin(angle)
    def pt(p):
        return ((p[0]*c-p[1]*s)*scale,(p[0]*s+p[1]*c)*scale)
    def line(a,b):
        m.add_line(pt(a),pt(b))
    for x,y,w,h in [(0,0,6,4)]+([] if single else [(-.2,-.2,6.4,4.4)]):
        if gap:
            line((x,y),(2,y));line((3,y),(x+w,y))
        elif not missing:
            line((x,y),(x+w,y))
        line((x+w,y),(x+w,y+h));line((x+w,y+h),(x,y+h));line((x,y+h),(x,y))
    if gap:
        line((2,0),(2,-.2));line((3,0),(3,-.2))
    if hole:
        m.add_lwpolyline([pt(p) for p in [(4,2),(5,2),(5,3),(4,3)]],close=True)
    m.add_text('试点房间',dxfattribs={'insert':pt((1,1)),'height':.15*scale})
    if build:
        build(doc,pt,line)
    stream=io.StringIO();doc.write(stream)
    drawing=workspace.ingest('room.dxf',stream.getvalue().encode())
    service=GeometryService(workspace,drawing['id'])
    label=next(r for r in workspace.regions(drawing['id']) if '试点房间' in r.text)
    return service,RoomMeasure(region_id=label.region_id,seed=list(pt((1,1)))),drawing


@pytest.mark.parametrize('angle',[0, .37, math.pi/4])
@pytest.mark.parametrize('units',[4,6])
def test_net_room_area_and_source_bound_edges(workspace,angle,units):
    service,args,drawing=ingest(workspace,angle=angle,units=units)
    r=service.room(args)
    assert r['status']=='measured',r['diagnostics']
    assert r['area_m2']==pytest.approx(24,abs=.015)
    assert r['selected']['shell_area_m2']==r['area_m2']
    assert r['source_sha256']==drawing['sha256']
    assert r['algorithm_version']=='room-net-v1.4'
    assert all(e['outside_face_fraction']>=.9 for e in r['selected']['edge_evidence'])
    assert set(r['selected']['entity_ids'])<=set(service.index.by_id)
    assert service.room(args)==r
    # Independent shoelace check, using stored WCS boundary.
    pts=r['selected']['boundary'];factor=service.index.data['unit_to_m']
    area=abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(pts,pts[1:])))/2*factor**2
    assert area==pytest.approx(r['area_m2'],abs=1e-6)
    ElementTree.fromstring(service.svg(r['measurement_id']))


@pytest.mark.parametrize('angle',[0,.37])
def test_opening_requires_both_observed_jambs(workspace,angle):
    service,args,_=ingest(workspace,gap=True,angle=angle)
    r=service.room(args)
    assert r['status']=='measured',r['diagnostics']
    assert r['area_m2']==pytest.approx(24,abs=.015)
    assert r['selected']['inferred_closures']
    assert all(c['entity_ids'] for c in r['selected']['inferred_closures'])
    without=service.room(args.model_copy(update={'close_openings':False}))
    assert without['status']!='measured' and without['area_m2'] is None


def test_missing_wall_not_replaced_by_bbox(workspace):
    service,args,_=ingest(workspace,missing=True)
    r=service.room(args)
    assert r['area_m2'] is None and r['status']=='unresolved'


def test_single_annotation_rectangle_not_verified_as_room(workspace):
    service,args,_=ingest(workspace,single=True)
    r=service.room(args)
    assert r['status']=='needs_review' and r['area_m2'] is None
    assert 'insufficient_outside_wall_face_support' in r['diagnostics']


def test_internal_object_requires_explicit_void_selection(workspace):
    service,args,_=ingest(workspace,hole=True)
    r=service.room(args)
    assert r['status']=='needs_review' and r['area_m2'] is None
    assert 'unclassified_interior_closed_objects' in r['diagnostics']
    void=next(e['entity_id'] for e in service.index.data['entities'] if e['kind']=='LWPOLYLINE')
    r=service.room(args.model_copy(update={'void_entity_ids':[void]}))
    assert r['status']=='measured' and r['area_m2']==23
    assert len(r['selected']['holes'])==1


def test_unknown_units_and_infinite_seed(workspace):
    service,args,_=ingest(workspace,units=0)
    r=service.room(args)
    assert r['area_m2'] is None and 'unknown_units' in r['diagnostics']
    with pytest.raises(ValueError):
        service.room(args.model_copy(update={'seed':[math.nan,0]}))


def test_scope_and_cross_snapshot_rejection(workspace):
    service,args,drawing=ingest(workspace)
    with pytest.raises(ValueError):
        service.room(args.model_copy(update={'layout_id':'foreign'}))
    r=service.room(args.model_copy(update={'radius_m':1}))
    assert r['area_m2'] is None
    measurement=service.room(args)
    other,_,_=ingest(workspace,angle=.2)
    with pytest.raises(KeyError):
        other.get(measurement['measurement_id'])
    with pytest.raises(ValueError):
        other.read(GeometryRead(entity_ids=measurement['selected']['entity_ids'][:1]))


def test_native_arcs_dimensions_and_block_provenance(workspace):
    def build(doc,pt,line):
        block=doc.blocks.new('rotated')
        block.add_line((0,0),(1000,0))
        doc.modelspace().add_blockref('rotated',(9000,9000),dxfattribs={'rotation':90,'xscale':2,'yscale':2})
        doc.modelspace().add_arc((12000,12000),1000,0,180)
        dim=doc.modelspace().add_linear_dim(base=(0,-1000),p1=(0,0),p2=(6000,0));dim.render()
    service,_,_=ingest(workspace,build)
    arc=next(e for e in service.index.data['entities'] if e['kind']=='ARC')
    assert arc['native']['radius']==1000 and arc['approximation_m']==.001
    dim=next(e for e in service.index.data['entities'] if e['kind']=='DIMENSION')
    assert dim['native']['actual_measurement']==6000
    block=next(e for e in service.index.data['entities'] if len(e['entity_path'])>1)
    r=service.measure(GeometryMeasure(operation='length',entity_ids=[block['entity_id']]))
    assert r['value']==pytest.approx(2)
    assert block['paths'][0][0]==[9000,9000]


def test_spatial_predicate_uses_shape_not_bbox(workspace):
    def build(doc,pt,line):
        line((10,10),(15,15))
    service,_,drawing=ingest(workspace,build)
    args=SpatialQuery(layout_id=drawing['layouts'][0]['layout_id'],bbox=[10000,14000,11000,15000])
    assert service.query(args)['total']==0


def test_geometry_measurement_is_not_room_semantics(workspace):
    service,args,_=ingest(workspace,hole=True)
    poly=next(e for e in service.index.data['entities'] if e['kind']=='LWPOLYLINE')
    r=service.measure(GeometryMeasure(operation='area',entity_ids=[poly['entity_id']]))
    assert r['value']==1 and r['kind']=='geometry' and 'area_m2' not in r
    lines=[e for e in service.index.data['entities'] if e['kind']=='LINE']
    r=service.measure(GeometryMeasure(operation='distance',entity_ids=[lines[0]['entity_id'],lines[4]['entity_id']]))
    assert r['value']==pytest.approx(.2)
    assert len(r['measurement_line'])==2


def test_tools_register_measurement_observation_and_validate_status(workspace):
    service,args,drawing=ingest(workspace,single=True)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=drawing['id'],question='房间净面积是多少',mode='agent'))
    result=json.loads(session.invoke_tool('measure_room',args.model_dump()))
    oid=result['observation_id']
    payload={'claims':[{'text':'边界需要复核，候选面积24平方米。','state':'derived','evidence_ids':[],
                       'evidence_basis':'tool_observation','observation_ids':[oid]}]}
    with pytest.raises(ValueError,match='待复核'):
        session.validate_answer(json.dumps(payload))
    payload['claims'][0]['state']='unknown'
    answer=session.validate_answer(json.dumps(payload))
    assert answer['tool_observations'][0]['data']['measurement_id']==result['measurement_id']


def test_http_evidence_roundtrip_and_snapshot_isolation(workspace):
    service,args,drawing=ingest(workspace)
    app=create_app(workspace.settings)
    with TestClient(app) as client:
        base=f'/api/drawings/{drawing["id"]}'
        r=client.post(base+'/rooms/measure',json=args.model_dump())
        assert r.status_code==200,r.text
        data=r.json()
        assert client.get(data['evidence_url']).json()==data
        svg=client.get(data['preview_url'])
        assert svg.status_code==200 and 'image/svg+xml' in svg.headers['content-type']
        assert client.get(base+'/measurements').json()['items'][0]['measurement_id']==data['measurement_id']
        assert client.post(base+'/rooms/measure',json={'seed':[1,2]}).status_code==422


def test_irregular_room_preserves_concave_boundary(workspace):
    doc=ezdxf.new();doc.units=6
    inner=Polygon([(0,0),(6,0),(6,4),(4,4),(4,2),(0,2)])
    m=doc.modelspace()
    for shape in (inner,inner.buffer(.2,join_style=2)):
        m.add_lwpolyline(list(shape.exterior.coords)[:-1],close=True)
    m.add_text('异形房间',dxfattribs={'insert':(1,1),'height':.1})
    stream=io.StringIO();doc.write(stream)
    d=workspace.ingest('irregular.dxf',stream.getvalue().encode())
    service=GeometryService(workspace,d['id'])
    r=service.room(RoomMeasure(layout_id=d['layouts'][0]['layout_id'],seed=[1,1]))
    assert r['status']=='measured',r['diagnostics']
    assert r['area_m2']==16
    assert len(r['selected']['boundary'])==7


def test_missing_jamb_does_not_invent_a_wall(workspace):
    def build(doc,pt,line):
        m=doc.modelspace()
        for e in list(m.query('LINE')):
            if tuple(e.dxf.start)[:2]==(2000,0) and tuple(e.dxf.end)[:2]==(2000,-200):
                m.delete_entity(e)
    service,args,_=ingest(workspace,build,gap=True)
    result=service.room(args)
    assert result['area_m2'] is None
    assert not result.get('selected',{}).get('inferred_closures')


def test_foreign_room_anchor_rejects_shared_shell(workspace):
    def build(doc,pt,line):
        doc.modelspace().add_text('另一房间',dxfattribs={'insert':pt((5,3)),'height':150})
    service,args,d=ingest(workspace,build)
    region=next(r for r in workspace.regions(d['id']) if '另一房间' in r.text)
    result=service.room(args.model_copy(update={'other_room_region_ids':[region.region_id]}))
    assert result['area_m2'] is None
    assert 'contains_other_room_anchor' in result['diagnostics']


def test_room_measurement_does_not_modify_existing_semantics(workspace):
    service,args,d=ingest(workspace)
    names=('source.dxf','scene.json','regions.json','entities.json')
    before={name:workspace.store.artifact(d['id'],name).read_bytes() for name in names}
    service.room(args)
    assert all(workspace.store.artifact(d['id'],name).read_bytes()==content for name,content in before.items())
    assert workspace.store.get('drawing',d['id'])==d


def test_explicit_unit_override_is_recorded_without_rewriting_drawing(workspace):
    service,args,d=ingest(workspace,units=0)
    unresolved=service.room(args)
    measured=service.room(args.model_copy(update={'unit_override':'mm'}))
    assert unresolved['status']=='unresolved' and measured['status']=='measured'
    assert measured['area_m2']==24 and measured['unit_basis']=='request_override'
    assert measured['unit']=='unknown' and measured['applied_unit']=='mm'
    assert measured['measurement_id']!=unresolved['measurement_id']
    assert workspace.store.get('drawing',d['id'])['unit']=='unknown'


def test_dense_intersections_stop_before_polygonization():
    from cad_agent.geometry.rooms import analyze_room
    records=[{'entity_id':str(i),'linear':True,'paths':[[[0,0],[10*math.cos(i*.02),10*math.sin(i*.02)]]]} for i in range(210)]
    args=RoomMeasure(layout_id='local',seed=[0,0],close_openings=False)
    result=analyze_room(records,[0,0],1,args)
    assert result['status']=='unresolved' and result['area_m2'] is None
    assert 'intersection_limit_exceeded' in result['diagnostics']


def test_scoped_room_ignores_distant_tessellation_budget(workspace, monkeypatch):
    import cad_agent.geometry.source as source
    def build(doc, pt, line):
        # Unknown units previously expanded these far circles at 0.001 drawing
        # units and exhausted the shared geometry budget before the target room.
        for i in range(20):
            doc.modelspace().add_circle((100000+i*3000,100000),1000)
    service,args,_=ingest(workspace,build,units=0)
    monkeypatch.setattr(source,'MAX_TOTAL_POINTS',30)
    r=service.room(args.model_copy(update={'unit_override':'mm'}))
    assert r['status']=='measured'
    assert r['area_m2']==24
    assert not r['source_diagnostics']
    assert r['unit_basis']=='request_override'


def test_repeated_linework_and_distant_polyline_are_locally_bounded(workspace):
    def build(doc, pt, line):
        for i in range(10):
            line((0,0),(6,0));line((6,0),(6,4))
        doc.modelspace().add_lwpolyline([(20000+i*10,20000+(i%2)*100) for i in range(5100)])
    service,args,_=ingest(workspace,build)
    r=service.room(args)
    assert r['status']=='measured' and r['area_m2']==24
    assert r['input_segment_count']==8
    assert len(r['search_attempts'])>1


def test_multiple_outside_faces_never_claim_unique_net_boundary(workspace):
    def build(doc,pt,line):
        for a,b in [((-.35,-.35),(6.35,-.35)),((6.35,-.35),(6.35,4.35)),
                    ((6.35,4.35),(-.35,4.35)),((-.35,4.35),(-.35,-.35))]:line(a,b)
    service,args,_=ingest(workspace,build)
    r=service.room(args)
    assert r['area_m2'] is None
    assert 'competing_parallel_wall_faces' in r['diagnostics']
    assert any(len(e['parallel_face_offsets_m'])>1 for e in r['selected']['edge_evidence'])


def test_walker_limit_accepts_mixed_diagnostics():
    from cad_agent.cad.reader import walk_entities
    doc=ezdxf.new();m=doc.modelspace()
    for i in range(3):m.add_line((i,0),(i,1))
    diagnostics=[{'code':'UNSUPPORTED_GEOMETRY'}]
    assert len(list(walk_entities(m,diagnostics,'Model',entity_limit=1)))==1
    assert diagnostics[-1].code=='ENTITY_LIMIT'


def test_explicit_unit_controls_basic_curve_tessellation(workspace):
    def build(doc,pt,line):doc.modelspace().add_circle((20000,20000),1)
    service,_,_=ingest(workspace,build,units=0)
    circle=next(r for r in service.index.data['entities'] if r['kind']=='CIRCLE')
    measured=service.measure(GeometryMeasure(operation='length',entity_ids=[circle['entity_id']],unit_override='m'))
    assert measured['value']==pytest.approx(2*math.pi,abs=.005)
    assert measured['approximate'] is True


def test_void_rejects_small_competing_face_not_whole_request(workspace):
    def build(doc,pt,line):
        doc.modelspace().add_lwpolyline([pt(p) for p in [(.4,.4),(3,.4),(3,3),(.4,3)]],close=True)
    service,args,_=ingest(workspace,build,hole=True)
    void=next(e['entity_id'] for e in service.index.data['entities'] if e['kind']=='LWPOLYLINE' and e['bbox'][0]==4000)
    r=service.room(args.model_copy(update={'void_entity_ids':[void]}))
    assert r['candidate_area_m2']==23
    assert len(r['selected']['holes'])==1
    assert r['void_excluded_candidate_count']>=1
    assert r['algorithm_version']=='room-net-void-v1.5'
