"""Adversarial opening tests: topology, stepped returns, symbols and conflicts."""
import math

import pytest
from shapely.geometry import Polygon

from cad_agent.components.contracts import RelationQuery
from cad_agent.components.openings import side_associations
from tests.test_components import connected_rooms, ingest


def fixture(doc, kind="door", broken=False):
    m=doc.modelspace()
    # Two complete spaces with a stepped lower jamb, unequal 1.0 / 1.1 m gaps.
    for x0,x1,y0,y1 in [(0,6,0,4),(-.2,6.2,-.2,4.2),(6.2,12.2,0,4),(6,12.4,-.2,4.2)]:
        for y in (y0,y1):m.add_line((x0,y),(x1,y),dxfattribs={"layer":"A-WALL"})
        for x in (x0,x1):
            gapstart=1 if x==6 else .9
            spans=[(y0,gapstart),(2,y1)] if x in (6,6.2) else [(y0,y1)]
            for a,b in spans:m.add_line((x,a),(x,b),dxfattribs={"layer":"A-WALL"})
    m.add_line((6,2),(6.2,2),dxfattribs={"layer":"A-WALL"})
    for a,b in [((6,1),(6.1,1)),((6.1,1),(6.1,.9)),((6.1,.9),(6.2,.9))]:
        if not broken or a!=(6.1,1):m.add_line(a,b,dxfattribs={"layer":"A-WALL"})
    if kind in ("door","conflict","arc_only"):
        m.add_arc((6.1,2),1,270,360,dxfattribs={"layer":"A-DOOR"})
        if kind!="arc_only":m.add_line((6.1,2),(7.1,2),dxfattribs={"layer":"A-DOOR"})
    if kind=="conflict":
        m.add_text("C1020",dxfattribs={"insert":(6.5,1.4),"height":.1})
        for x in (6.07,6.13):m.add_line((x,1),(x,2),dxfattribs={"layer":"A-WINDOW"})
    return m


def test_stepped_jamb_has_one_room_on_each_opposite_side(workspace):
    s,d,lid=ingest(workspace,fixture,units=6)
    r=s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind="floor_plan"))
    assert len(r["openings"])==1 and len(r["rooms"])==2
    o=r["openings"][0]
    assert o["opening_type"]=="door_candidate" and o["door_type"]=="swing_candidate"
    assert o["passage_status"]=="supported_door_between_two_spaces"
    assert all(len(x["room_ids"])==1 for x in o["sides"])
    assert o["sides"][0]["room_ids"]!=o["sides"][1]["room_ids"]
    assert math.dist(*o["faces"][0])!=math.dist(*o["faces"][1])
    assert len([x for x in r["relations"] if x["kind"]=="door_connects_candidate_spaces"])==1
    assert all(x["area_m2"] is None for x in r["rooms"])


@pytest.mark.parametrize("kind,broken",[("door",True),("arc_only",False)])
def test_absent_jamb_or_leaf_does_not_create_portal(workspace,kind,broken):
    s,d,lid=ingest(workspace,lambda doc:fixture(doc,kind,broken),units=6)
    r=s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind="floor_plan"))
    assert not r["openings"]
    assert not any(x["kind"]=="door_connects_candidate_spaces" for x in r["relations"])


def test_window_between_two_spaces_is_adjacency_not_passage(workspace):
    def draw(doc):
        connected_rooms(doc)
        for e in doc.modelspace():e.dxf.layer="A-WALL"
        for x in (6.07,6.13):
            # Split frame rails: they must combine geometrically, not be missed.
            for y0,y1 in [(1,1.5),(1.5,2)]:doc.modelspace().add_line((x,y0),(x,y1),dxfattribs={"layer":"A-WINDOW"})
        doc.modelspace().add_text("C1020",dxfattribs={"insert":(6.5,1.4),"height":.1})
        doc.modelspace().add_arc((6.1,1),1,45,90,dxfattribs={"layer":"A-WINDOW"})
        doc.modelspace().add_line((6.1,1),(6.80710678,1.70710678),dxfattribs={"layer":"A-WINDOW"})
    s,d,lid=ingest(workspace,draw,units=6)
    r=s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind="floor_plan"));o=r["openings"][0]
    assert o["opening_type"]=="window_candidate" and len(o["room_ids"])==2
    assert o["connection_status"]=="blocked_window" and o["passage_status"]=="not_established"
    assert not any(x["kind"]=="door_connects_candidate_spaces" for x in r["relations"])


