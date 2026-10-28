import math
import pytest
from cad_agent.geometry.candidate_routes import candidates,sample_pair
from test_geometry_workbench import line,arc


def rec(name,a,b):
    r=line(name,a,b);r.update(entity_path=[name],paths=[[a,b]])
    return r


def test_candidate_ranking_uses_source_endpoints_not_target_width():
    records=[rec('inner_a',[0,0],[10,0]),rec('outer_a',[0,-1],[10,-1]),rec('inner_b',[0,4],[10,4]),rec('outer_b',[0,5],[10,5])]
    anchor=dict(kind='DIMENSION',native=dict(defpoint2=[5,0,0],defpoint3=[5,4,0]))
    r=candidates(records,'LINE',anchor)
    assert set(r['items'][0]['entity_ids'])=={'inner_a','inner_b'}
    assert r['items'][0]['width_drawing_units']==4
    assert not r['rule_threshold_used'] and not r['target_complete']


def test_narrow_obstacle_between_uniform_samples_is_seen_conditionally():
    a,b=rec('a',[0,0],[100,0]),rec('b',[0,10],[100,10])
    obstacle=dict(entity_id='o',paths=[[[10.01,0],[10.02,0],[10.02,3],[10.01,3],[10.01,0]]])
    r=sample_pair(a,b,3,[obstacle],[-5,105])
    assert r['uncovered_declared_intervals']==[[-5,0],[100,105]]
    hits=[s for s in r['samples'] if s['projected_obstacle_ids']]
    assert hits and all(x['height_relation']=='unknown' for x in hits)
    assert any(x['conditional_coheight_width_drawing_units']==pytest.approx(7) for x in hits)
    assert r['sample_minimum_drawing_units']==10
    assert not r['global_minimum_proven']


def test_curve_obstacle_and_unsupported_shape_are_separate():
    a,b=arc('a',5,0,90),arc('b',10,0,90)
    obstacle=dict(entity_id='open',paths=[[[0,0],[1,1]]])
    r=sample_pair(a,b,3,[obstacle],[0,180])
    assert r['unsupported_obstacle_ids']==['open']
    assert r['uncovered_declared_intervals']==[[90,180]]
    assert r['sample_minimum_drawing_units']==pytest.approx(5)


def test_request_outside_available_domain_has_no_samples():
    r=sample_pair(rec('a',[0,0],[10,0]),rec('b',[0,4],[10,4]),requested_interval=[20,30])
    assert r['samples']==[] and r['sample_minimum_drawing_units'] is None
    assert r['uncovered_declared_intervals']==[[20,30]]


def test_host_hypothesis_profile_export(workspace,tmp_path):
    import ezdxf
    from cad_agent.geometry.workbench import GeometryWorkbench,PrepareBundle,ReadBundle
    from cad_agent.geometry.workbench_routes import InspectViews,Hypothesis,MeasureRoute,RouteSegment,ExportResults,views,hypothesis,route,export
    d=ezdxf.new();d.units=0;m=d.modelspace()
    m.add_line((0,0),(20,0));m.add_line((0,4),(20,4));m.add_text('PLAN A',dxfattribs={'insert':(2,7),'height':1})
    p=tmp_path/'view.dxf';d.saveas(p);drawing=workspace.ingest('view.dxf',p.read_bytes());sid=drawing['snapshot_id']
    service=GeometryWorkbench(workspace,sid)
    b=service.prepare(PrepareBundle(layout_id=drawing['layouts'][0]['layout_id'],bbox=[-1,-1,22,9]));bid=b['bundle_id']
    labels=views(service,InspectViews(bundle_id=bid));assert labels['items']
    sources=service.read(ReadBundle(bundle_id=bid,kind='LINE'))['items'];ids=[r['entity_id'] for r in sources]
    lookup=service.read(ReadBundle(bundle_id=bid,source_paths=[sources[0]['entity_path']]))
    assert lookup['items'][0]['entity_id']==ids[0]
    h=hypothesis(service,Hypothesis(bundle_id=bid,label_region_ids=[labels['items'][0]['region_id']],entity_ids=ids,view_kind='plan',interpretation='candidate route'))
    r=route(service,MeasureRoute(bundle_id=bid,hypothesis_id=h['hypothesis_id'],segments=[RouteSegment(entity_ids=ids,requested_interval=[-2,22])]))
    assert r['segments'][0]['uncovered_declared_intervals']==[[-2,0],[20,22]]
    assert not r['global_minimum_proven']
    out=export(service,ExportResults(bundle_id=bid,route_ids=[r['route_id']],hypothesis_ids=[h['hypothesis_id']]))
    assert out['counts']['routes']==1
    saved=workspace.store.get('geometry_resultset',out['result_id'])
    assert saved['routes'][0]['segments'][0]['samples']
