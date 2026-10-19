import io
import json
import math

import ezdxf
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cad_agent.app import create_app
from cad_agent.analysis.session import CadAgentSession
from cad_agent.contracts import ChatRequest
from cad_agent.components.contracts import ContextQuery, DimensionQuery, RelationQuery
from cad_agent.components.service import ComponentService


def ingest(workspace, builder, units=4):
    doc=ezdxf.new("R2018"); doc.units=units
    builder(doc)
    stream=io.StringIO(); doc.write(stream)
    d=workspace.ingest("components.dxf",stream.getvalue().encode())
    return ComponentService(workspace,d["id"]),d,d["layouts"][0]["layout_id"]


def framed(doc, angle=0):
    m=doc.modelspace()
    def point(x,y):
        a=math.radians(angle)
        return (x*math.cos(a)-y*math.sin(a),x*math.sin(a)+y*math.cos(a))
    for x in (0,12000):
        m.add_lwpolyline([point(x,0),point(x+10000,0),point(x+10000,8000),point(x,8000)],close=True)
        for text,xx,yy in [("二层平面图",x+1000,1000),("比例 1:100",x+3000,1000),("图号：JZ-02",x+1000,500),("工程名称：示例工程",x+3000,500)]:
            m.add_text(text,dxfattribs={"insert":point(xx,yy),"height":100,"rotation":angle})


@pytest.mark.parametrize("angle",[0,27])
def test_context_rotated_and_repeated_titles_are_independent(workspace,angle):
    service,d,lid=ingest(workspace,lambda doc:framed(doc,angle))
    record=service.build("context",ContextQuery(layout_id=lid))
    assert len(record["subframes"])==2
    assert len({s["object_id"] for s in record["subframes"]})==2
    assert all(s["bbox"] and s["floor_candidates"]==["二层"] for s in record["subframes"])
    assert all(s["rotation"]==angle for s in record["subframes"])
    borders=[f for f in record["frames"] if f["kind"]=="border_candidate"]
    assert len(borders)==2 and all(f["metadata"]["drawing_number"]["values"][0]["value"]=="JZ-02" for f in borders)
    assert all(s["scale_candidates"][0]["value"]=="1:100" for s in record["subframes"])
    assert service.build("context",ContextQuery(layout_id=lid,offset=1))==record


def test_title_anchor_does_not_invent_subframe_extent(workspace):
    def build(doc):
        m=doc.modelspace()
        m.add_text("首层平面图",dxfattribs={"height":100})
        m.add_text("详见二层平面图",dxfattribs={"insert":(2000,0),"height":100})
    s,d,lid=ingest(workspace,build)
    record=s.build("context",ContextQuery(layout_id=lid))
    assert len(record["subframes"])==1
    assert record["subframes"][0]["bbox"] is None
    assert record["subframes"][0]["diagnostics"]==["subframe_extent_unresolved"]


def dimensions(doc):
    m=doc.modelspace()
    m.add_line((0,0),(0,1000));m.add_line((6000,0),(6000,1000))
    for y,text,lf in [(-500,"<>",1),(-1000,"12000",2),(-1500,"5900",1)]:
        dim=m.add_linear_dim(base=(0,y),p1=(0,0),p2=(6000,0),text=text,override={"dimlfac":lf})
        dim.render()


def test_dimension_values_styles_conflicts_and_endpoint_binding(workspace):
    s,d,lid=ingest(workspace,dimensions)
    r=s.build("dimensions",DimensionQuery(layout_id=lid))
    by_text={x["text_override"]:x for x in r["dimensions"]}
    assert by_text["<>"]["geometry_length_m"]==6
    assert by_text["12000"]["expected_unformatted_text_value"]==12000
    assert by_text["12000"]["geometry_length_m"]==6
    assert by_text["12000"]["status"]!="conflict"
    assert by_text["5900"]["status"]=="conflict"
    assert all(m["total"]==1 for m in by_text["<>"]["target_candidates"])
    assert by_text["<>"]["association_status"]=="candidate_endpoint_proximity"
    assert by_text["<>"]["rendered_texts"]==["6000"]


def test_aligned_dimension_library_disagreement_is_visible(workspace):
    def build(doc):
        dim=doc.modelspace().add_aligned_dim((0,0),(0,2700),distance=500)
        dim.render()
        # Real archives contain aligned dimensions with missing/stale angles.
        dim.dimension.dxf.angle=0
        dim.dimension.dxf.dimtype=33
    s,d,lid=ingest(workspace,build)
    item=s.build("dimensions",DimensionQuery(layout_id=lid))["dimensions"][0]
    assert item["actual_measurement"]==0 and item["geometry_recomputed"]==2700
    assert item["rendered_texts"]==["2700"] and item["status"]=="conflict"
    assert "native_and_recomputed_measurement_conflict" in item["diagnostics"]


