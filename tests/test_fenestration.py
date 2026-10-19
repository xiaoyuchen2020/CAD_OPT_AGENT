"""Composite symbols: physical evidence, identities and adversarial neighbors."""
import math

import ezdxf
import pytest
from cad_agent.components.fenestration import double_swing_pairs, match_double_swing, sliding_panel_evidence
from cad_agent.components.openings import classify_openings, swing_symbols
from shapely.geometry import LineString


def opening(width=2, height=.2):
    return dict(object_id='o',boundary=[[0,0],[width,0],[width,height],[0,height],[0,0]],
                faces=[[[0,0],[width,0]],[[0,height],[width,height]]],entity_ids=[],diagnostics=[],
                room_ids=['a','b'],connection_status='two_space_candidates')


def panel(eid,x0,x1,y0,y1,exploded=False):
    path=[[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]
    if exploded:return [dict(entity_id=f'{eid}-{i}',kind='LINE',layer='0',linear=True,paths=[[a,b]]) for i,(a,b) in enumerate(zip(path,path[1:]))]
    return [dict(entity_id=eid,kind='LWPOLYLINE',layer='0',linear=True,paths=[path])]


def classify(o,records,symbols=(),tag=None,**kw):
    texts=[] if tag is None else [dict(entity_id='tag',text=tag,origin=[0,-.2],**kw)]
    g=dict(openings=[o],relations=[]);classify_openings(g,records,list(symbols),texts,1);return g


def leaves(angle=90,offset=.1):
    a=math.radians(angle)
    return [dict(arc_id=str(i),hinge=h,closed_tip=[1,offset],open_tip=tip,radius_m=1,
                 entity_ids=[str(i),f'leaf{i}'],sweep_degrees=angle)
            for i,h,tip in [(0,[0,offset],[math.cos(a),offset+math.sin(a)]),
                            (1,[2,offset],[2-math.cos(a),offset+math.sin(a)])]]


@pytest.mark.parametrize('angle',[45,90])
def test_complementary_leaves_have_one_symbol_span(angle):
    ss=leaves(angle);pairs=double_swing_pairs(ss,1)
    assert len(pairs)==1 and pairs[0]['symbol_span_m']==2
    g=classify(opening(),[],ss,'FM2024')
    assert g['openings'][0]['operation_type']=='double_swing'
    assert g['openings'][0]['passage_status']=='supported_door_between_two_spaces'


def test_small_angle_double_casement_needs_identity_and_window_never_passage():
    for tag,expected in [(None,'unknown'),('C2024','window_candidate')]:
        records=panel('a',0,1.02,.05,.08)+panel('b',.98,2,.09,.12)
        # Remove sliding overlap, retaining supported parallel frame rails.
        records=panel('frame',0,2,.05,.10)
        g=classify(opening(),records,leaves(45),tag)
        assert g['openings'][0]['opening_type']==expected
        assert g['openings'][0]['passage_status']=='not_established'


@pytest.mark.parametrize('mutation',['one_leaf','other_side','adjacent','nonmeeting'])
def test_unrelated_or_incomplete_leaves_cannot_form_double(mutation):
    ss=leaves()
    if mutation=='one_leaf':ss.pop()
    elif mutation=='other_side':ss[1]['open_tip'][1]=-1
    elif mutation=='adjacent':ss[1]['hinge']=[4,.1]
    else:ss[1]['closed_tip']=[1.2,.1]
    assert not double_swing_pairs(ss,1)


def test_offset_double_requires_observed_frame_and_preserves_width():
    pair=double_swing_pairs(leaves(offset=-.2),1);o=opening(2.5)
    assert not match_double_swing(o,pair,[],1)
    frame=[(LineString([(x,-.2),(x,0)]),str(x)) for x in (0,2)]
    hit=match_double_swing(o,pair,frame,1)[0]
    assert hit['frame_offset_m']==.2 and hit['symbol_span_m']==2
    assert hit['opening_coverage']==.8 and o['faces'][0][1][0]==2.5
    assert not match_double_swing(o,pair,frame[:1],1)


@pytest.mark.parametrize('exploded',[False,True])
@pytest.mark.parametrize('tag,expected',[('TLM2024','door_candidate'),('TLC2024','window_candidate'),(None,'unknown')])
def test_same_sliding_geometry_does_not_determine_door_window_identity(exploded,tag,expected):
    rs=panel('a',0,1.02,.04,.07,exploded)+panel('b',.98,2,.08,.11,exploded)
    g=classify(opening(),rs,tag=tag);o=g['openings'][0]
    assert o['opening_type']==expected and o['operation_type']=='sliding'
    assert (o['passage_status']=='supported_door_between_two_spaces')==(expected=='door_candidate')


def test_four_exploded_sliding_panels_and_broken_cap():
    rs=[]
    for i,(a,b,c,d) in enumerate([(0,1.35,.10,.13),(1.02,2.4,.06,.096),(2.4,3.78,.06,.096),(3.45,4.8,.10,.13)]):
        rs+=panel(str(i),a,b,c,d,True)
    evidence=sliding_panel_evidence(opening(4.8),rs,1)
    assert len(evidence)==1 and evidence[0]['panel_count']==4 and evidence[0]['symbol_span_m']==4.8
    assert len(evidence[0]['entity_ids'])==16
    # No fabricated cap can close a broken two-leaf symbol.
    two=panel('a',0,1.02,.04,.07,True)+panel('b',.98,2,.08,.11,True)
    assert not sliding_panel_evidence(opening(),two[:-1],1)


@pytest.mark.parametrize('variant',['butt','same_track','rails_only','oversize_tag'])
def test_fixed_glazing_and_neighbor_labels_cannot_be_sliding_doors(variant):
    rs=panel('a',0,1,.04,.07)+panel('b',1,2,.08,.11)
    if variant=='same_track':rs=panel('a',0,1.02,.04,.07)+panel('b',.98,2,.04,.07)
    elif variant=='rails_only':rs=[dict(entity_id=str(i),kind='LINE',layer='0',linear=True,paths=[[[0,y],[2,y]]]) for i,y in enumerate([.04,.08])]
    elif variant=='oversize_tag':rs=panel('a',0,1.02,.04,.07)+panel('b',.98,2,.08,.11)
    o=classify(opening(),rs,tag='TLM4824' if variant=='oversize_tag' else 'TLM2024')['openings'][0]
    assert o['opening_type']=='unknown' and o['passage_status']=='not_established'


def test_rotated_text_extent_associates_even_when_origin_past_jamb():
    o=opening(.7);rs=panel('frame',0,.7,.04,.09)
    texts=[dict(entity_id='tag',text='C0715',origin=[-.2,-.7],bbox=[-.2,-.4,.5,-.1])]
    g=dict(openings=[o],relations=[]);classify_openings(g,rs,[],texts,1)
    assert o['opening_type']=='window_candidate'
    assert o['classification_evidence']['labels'][0]['entity_id']=='tag'


def test_competing_equal_distance_tags_are_not_assigned():
    a=opening();b=opening();b['object_id']='b'
    g=dict(openings=[a,b],relations=[])
    classify_openings(g,panel('frame',0,2,.04,.09),[],[dict(entity_id='tag',text='C2024',origin=[0,-.2])],1)
    assert all(not x['classification_evidence']['labels'] for x in g['openings'])


def test_native_small_arc_without_radial_leaf_is_not_a_symbol():
    doc=ezdxf.new();arc=doc.modelspace().add_arc((0,0),1,0,45)
    rec=dict(entity_id='arc',kind='ARC',layer='0',linear=False,paths=[])
    assert not swing_symbols([rec],dict(doc=doc,entities={'arc':(arc,[],None,None)}),1)


@pytest.mark.parametrize('kind,mirror',[('double_swing',False),('double_swing',True),('sliding',False)])
def test_native_composite_symbols_flow_through_room_graph(workspace,kind,mirror):
    from tests.test_components import connected_rooms,ingest
    from cad_agent.components.contracts import RelationQuery
    def draw(doc):
        temp=ezdxf.new();connected_rooms(temp)
        for e in temp.modelspace():e.dxf.layer='A-WALL'
        m=temp.modelspace()
        if kind=='double_swing':
            for y,start,end in [(1,0,90),(2,270,360)]:
                m.add_arc((6.1,y),.5,start,end,dxfattribs={'layer':'A-DOOR'})
                m.add_line((6.1,y),(6.6,y),dxfattribs={'layer':'A-DOOR'})
        else:
            m.add_lwpolyline([(6.04,1),(6.07,1),(6.07,1.52),(6.04,1.52)],close=True,dxfattribs={'layer':'A-WINDOW'})
            m.add_lwpolyline([(6.08,1.48),(6.11,1.48),(6.11,2),(6.08,2)],close=True,dxfattribs={'layer':'A-WINDOW'})
            m.add_text('TLM1024',dxfattribs={'insert':(6.5,1.2),'height':.08})
        block=doc.blocks.new('PLAN')
        for e in m:
            clone=e.copy();clone.doc=None;block.add_entity(clone)
        doc.modelspace().add_blockref('PLAN',(0,0),dxfattribs={'rotation':27,'xscale':-1 if mirror else 1})
    service,d,lid=ingest(workspace,draw,units=6)
    graph=service.build('relations',RelationQuery(layout_id=lid,bbox=[-20,-20,20,20],view_kind='floor_plan'))
    assert len(graph['openings'])==1
    o=graph['openings'][0]
    assert o['operation_type']==kind and o['opening_type']=='door_candidate'
    assert o['passage_status']=='supported_door_between_two_spaces'


def test_swing_and_sliding_conflict_blocks_passage():
    records=panel('a',0,1.02,.04,.07)+panel('b',.98,2,.08,.11)
    g=classify(opening(),records,leaves(),tag='TLM2024');o=g['openings'][0]
    assert o['operation_type']=='conflict' and o['connection_status']=='blocked_operation_conflict'
    assert not g['relations']


def test_overlapping_aperture_hypotheses_cannot_duplicate_passages():
    a=opening();b=opening(2.1);b['object_id']='alternative'
    g=dict(openings=[a,b],relations=[]);classify_openings(g,[],leaves(),[],1)
    assert all(o['opening_type']=='door_candidate' for o in g['openings'])
    assert all(o['connection_status']=='blocked_overlapping_candidates' and o['passage_status']=='not_established' for o in g['openings'])
    assert a['competing_opening_ids']==['alternative'] and not g['relations']


def test_oblique_leaders_cannot_bind_an_offset_door_to_wall_gap():
    pair=double_swing_pairs(leaves(offset=-.2),1)
    leaders=[(LineString([(x,-.2),(x+.2,0)]),str(x)) for x in (0,2)]
    assert not match_double_swing(opening(2.5),pair,leaders,1)
