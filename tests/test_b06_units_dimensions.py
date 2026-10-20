"""B06: source facts and contradictory channels stay separate; zero model calls."""
from copy import deepcopy
from types import SimpleNamespace
import math

import ezdxf
import pytest
from shapely.geometry import box
from shapely.strtree import STRtree

from cad_agent.analysis.text_units import read_unit_candidates, read_elevation_annotation
from cad_agent.analysis.unit_policy import resolve_units
from cad_agent.cache import file_stamp
from cad_agent.components.dimensions import read_dimensions_from_source, measurement_comparisons
from cad_agent.components.dimension_chain import check_chain, DimensionChainQuery
from cad_agent.contracts import stable_id
from cad_agent.geometry.source import candidate_bounds, compute_text_metadata, source_text_metadata


def dimensions_source(doc):
    sid='fixture';lid=stable_id('layout',sid,'Model');entities={};ids=[];shapes=[]
    for e in doc.modelspace():
        eid=stable_id('ent',sid,lid,e.dxf.handle);bounds=candidate_bounds(e)
        entities[eid]=(e,[e.dxf.handle],lid,bounds)
        if bounds:
            ids.append(eid);a,b,c,d=bounds;shapes.append(box(a-1e-7,b-1e-7,c+1e-7,d+1e-7))
    return dict(entities=entities,ids=ids,tree=STRtree(shapes),diagnostics=[]),sid,lid


def test_conflicting_native_channels_keep_control_points_and_no_confirmed_target():
    doc=ezdxf.new('R2018');m=doc.modelspace()
    m.add_line((0,0),(100,0));m.add_line((0,2700),(100,2700))
    dim=m.add_aligned_dim((0,0),(0,2700),distance=500);dim.render()
    dim.dimension.dxf.angle=0;dim.dimension.dxf.dimtype=33;dim.dimension.dxf.actual_measurement=2700
    source,sid,lid=dimensions_source(doc)
    d=read_dimensions_from_source(source,sid,lid,None,'mm')['dimensions'][0]
    assert d['actual_measurement']==0 and d['stored_measurement']==d['geometry_recomputed']==2700
    assert d['rendered_texts']==['2700'] and d['status']=='conflict'
    assert any(c['state']=='agree' and c['left']=='dxf_group_42' for c in d['measurement_comparisons'])
    assert any(c['state']=='conflict' and c['left']=='ezdxf_get_measurement' for c in d['measurement_comparisons'])
    assert [p['dxf_attribute'] for p in d['definition_control_points']]==['defpoint2','defpoint3']
    assert all(p['source_entity_id']==d['object_id'] for p in d['definition_control_points'])
    assert d['target_binding']['state']=='candidate' and d['target_binding']['confirmed_entity_ids']==[]
    assert d['target_binding']['engineering_role']=='unconfirmed'
    unknown=read_dimensions_from_source(source,sid,lid,None,'unknown')['dimensions'][0]
    assert unknown['geometry_length_m'] is None and unknown['target_binding']['state']=='unresolved'


def test_rendered_rounding_is_not_a_conflict_and_stored_disagreement_is():
    r=measurement_comparisons(8500.125,8500.125,8500.125,['8500'],1,0,True)
    assert all(c['state']=='agree' for c in r)
    r=measurement_comparisons(8500,7200,8500,['8500'],1,0,True)
    assert any(c['state']=='conflict' and c['right']=='dxf_group_42' for c in r)
    assert not any(c['left']=='native_rendered_text' for c in measurement_comparisons(1,1,1,['1±.1'],1,2,True))