def test_conflicting_symbol_and_window_tag_blocks_passage(workspace):
    s,d,lid=ingest(workspace,lambda doc:fixture(doc,"conflict"),units=6)
    r=s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind="floor_plan"));o=r["openings"][0]
    assert o["opening_type"]=="conflict" and o["connection_status"]=="blocked_type_conflict"
    assert not any(x["kind"]=="door_connects_candidate_spaces" for x in r["relations"])


def test_labels_alone_and_untyped_gap_are_not_door_passages(workspace):
    def draw(doc):
        connected_rooms(doc);doc.modelspace().add_text("M1020",dxfattribs={"insert":(6.5,1.4),"height":.1})
    s,d,lid=ingest(workspace,draw,units=6)
    r=s.build("relations",RelationQuery(layout_id=lid,bbox=[-1,-1,14,6]));o=r["openings"][0]
    assert len(o["room_ids"])==2 and o["opening_type"]=="unknown" and o["passage_status"]=="not_established"


def test_opposite_side_probes_reject_same_side_overlaps_and_holes():
    faces=[[[0,0],[1,0]],[[0,.2],[1,.2]]]
    lower=dict(object_id="lower",boundary=[[-1,-2],[2,-2],[2,0],[-1,0]])
    upper=dict(object_id="upper",boundary=[[-1,.2],[2,.2],[2,2],[-1,2]])
    sides=side_associations(faces,[lower,upper],1,.001)
    assert [s["room_ids"] for s in sides]==[["lower"],["upper"]]
    duplicate=lower|dict(object_id="overlap")
    assert side_associations(faces,[lower,duplicate,upper],1,.001)[0]["status"]=="ambiguous"
    upper["internal_rings"]=[[[-.1,.21],[1.1,.21],[1.1,.5],[-.1,.5]]]
    assert side_associations(faces,[lower,upper],1,.001)[1]["status"]=="unresolved"

@pytest.mark.parametrize('rotation,mirror',[(37,False),(27,True)])
def test_rotated_mirrored_insert_keeps_wcs_symbol_and_side_binding(workspace,rotation,mirror):
    import ezdxf
    def draw(doc):
        temp=ezdxf.new();fixture(temp)
        block=doc.blocks.new('PLAN')
        for e in temp.modelspace():
            # copy foreign entities without carrying source document handles
            clone=e.copy();clone.doc=None;block.add_entity(clone)
        doc.modelspace().add_blockref('PLAN',(0,0),dxfattribs={'rotation':rotation,'xscale':-1 if mirror else 1})
    s,d,lid=ingest(workspace,draw,units=6)
    r=s.build('relations',RelationQuery(layout_id=lid,bbox=[-20,-20,20,20],view_kind='floor_plan'))
    assert any(o['passage_status']=='supported_door_between_two_spaces' for o in r['openings'])


def test_clipped_space_never_gets_nearest_room_fallback(workspace):
    s,d,lid=ingest(workspace,fixture,units=6)
    r=s.build('relations',RelationQuery(layout_id=lid,bbox=[-1,-1,9,6],view_kind='floor_plan'))
    assert r['openings']
    assert all(o['passage_status']=='not_established' for o in r['openings'])
    assert not any(x['kind']=='door_connects_candidate_spaces' for x in r['relations'])


def test_defaults_and_http_round_trip_share_immutable_record(workspace):
    s,d,lid=ingest(workspace,fixture,units=6)
    args=RelationQuery(layout_id=lid,bbox=[-1,-1,14,6],view_kind='floor_plan')
    direct=s.build('relations',args)
    explicit=s.build('relations',RelationQuery.model_validate(args.model_dump()|{'max_opening_m':3.0,'limit':20,'view':'openings'}))
    assert direct['record_id']==explicit['record_id']
    assert direct['discovery_id']==explicit['discovery_id']