def test_dimensions_unknown_unit_scope_and_cross_snapshot(workspace):
    s,d,lid=ingest(workspace,dimensions,units=0)
    r=s.build("dimensions",DimensionQuery(layout_id=lid))
    assert all(x["geometry_length_m"] is None for x in r["dimensions"])
    resolved=s.build("dimensions",DimensionQuery(layout_id=lid,unit_override="mm"))
    assert resolved["unit_basis"]=="request_override"
    assert s.build("dimensions",DimensionQuery(layout_id=lid,bbox=[10000,10000,11000,11000]))["dimensions"]==[]
    other,_,_=ingest(workspace,lambda doc:framed(doc))
    with pytest.raises(KeyError):other.get(r["record_id"])
    with pytest.raises(ValueError):s.build("context",ContextQuery(layout_id="foreign"))
    with pytest.raises(ValidationError):DimensionQuery(layout_id=lid,bbox=[0,0,float("inf"),2])


def connected_rooms(doc, gap=True):
    m=doc.modelspace()
    for x0,x1 in [(0,6),(-.2,6.2),(6.2,12.2),(6,12.4)]:
        y0,y1=(0,4) if x0 in (0,6.2) else (-.2,4.2)
        m.add_line((x0,y0),(x1,y0));m.add_line((x0,y1),(x1,y1))
        for x in (x0,x1):
            if gap and x in (6,6.2):
                m.add_line((x,y0),(x,1));m.add_line((x,2),(x,y1))
            else:m.add_line((x,y0),(x,y1))
    if gap:
        m.add_line((6,1),(6.2,1));m.add_line((6,2),(6.2,2))
    for x in (1,7):m.add_text("空间",dxfattribs={"insert":(x,2),"height":.15})


def test_wall_opening_room_graph_and_no_gap_negative(workspace):
    s,d,lid=ingest(workspace,connected_rooms,units=6)
    r=s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6]))
    assert len(r["rooms"])==2
    assert len(r["openings"])==1
    opening=r["openings"][0]
    assert opening["width_m"]==1 and opening["paired_face_spacing_m"]==.2
    assert len(opening["room_ids"])==2 and opening["connection_status"]=="two_space_candidates"
    assert opening["wall_ids"] and opening["door_type"] is None
    ids={x["object_id"] for name in ("rooms","walls","openings") for x in r[name]}
    assert all(x["source_id"] in ids and x["target_id"] in ids for x in r["relations"])
    assert all(x["status"]=="candidate" and x["entity_ids"] for x in r["relations"])
    s,d,lid=ingest(workspace,lambda doc:connected_rooms(doc,False),units=6)
    assert not s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6]))["openings"]


def test_http_agent_evidence_and_pagination(workspace):
    s,d,lid=ingest(workspace,dimensions)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=d["id"],question="读取尺寸"))
    args=DimensionQuery(layout_id=lid,limit=1).model_dump()
    tool=json.loads(session.invoke_tool("read_dimensions",args))
    assert tool["observation_id"] and tool["has_more"] and tool["total"]==3
    again=json.loads(session.invoke_tool("get_component_evidence",dict(record_id=tool["record_id"],view="dimensions",offset=1,limit=1)))
    assert again["items"][0]["object_id"]!=tool["items"][0]["object_id"]
    with TestClient(create_app(workspace.settings)) as client:
        response=client.post(f'/api/drawings/{d["id"]}/components/dimensions',json=args)
        assert response.status_code==200,response.text
        assert response.json()["record_id"]==tool["record_id"]
        full=client.get(response.json()["evidence_url"])
        assert full.status_code==200 and len(full.json()["dimensions"])==3
        assert client.get(f'/api/drawings/foreign/components/{tool["record_id"]}').status_code==404


def test_layer_clues_exclude_furniture_without_confirming_identity(workspace):
    def build(doc):
        connected_rooms(doc)
        for e in doc.modelspace():e.dxf.layer="A-WALL"
        for x,y,w,h in [(16,0,3,2),(15.8,-.2,3.4,2.4)]:
            doc.modelspace().add_lwpolyline([(x,y),(x+w,y),(x+w,y+h),(x,y+h)],close=True,dxfattribs={"layer":"FURNITURE"})
    s,d,lid=ingest(workspace,build,units=6)
    args=RelationQuery(layout_id=lid,bbox=[-1,-1,22,6])
    all_lines=s.build("relations",args.model_copy(update={"linework_mode":"all"}))
    filtered=s.build("relations",args)
    assert len(all_lines["rooms"])==3 and len(filtered["rooms"])==2
    assert filtered["linework_basis"]=="wall_and_column_layer_hints"
    assert filtered["layer_selection"]["excluded_entity_count"]==2
    assert all(r["status"]=="candidate" for r in filtered["rooms"])


def test_component_claim_cannot_upgrade_candidates_to_observed(workspace):
    s,d,lid=ingest(workspace,dimensions)
    session=CadAgentSession(workspace,ChatRequest(snapshot_id=d["id"],question="读取尺寸"))
    result=json.loads(session.invoke_tool("read_dimensions",dict(layout_id=lid)))
    claim=dict(text="标注包含需要复核的冲突。",state="observed",evidence_basis="tool_observation",observation_ids=[result["observation_id"]])
    with pytest.raises(ValueError):session.validate_answer(json.dumps(dict(claims=[claim])))
    claim["state"]="unknown"
    assert session.validate_answer(json.dumps(dict(claims=[claim])))["claims"]