def test_conflict_chain_does_not_sum_near_zero_library_values(monkeypatch):
    doc=ezdxf.new('R2018');m=doc.modelspace()
    for a,b,x in [(0,1000,500),(1000,2700,500),(0,2700,1000)]:
        dim=m.add_aligned_dim((0,a),(0,b),distance=x);dim.render()
        dim.dimension.dxf.angle=0;dim.dimension.dxf.dimtype=33;dim.dimension.dxf.actual_measurement=b-a
    source,sid,lid=dimensions_source(doc)
    record=read_dimensions_from_source(source,sid,lid,None,'mm')
    record.update(record_id='record',request={},unit='mm',unit_status='source_declared')
    monkeypatch.setattr('cad_agent.components.dimension_chain.ComponentService',lambda *a:SimpleNamespace(build=lambda *a:record))
    result=check_chain(None,sid,DimensionChainQuery(layout_id=lid,orientation='vertical',side='left'))
    assert result['status']=='conflict'
    assert result['sum_value'] is None and result['difference'] is None
    assert result['display_arithmetic']['sum']=='2700' and result['display_arithmetic']['difference']=='0'
    assert result['geometric_sum']==result['geometric_total']==2700
    assert result['target_binding_status']=='unconfirmed'


def scene_pair():
    anchor=dict(entity_id='a',entity_path=['A'],kind='TEXT',text='面积30m',layout_id='l',layer='0',
        origin=[0,0],bbox=[0,0,6,1],height=1,rotation=0)
    exponent=dict(entity_id='e',entity_path=['E'],kind='TEXT',text='2',layout_id='l',layer='0',
        origin=[6,.6],bbox=[6,.6,6.5,1.1],height=.5,rotation=0)
    target=SimpleNamespace(text=anchor['text'],entity_ids=['a'])
    def index(texts):return SimpleNamespace(snapshot='s',scene={'texts':texts},entity_evidence=lambda eid:[dict(entity_ids=[eid],quote=next(t['text'] for t in texts if t['entity_id']==eid))])
    return anchor,exponent,target,index


def test_scene_only_superscript_never_becomes_supported():
    a,e,t,make=scene_pair();r=read_unit_candidates(make([a,e]),t)[0]
    assert r['unit_resolution']=='candidate' and r['state']=='candidate'
    assert r['native_relation_evidence']==[] and r['measurement_comparability']=='unconfirmed'


@pytest.mark.parametrize('change',[{'layer':'foreign'},{'layout_id':'other'}, {'rotation':90},
                                  {'entity_path':['BLOCK','E']},{'height':1},{'origin':[6,0]}])
def test_misbound_superscript_rejected(change):
    a,e,t,make=scene_pair();e.update(change)
    assert read_unit_candidates(make([a,e]),t)==[]


def test_two_exponents_and_two_competing_anchors_stay_ambiguous():
    a,e,t,make=scene_pair();e2=dict(e,entity_id='e2',entity_path=['E2'],text='3')
    results=read_unit_candidates(make([a,e,e2]),t)
    assert len(results)==2 and all(r['ambiguous'] and r['unit_resolution']=='candidate' for r in results)
    a2=dict(a,entity_id='a2',entity_path=['A2'],text='面积40m')
    result=read_unit_candidates(make([a,a2,e]),t)[0]
    assert result['competing_anchor_count']==2 and result['ambiguous']


def test_elevation_surface_hints_never_create_datum_or_height():
    target=SimpleNamespace(text='  结构标高-5.000  ',entity_ids=['a'],region_id='r')
    r=read_elevation_annotation(target)
    assert r['surface']=='structure' and r['values'][0]['value']==-5
    assert all(r['quote'][slice(*v['text_span'])]==v['raw'] for v in r['values'])
    assert r['datum']==r['pairability']=='unresolved' and r['derived_height'] is None
    target.text='-0.100与-5.000';r=read_elevation_annotation(target)
    assert len(r['values'])==2 and r['surface']=='unresolved' and r['derived_height'] is None
    target.entity_ids=[];assert read_elevation_annotation(target) is None


def test_native_text_metadata_is_bounded_ignores_nested_and_checks_stamp(tmp_path):
    doc=ezdxf.new('R2018');root=doc.modelspace().add_text('面积30m');nested=doc.blocks.new('B').add_text('2')
    line=doc.modelspace().add_line((0,0),(1,1));p=tmp_path/'source.dxf';doc.saveas(p)
    payload=dict(source_path=str(p),source_stamp=list(file_stamp(p)),sid='s',handles=[root.dxf.handle,nested.dxf.handle,line.dxf.handle,'FFFF'])
    result=compute_text_metadata(payload)
    assert list(result)==[root.dxf.handle] and result[root.dxf.handle]['style']=='Standard'
    with pytest.raises(ValueError,match='24'):compute_text_metadata(dict(payload,handles=['A']*25))
    with pytest.raises(ValueError,match='变化'):compute_text_metadata(dict(payload,source_stamp=[0,0]))


def test_text_metadata_worker_path_does_not_parse_in_parent(workspace,monkeypatch):
    import os
    sid='snap_b06';path=workspace.store.artifact(sid,'source.dxf');path.parent.mkdir(parents=True,exist_ok=True)
    doc=ezdxf.new('R2018');t=doc.modelspace().add_text('面积30m');doc.saveas(path)
    monkeypatch.setattr('ezdxf.lldxf.fileindex.load',lambda *a:pytest.fail('parent native DXF parsing'))
    result=source_text_metadata(workspace,sid,[t.dxf.handle])
    assert result[t.dxf.handle]['text']=='面积30m'
    assert workspace.geometry_executor.last_worker_pid!=os.getpid()


def test_explicit_user_unit_remains_assumption(monkeypatch):
    batches={}
    fake=SimpleNamespace(drawing={'unit':'unknown'},measure_selected=lambda batch:None)
    monkeypatch.setattr('cad_agent.geometry.room_batch.RoomBatchService',lambda *a:fake)
    workspace=SimpleNamespace(store=SimpleNamespace(put=lambda kind,key,value:batches.update({key:value})))
    result=resolve_units(workspace,'s',dict(batch_id='b',scope={},items=[]),dict(condition='explicit',unit='mm'))
    assert result['unit_status']=='assumption' and result['unit_resolution']['source_unit']=='unknown'
    fake.drawing={'unit':'m'}
    result=resolve_units(workspace,'s',dict(batch_id='b',scope={},items=[]),dict(condition='explicit',unit='mm'))
    assert result['unit_status']=='conflict' and result['unit']=='unknown'


def test_dimension_cache_version_and_unit_provenance_reach_public_page(monkeypatch):
    import json
    import cad_agent.components.service as module
    from cad_agent.components.contracts import DimensionQuery
    records={};calls=[]
    class Store:
        def get(self,kind,key):return records[key]
        def put(self,kind,key,value):records[key]=value
    svc=module.ComponentService.__new__(module.ComponentService)
    svc.w=SimpleNamespace(store=Store());svc.sid='snap_fixture';svc.drawing={'unit':'unknown','sha256':'sha'}
    svc.scope=lambda args:(None,[],None)
    monkeypatch.setattr(module,'read_dimensions',lambda *a:calls.append(a) or dict(dimensions=[],diagnostics=[]))
    args=DimensionQuery(layout_id='l',unit_override='mm')
    request=args.model_dump(exclude={'offset','limit','view'})|{'bbox':None}
    legacy=stable_id('component',svc.sid,'sha',module.COMPONENT_VERSION,module.GEOMETRY_VERSION,module.DISCOVERY_VERSION,'dimensions',json.dumps(request,sort_keys=True))
    records[legacy]=dict(snapshot_id=svc.sid,source_sha256='sha',legacy=True)
    fresh=svc.build('dimensions',args)
    assert fresh['record_id']!=legacy and 'legacy' not in fresh and len(calls)==1
    assert svc.build('dimensions',args)==fresh and len(calls)==1
    page=svc.page(fresh,'dimensions')
    assert page['unit_basis']=='request_override' and page['unit_status']=='assumption'
    assert page['dimension_version']==module.DIMENSION_VERSION
    svc.drawing['unit']='m'
    changed=svc.build('dimensions',DimensionQuery(layout_id='l',unit_override='cm'))
    assert changed['unit_status']=='assumption' and changed['unit_source_conflict']
